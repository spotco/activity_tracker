# Brave/Chrome focused-hostname integration — implementation plan

**Date:** 2026-11-01  
**Repository:** `spotco/activity_tracker`  
**Implementation agent:** Cursor Composer 2.5  
**Status:** Approved plan; implementation not started  
**Decision:** Preserve exact hostnames, including subdomains. Do not collapse `www`, `docs`, `mail`, etc.

## Goal and scope

Enrich the existing Windows foreground-application tracker with the **hostname of the selected tab in the focused Brave browser window**, plus hourly application/domain breakdowns. Chromium/Chrome compatibility is desired, but **actual Brave compatibility is a release requirement**.

This is observational activity data, *not* a measure of productivity or intentionality. Do not infer what work was useful, planned, necessary, or distracting from the hostname.

### Agreed decisions

- Hostnames remain distinct: `github.com`, `gist.github.com`, `chatgpt.com`, `docs.google.com`, and `mail.google.com` have separate buckets.
- Normalize hostnames to lowercase, remove a final dot, and use a consistent IDNA representation if required. **Do not** strip `www` or combine registrable domains.
- Extract the host from HTTP(S) tab URLs **in the extension**. Never send or persist full URLs, paths, query strings, fragments, titles, page contents, keystrokes, or browsing history. Do not observe incognito windows. Internal/new-tab/unsupported pages yield an explicit unknown state.
- Windows foreground sampling and idle/lock/sleep checks remain the **only authority for countable duration**. Extension messages can provide *labels*, never additional seconds.
- Keep the existing per-user local IPC and in-memory-before-export workflow. No HTTP server, database, network telemetry, hosted component, auto-save, or automatic upload.
- Do not modify `osphoto-dev`, Life & Games, or other repositories.
- No frontend framework and no new infrastructure beyond a lightweight MV3 extension, local native host, and installer.

## Existing repository architecture (reviewed 2026-11-01)

| File | Existing purpose | Targeted changes |
| --- | --- | --- |
| `tracker_core.py` | Platform-independent conservative interval accounting; daily totals and JSON export | Optional browser label on already eligible intervals; hourly buckets; v2 export |
| `windows_api.py` | Foreground exe, Windows last input, desktop lock, monotonic and wall-clock sampling | Prefer no changes unless strictly necessary |
| `tracker_config.py` | 5-second default sample interval, idle threshold, exclusions, aliases | Minimal browser-integration config, preserve old config loading |
| `tracker_ipc.py` | Authenticated per-user Windows named-pipe command channel | Add validated, bounded browser-state command |
| `activity_tracker.pyw` | Console-free tray entry and sampling loop | Consume browser observations and share state with sampler |
| `activity_tracker_cli.py` | CLI commands; alternate standalone runner | Identical browser intake/labeling behavior |
| `tests/` | Deterministic Python accounting and CLI tests | Expand coverage without requiring Win32 for pure tests |
| `.github/workflows/tests.yml` | Windows Python unittest CI | Add JS tests if needed; keep existing tests passing |

PR #1 is already merged into `main`: new installations default to **5-second** polling; old `config.json` files retain their existing value and need manual adjustment/restart. The tracker intentionally drops unknown switch intervals rather than inventing time. Preserve that behavior.

## Architecture

```text
Focused Brave tab
   | MV3 tabs/windows/navigation events + short heartbeat
   v
browser_extension/    [hostname/focused/unknown state only; no timing totals]
   | chrome.runtime.connectNative()
   v
browser_native_host.py / packaged host executable
   | framed stdin/stdout; validate; forward
   v
tracker_ipc.py        [existing per-user named pipe]
   v
tracker_browser.py    [latest valid observation, freshness, generation]
   | snapshot at existing Windows poll
   v
ActivityTracker       [Windows foreground + idle/lock/sleep authoritative]
   v
Explicit JSON export  [daily programs + hourly programs + browser host buckets]
```

**Important:** The extension is not a second timer. Every credited hostname second must be a subset of seconds already credited to the corresponding browser executable. A missing, uncertain, mismatched or stale label becomes `domain_unknown` *within counted browser time*, not a fabricated untracked interval.

## Cursor implementation sequence

### 0. Establish baseline and feature branch

1. Pull latest `main` and create a focused branch such as `feature/brave-domain-tracking`.
2. Read `README.md`, the files listed above, and tests; run `py -3.11 -m unittest discover -s tests -v` on Windows and record the baseline.
3. Keep edits bounded; commit coherent, tested stages. Avoid redesigning the sampling engine or command channel.

### 1. Chromium extension (`browser_extension/`)

1. Add `manifest.json` targeting **Manifest V3**, a background service worker, and a very small connection-status popup.
2. Use narrowly required `tabs`/`windows` events and relevant tab URL permission to observe selected-tab activation, relevant URL/navigation updates, browser window focus/blur, and extension startup. Do not use content scripts or the `history` API.
3. On initialization and every state-changing event, compute a state such as `{focused, hostname|null, generation}`. Treat incognito, chrome/brave internal URLs, no active window, unavailable tab URL and unsupported schemes as unknown. On changed tab/focus/hostname, advance generation even if two consecutive tabs share a hostname, so an uncertain transition interval is not silently credited.
4. Keep a native Messaging port; send state changes promptly and a periodic heartbeat around 5 seconds while connected. Handle MV3 worker restart and native-port disconnect/reconnect; no assumption that an unconnected service worker runs forever.
5. No in-extension elapsed-time accumulation. Popup only shows connected/disconnected and current hostname (or unknown), with a concise privacy explanation.

### 2. Local Native Messaging host and Windows setup

1. Implement `browser_native_host.py` with Chromium's 4-byte native-endian length-prefixed JSON framing over stdin/stdout, bounded read lengths, valid UTF-8 and schema validation. **Never print logs to stdout**; errors/logging go to stderr.
2. Forward only validated state to the existing `tracker_ipc` command channel. On tracker unavailable, safely retry/reconnect without buffering old observations as newly current.
3. Package as a local Windows host executable with predictable runtime dependencies (a normal Python console script alone should not require the user to install a Python interpreter for the browser host). Provide repeatable build instructions.
4. Provide `install_host.ps1` and `uninstall_host.ps1` for HKCU-level native-host registration; require explicit extension ID and allowlist exactly that ID in the host manifest. Do not grant wildcard origins. Document the correct Brave registry discovery path and Chrome compatibility; verify real Brave behavior rather than assuming both browsers search identical keys.
5. Installation must not require admin rights. Include native host status and troubleshooting for extension ID changes caused by unpacked extension/reinstall.

### 3. Shared browser-state ingestion

1. Add a small, platform-independent `tracker_browser.py` module responsible for strict schema validation, safe hostname normalization, tracker-local reception timestamps, and fresh/unknown states.
2. Use a generation/sequence or state epoch to avoid attributing time across tab/focus transitions. Introduce a **15-second** stale-observation cutoff (configurable only if a compelling test scenario requires it). Reject replay/out-of-order or malformed data.
3. Add one shared ingestion/IPC handler and wire it into both `activity_tracker.pyw` and the standalone `activity_tracker_cli.py`. Protect shared state consistently with the existing locking/threading model.
4. Take an immutable snapshot of browser state *at each Windows poll*. Ensure a later arrival cannot retroactively label an earlier sample. On tracker restart/reset, clear the browser eligibility baseline; do not reuse stale state or count pre-start activity.
5. Default to Brave only. If Chrome is supported in the same iteration, keep identities isolated and test that a Chrome extension cannot attribute `brave.exe` time or vice versa.

### 4. Conservative domain accounting and hourly buckets

1. Extend `ActivitySample` or sampling input in a backward-compatible way to carry an optional, validated, immutable browser observation snapshot. Keep all existing Win32 interval gates.
2. For an otherwise creditable browser interval, label it by hostname **only when observations at both endpoints are valid, fresh, focused, and identical in hostname and generation, and the Windows executable is the expected browser**. Otherwise put that already-counted browser interval into `domain_unknown`.
3. Apply the same eligible-idle-window slicing already used for program totals; split at local-day and **timezone-offset-aware local-hour** boundaries. Do not credit an interval that fails executable/idle/lock/suspend/wall-clock gates.
4. Record hourly totals for **all** programs and hostname sub-buckets for Brave. Use unambiguous hour identifiers (local date/hour plus UTC offset or UTC start) so repeated fall-back DST hours do not merge.
5. Export schema `schema_version: 2`, retaining the existing `days[].programs[]` fields and original meanings. Add documented hourly structures and hostname buckets; round at export only to avoid cumulative drift. Apply program exclusions to *all* summaries and nested browser data; preserve alias display names without changing executable identity.
6. Separate the three concepts in reports: (a) credited Brave time attributed to a hostname, (b) credited Brave time with `domain_unknown`, and (c) intervals that were **not credited at all**. Do not imply (c) is an accurately known duration without sufficient observation.

### 5. Tests and verification

- Python unit tests: normalization and invalid inputs; framing and message-size boundaries; malformed or unauthorized/mismatched identities; duplicate/stale/out-of-order messages; reconnect and tracker resets.
- Core accounting tests: constant tab; same-hostname tab transition with changed generation; hostname switch; focus lost; Brave-to-Cursor switch; extension disconnect; fresh-to-stale boundary; Windows idle grace, lock, suspend and sleep gaps; midnight and DST fallback; excluded browser; alias preservation; CLI/tray shared-handler behavior.
- JSON tests: old daily program fields unaffected; v2 structure; hourly sums match daily sums; **sum of per-hostname seconds + domain_unknown seconds equals the counted browser seconds** per hour/day within documented rounding tolerance. No browser time counted twice.
- JS unit tests (minimal Node built-in test runner or similarly light tool): event-state transitions, URL-to-hostname extraction, correct subdomain separation, unknown/incognito handling, heartbeat/reconnect behavior. Extend CI without introducing heavy web tooling.
- Real Windows/Brave smoke: load unpacked extension via `brave://extensions`, obtain its ID, run host installer, confirm native Messaging connects, visit `github.com` then `chatgpt.com`, switch focus to Cursor, go idle, reconnect/restart, reset and export with existing CLI. Verify the report shows separate hostnames and no inflation. Repeat a targeted test for Chrome if advertised as tested.
- An automated mock/CI success is **not** evidence of an actual Brave smoke test. Explicitly state which checks could not be run.

### 6. Documentation and delivery

1. Update `README.md` for permissions, installation/uninstallation, host registration, security/privacy, schema v2, sample JSON export, daily/hourly totals, and limitations.
2. Include the exact manual steps for an interactive Windows Brave smoke test if Composer cannot run one.
3. Create a focused PR with commits, tests run, outcomes, unresolved risks, and any manual action needed.
4. No edits to other repositories. Do not mark Brave compatibility verified until a true Brave integration test passes.

## Suggested example output

The exact v2 JSON field names are up to the implementation, but preserve these semantics:

```text
Date: 2026-11-01
  brave.exe: 45 minutes credited
    github.com: 20 minutes
    chatgpt.com: 15 minutes
    domain_unknown: 10 minutes
  Cursor.exe: separately credited
Hourly report: same breakdown split across the correct local hours
```

The 45 Brave minutes are **45 total**, not 45 plus website minutes. The `domain_unknown` category means counted browser time lacking reliable hostname evidence; it does not include unsampled, locked, idle-excluded or otherwise uncounted time.

## Definition of done

- The unpacked extension and native host **actually communicate in Brave on Windows**.
- Browser hostnames remain distinct: `docs.google.com` does not merge into `google.com` and `www.github.com` does not merge into `github.com`.
- Existing CLI and tray tracking still work; browser integration is additive and safe to disable.
- Daily totals remain consistent with hourly totals; browser domain totals partition browser credited time, never add to it.
- Idle, lock, sleep, tab switch, browser-to-app switch, stale/disconnected extension, reset and restart do not inflate or mislabel time.
- Tests pass; documentation states limitations and separately identifies anything not verified interactively.
- Focused implementation PR is ready for review.

## Copy/paste handoff for Cursor Composer 2.5

> Implement this plan in order from `plans/2026-11-01/plugin_impl.md`. Start from latest `main`, create a feature branch, inspect the listed files, preserve conservative accounting and privacy boundaries, commit bounded tested stages, and submit a focused PR. The exact-hostname decision is final; do not ask again. No new database, server, hosted service, or other repository changes. Verify actual Brave native Messaging if available, otherwise provide precise manual smoke steps and report it unverified. When an unforeseen architectural decision would substantially expand the scope, stop and ask before implementing it.

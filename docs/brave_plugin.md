# Brave hostname plugin

The extension sends the focused tab's hostname to the local activity tracker. Windows foreground sampling still decides how many seconds are credited. The hostname is only a label on time that Brave already earned. Uncertain labels are stored as `domain_unknown` inside that Brave time.

This note is the setup that actually works on the Brave build tested here (November 2026), including the native-host discovery failure and the checks that proved the fix.

## How it is wired

```text
Brave extension (browser_extension/)
  chrome.runtime.connectNative("com.spotco.activity_tracker")
    -> native host exe (stdio, length-prefixed JSON)
      -> per-user named pipe
        -> activity tracker (activity_tracker.pyw or the CLI session)
```

| Piece | Location |
| --- | --- |
| Extension | `browser_extension/`, loaded unpacked in Brave |
| Host name | `com.spotco.activity_tracker` |
| Built host | `dist/activity_tracker_native_host.exe` (gitignored; build with `scripts/build_native_host.ps1`) |
| Installed host | `%LOCALAPPDATA%\ActivityTracker\native_host\activity_tracker_native_host.exe` |
| Manifest | `%LOCALAPPDATA%\ActivityTracker\native_host\com.spotco.activity_tracker.json` |
| Registry value | `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.spotco.activity_tracker` |
| Tracker config | `config.json` next to the tracker, not a second config under LocalAppData |

`allowed_origins` is one extension id, `chrome-extension://<id>/`. Do not use a wildcard.

The id is assigned by Brave from the unpacked folder path. On this machine it is `pccnbfglhhflbjeapnjgijgdogfgjpcl`. Another folder path gets another id, and the manifest must be reinstalled for that id.

## Install

1. Start the tracker (`activity_tracker.pyw`).
2. In Brave, open `brave://extensions`, turn on Developer mode, and Load unpacked on `browser_extension/`.
3. Copy the extension id from that card.
4. Build and register the host:

```powershell
.\scripts\build_native_host.ps1
.\scripts\install_host.ps1 -ExtensionId YOUR_EXTENSION_ID_HERE
```

5. Reload the extension on `brave://extensions`.

`install_host.ps1` copies the built exe into `%LOCALAPPDATA%\ActivityTracker\native_host\`, writes the manifest as UTF-8 without a BOM, and sets the registry default value to that manifest path. The manifest `path` is an absolute `.exe` path with Windows backslashes.

Uninstall with `.\scripts\uninstall_host.ps1`. That removes both the Chrome key this Brave reads and the older `HKCU\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.spotco.activity_tracker` key.

## What had to be true before Brave would launch the host

Brave reported `Specified native messaging host not found` and never started the exe. A direct stdin test of the same exe already returned a tracker ping, so the failure was discovery, not the tracker pipe and not native-messaging framing.

These attempts did not register a host Brave would start:

| Attempt | Result |
| --- | --- |
| Host path pointing at a `.cmd` that ran Python | Same "host not found" error. On Windows the manifest path has to be an `.exe`. |
| Manifest path on `E:\` with forward slashes, or only under `HKCU\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts` | Brave still reported host not found. No `activity_tracker_native_host.exe` process was created. |
| A C# launcher that proxied stdin/stdout to `python.exe` | The length prefix was corrupted (`Native message length out of bounds`). That launcher was removed. |
| Reading `chrome.runtime.lastError` immediately after `connectNative` | The real error arrives later, on `onDisconnect`. An immediate read was a stale or empty error and made the popup say the port was missing. |

The check that found the working key: duplicate the same manifest under `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.spotco.activity_tracker`, then probe the already-running Brave service worker (remote debugging port 9222, no second Brave process). `connectNative` then returned `{ok: true, tracker: {ok: true, command: "ping"}}`. This Brave build looks up native hosts in Chrome's registry location. `install_host.ps1` writes that key.

The live probe is `node scripts/probe_native_cdp.mjs`. It resolves the current service-worker target from `http://127.0.0.1:9222/json` and must not reuse an old target id. It only works when Brave was started with `--remote-debugging-port=9222`.

## Startup order and reboot

Once Brave and the tracker are both running, connection does not depend on which one started first.

- The extension calls `connectNative` when its service worker starts (`chrome.runtime.onStartup`, install, and the script's own startup).
- If the tracker is not listening yet, the extension retries about every 2 seconds while the worker is awake, and a one-minute alarm wakes the worker later.
- If Brave starts second, the extension connects as soon as the worker starts. The native host process is started by Brave on each connection. It is not a service you launch yourself.

A reboot keeps the registry value, the installed exe, and the unpacked extension. Neither Brave nor the tracker is in the Windows startup list, so after a restart you open Brave and start `activity_tracker.pyw` again. Tracked totals live in memory until you export them. A new tracker process starts empty.

## Popup hostname

The popup used to query `chrome.windows.getAll` for a window with `focused: true`. Opening the popup takes focus, so that query found nothing and the popup printed `hostname: unknown (no focused window)` even while the tracker was receiving the real hostname.

The popup now uses `chrome.windows.getLastFocused`. That is the browser window the popup was opened from. Internal pages (`brave://`, `chrome://`, and similar) still show unknown, because those URLs are not hostnames the tracker stores.

The tracker's own view is `py -3.11 activity_tracker_cli.py status`. A healthy session looks like `browser_plugin.connected: true` with a recent `last_message_seconds_ago` and `last_ingest.ok: true`. The extension also has an options page, `browser_extension/debug.html` (Brave: extension card, Extension options), with the same channel counters.

## Accounting check

A hostname is credited only when the same focused hostname is stable at both ends of a poll interval. Anything less stays `domain_unknown` inside Brave's seconds.

```powershell
py -3.11 activity_tracker_cli.py export .\smoke.json --overwrite
```

For each day and each hour, `brave.exe` `seconds` equals the sum of that entry's `browser_domains` seconds, including `domain_unknown`. The domain lines do not add extra time.

## Debug notes that are easy to misread

- `browser_plugin.connected: false` with `last_message_seconds_ago: null` means the tracker has never accepted an extension message this session. That was the state while Brave could not find the host.
- `connected: false` with a numeric age means a message arrived and then went stale (the extension stopped sending, usually because the host or worker died).
- `last_ingest.error` is a tracker rejection (session epoch, sequence, or hostname), which is different from "host not found".
- Extension card **Errors** showing `Specified native messaging host not found` means the registry lookup failed. Fix the key or the manifest, then reload the extension. Do not treat that string as a broken exe until a manual framed `{"type":"status"}` write to the exe also fails.

# Windows Foreground Activity Tracker (Python v1)

A small Windows 10/11 tray application that estimates foreground application use. It stores activity totals in process memory and only writes activity data when you explicitly export it.

## Install and run

1. Install Python 3.11 or newer for Windows, including Tcl/Tk.
2. From this folder, install the two tray dependencies:

   ```powershell
   py -3.11 -m pip install -r requirements.txt
   ```

3. Start the app without a console:

   ```powershell
   pyw -3.11 activity_tracker.pyw
   ```

   You can also launch the installed `pythonw.exe` with `activity_tracker.pyw`.
   The app creates `config.json` beside the script with defaults if the file is missing. It does not start automatically with Windows and does not need administrator privileges. **For all-day coverage, arrange for the app to launch at Windows sign-in** (for example via the user's Startup folder); otherwise hours before launch are not sampled.

You can also double-click `run_activity_tracker.bat` to start the tray app. The launcher starts the console-free `pythonw` entry point and closes its command window immediately. Double-clicking it again while the app is running is a silent no-op; the existing tracker continues running.

Right-click the notification-area icon for **Show Data**, **Reset Data**, and **Exit**. Show Data opens the current JSON in a scrollable, read-only popup. In the tray-only app, that popup also has Copy JSON, Refresh, Export JSON, Close, and Exit buttons. Reset Data asks for confirmation and starts a fresh sampling baseline. Exit stops tracking; unexported data is lost.

## Agent-accessible CLI

The CLI can control the live tracker from another process, including when the tray app was started with `run_activity_tracker.bat`. For example:

```powershell
py -3.11 activity_tracker_cli.py status
py -3.11 activity_tracker_cli.py show
py -3.11 activity_tracker_cli.py export "C:\Users\Me\My Data\activity.json"
py -3.11 activity_tracker_cli.py reset --yes
py -3.11 activity_tracker_cli.py exit
```

Reset requires `--yes`; exporting over an existing file requires `--overwrite`. One-shot commands print a JSON response and return a nonzero exit code when a command fails. Running `py -3.11 activity_tracker_cli.py` with no command attaches an interactive prompt to the current tracker if one is running.

To run a terminal-controlled tracker when the tray app is not already running, start the CLI with no command. It creates its own tracking session and keeps the terminal attached. The tray app and CLI share the same config, accounting rules, exclusions, in-memory storage, and per-user local command channel. Only one tracker session can run at a time. The CLI tray menu also has **Show Data**, **Reset Data**, and **Exit**. The `show` command opens the popup; it does not dump activity records into the terminal.

- `status` — tracking settings and exportable totals.
- `show` — open the scrollable popup with the current filtered JSON (`view` is an alias).
- `export <path>` — write a pretty-printed UTF-8 snapshot; use `--overwrite` to replace an existing file.
- `reset` — clear totals after typing `RESET` in interactive mode, or pass `--yes` for a one-shot command.
- `help` and `exit` — show commands or stop tracking. Exiting loses unexported data.

Paths with spaces may be quoted. The local command channel uses a per-user Windows named pipe and carries commands only; activity data remains in memory until exported. The `requirements.txt` dependencies enable the tray icon, while CLI tracking itself only needs Python's standard library and native Windows APIs.

## Configuration

Edit `config.json` before launch. Values are loaded once at startup.

- `poll_interval_seconds`: integer from 1 to 3600; default **5** for new installations. Existing `config.json` files are deliberately not rewritten; if yours is set to 30, change it to 5 and restart the tracker to reduce missed time on app switches.
- `idle_threshold_seconds`: integer from 1 to 86400; default 300.
- `excluded_programs`: executable names excluded from View and Export, matched case-insensitively against the actual executable name.
- `program_aliases`: optional executable-to-display-name mappings, also matched case-insensitively. Aliases do not change program identity.

Invalid JSON or values are shown in a startup error dialog. Unknown configuration keys are ignored so the structure can be extended later.

The default exclusion list is empty, so all identifiable foreground applications are included, including terminals and browsers. The tracker process itself, intervals without an identifiable foreground process, locked-desktop intervals, AFK time past the grace period, and long sleep/delay gaps remain untracked.

## Accounting and limitations

- The tracker samples the foreground window and Windows last-input timestamp. It stores executable names only; it does not collect window titles, URLs, keystrokes, or mouse coordinates.
- A sampling interval is credited only when the same executable is identified at both endpoints. An interval where the foreground app changed is left untracked because the transition time was not observed. At a 30-second poll interval, each detected switch can discard up to 30 seconds; the new 5-second default reduces that worst-case loss to 5 seconds per detected switch but does **not** reconstruct the exact transition time or capture rapid switches between polls. Polling does not provide exact active playtime.
- Time is measured between actual callback timestamps. Intervals longer than twice the configured polling interval are discarded to avoid crediting sleep, suspension, or a delayed callback as continuous use. The next sample becomes a new baseline.
- The five-minute idle grace period intentionally allows up to five minutes after the last keyboard or mouse input to count. When the threshold is crossed, counting stops at that cutoff. Returning after a longer AFK period does not backfill the absence; time begins again from the observed Windows input timestamp.
- A desktop other than the `Default` input desktop is treated as locked. If Windows cannot report the input desktop, that interval is skipped.
- Totals split across local calendar dates and keep fractional seconds internally. JSON `minutes` is derived from `seconds`.
- Excluded programs may still be collected in memory, but are removed from both View and Export before the snapshot is built.
- Nothing is saved automatically. Reset clears the in-memory totals **and updates `tracking_started_at` to the reset time**; exiting loses all unexported data. There is no database, network activity, telemetry, or API integration.
- The JSON is a versioned export of observations, not a Life & Games database record and does not infer whether activity was gaming, productive, intentional, or debugging.

## Tests

Run the platform-independent accounting tests with:

```powershell
py -3.11 -m unittest discover -s tests -v
```

The tests use synthetic timestamps and do not generate keyboard or mouse input. The Win32 calls, tray icon, dialogs, and locked-desktop behavior require a Windows interactive desktop for a GUI smoke test.

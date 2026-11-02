"""Agent-operable CLI and small tray UI for the Windows foreground tracker."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable

from tracker_config import ConfigError, TrackerConfig
from tracker_core import ActivityTracker
from tracker_browser import BrowserStateStore
from tracker_ipc import AgentChannelUnavailable, AgentCommandServer, send_agent_command
from tracker_sampling import (
    build_status_response,
    ingest_browser_state_request,
    observe_with_browser_context,
    reset_tracker_state,
)
from windows_api import (
    acquire_single_instance_mutex,
    kernel32,
    release_single_instance_mutex,
)


Output = Callable[[str], None]
Input = Callable[[str], str]


class TrackerCommands:
    """CLI handlers. GUI requests are queued for Tk's main thread."""

    def __init__(
        self,
        tracker: ActivityTracker,
        *,
        output: Output = print,
        input_fn: Input = input,
        show_callback: Callable[[], None] | None = None,
        lock: threading.RLock | None = None,
        browser_store: BrowserStateStore | None = None,
    ) -> None:
        self.tracker = tracker
        self.output = output
        self.input = input_fn
        self.show_callback = show_callback or (lambda: None)
        self.lock = lock or threading.RLock()
        self.browser_store = browser_store or BrowserStateStore()

    def execute(self, line: str) -> bool:
        """Run one command. Return False only when the CLI should exit."""
        command, argument = _parse_command(line)
        if not command:
            return True

        if command in {"help", "?"}:
            self.output(
                "Commands: help, status, show, export <path>, reset, exit\n"
                "show opens the tracked JSON in a scrollable popup. Paths may be quoted."
            )
        elif command == "status":
            with self.lock:
                self._json(build_status_response(self.tracker, self.browser_store, self.tracker.config))
        elif command in {"show", "view"}:
            # Keep view as an alias, but never dump the activity records into
            # the terminal. The popup is created on Tk's main thread.
            self.show_callback()
            self._json({"ok": True, "command": "show", "popup_requested": True})
        elif command == "export":
            self._export(argument)
        elif command == "reset":
            confirmation = self.input("Type RESET to clear all in-memory tracked data: ")
            if confirmation.strip().casefold() != "reset":
                self._json({"ok": False, "command": "reset", "cancelled": True})
            else:
                with self.lock:
                    reset_tracker_state(self.tracker, self.browser_store, self.lock)
                self._json({"ok": True, "command": "reset", "cleared": True})
        elif command in {"exit", "quit"}:
            self._json({"ok": True, "command": "exit", "message": "Unexported data will be lost."})
            return False
        else:
            self._json({"ok": False, "command": command, "error": "Unknown command. Use help."})
        return True

    def _export(self, argument: str) -> None:
        if not argument:
            self._json({"ok": False, "command": "export", "error": "Usage: export <path>"})
            return
        path = Path(argument).expanduser()
        if path.exists():
            answer = self.input(f"{path} exists. Type OVERWRITE to replace it: ")
            if answer.strip().casefold() != "overwrite":
                self._json({"ok": False, "command": "export", "cancelled": True})
                return
        try:
            with self.lock:
                snapshot = self.tracker.export_data()
            with path.open("w", encoding="utf-8", newline="\n") as output_file:
                json.dump(snapshot, output_file, ensure_ascii=False, indent=2)
                output_file.write("\n")
        except (OSError, TypeError, ValueError) as exc:
            self._json({"ok": False, "command": "export", "error": str(exc)})
            return
        self._json({"ok": True, "command": "export", "path": str(path), "days": len(snapshot["days"])})

    def _json(self, value: dict) -> None:
        self.output(json.dumps(value, ensure_ascii=False, indent=2))


class TrackerWindow:
    """Tk viewer and tray action dispatcher; all widget work stays on Tk's thread."""

    def __init__(
        self,
        root: tk.Tk,
        tracker: ActivityTracker,
        lock: threading.RLock,
        requests: queue.Queue[str],
        browser_store: BrowserStateStore,
    ) -> None:
        self.root = root
        self.tracker = tracker
        self.lock = lock
        self.browser_store = browser_store
        self.requests = requests
        self.viewer: tk.Toplevel | None = None
        self.viewer_text: tk.Text | None = None
        self.running = True
        self.root.withdraw()
        self.root.title("Windows Foreground Activity Tracker")
        self.root.protocol("WM_DELETE_WINDOW", self.request_exit)
        self.root.after(200, self._process_requests)

    def request_exit(self) -> None:
        self.requests.put("exit")

    def _process_requests(self) -> None:
        if not self.running:
            return
        try:
            while True:
                request = self.requests.get_nowait()
                if request == "show":
                    self.show_data()
                elif request == "reset":
                    self.reset_data()
                elif request == "exit":
                    self.running = False
                    self.close_viewer()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(200, self._process_requests)

    def _snapshot_text(self) -> str:
        with self.lock:
            snapshot = self.tracker.export_data()
        return json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"

    def show_data(self) -> None:
        if self.viewer is not None and self.viewer.winfo_exists():
            self.refresh()
            self.viewer.deiconify()
            self.viewer.lift()
            return

        window = tk.Toplevel(self.root)
        self.viewer = window
        window.title("Tracked Activity Data")
        window.geometry("700x520")
        window.minsize(480, 320)
        window.protocol("WM_DELETE_WINDOW", self.close_viewer)

        frame = tk.Frame(window, padx=10, pady=10)
        frame.pack(fill=tk.BOTH, expand=True)
        text_frame = tk.Frame(frame)
        text_frame.pack(fill=tk.BOTH, expand=True)
        vertical = tk.Scrollbar(text_frame)
        vertical.pack(side=tk.RIGHT, fill=tk.Y)
        horizontal = tk.Scrollbar(text_frame, orient=tk.HORIZONTAL)
        horizontal.pack(side=tk.BOTTOM, fill=tk.X)
        text = tk.Text(
            text_frame,
            wrap=tk.NONE,
            font=("Consolas", 10),
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
            undo=False,
        )
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vertical.config(command=text.yview)
        horizontal.config(command=text.xview)
        text.configure(state=tk.DISABLED)
        self.viewer_text = text

        buttons = tk.Frame(frame, pady=8)
        buttons.pack(fill=tk.X)
        tk.Button(buttons, text="Copy JSON", command=self.copy_json).pack(side=tk.LEFT)
        tk.Button(buttons, text="Refresh", command=self.refresh).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(buttons, text="Close", command=self.close_viewer).pack(side=tk.RIGHT)
        self.refresh()

    def refresh(self) -> None:
        if self.viewer_text is None or not self.viewer_text.winfo_exists():
            return
        payload = self._snapshot_text()
        self.viewer_text.configure(state=tk.NORMAL)
        self.viewer_text.delete("1.0", tk.END)
        self.viewer_text.insert("1.0", payload)
        self.viewer_text.configure(state=tk.DISABLED)

    def copy_json(self) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._snapshot_text())
            self.root.update()
        except tk.TclError as exc:
            messagebox.showerror("Copy JSON", str(exc), parent=self.viewer or self.root)

    def close_viewer(self) -> None:
        if self.viewer is not None:
            try:
                self.viewer.destroy()
            except tk.TclError:
                pass
        self.viewer = None
        self.viewer_text = None

    def reset_data(self) -> None:
        if messagebox.askyesno(
            "Reset Tracked Data",
            "Clear all accumulated activity data? This cannot be undone.",
            parent=self.root,
        ):
            with self.lock:
                reset_tracker_state(self.tracker, self.browser_store, self.lock)


def _parse_command(line: str) -> tuple[str, str]:
    stripped = line.strip()
    if not stripped:
        return "", ""
    command, separator, rest = stripped.partition(" ")
    argument = rest.strip() if separator else ""
    if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in {"'", '"'}:
        argument = argument[1:-1]
    return command.casefold(), argument


def _sampling_loop(
    tracker: ActivityTracker,
    config: TrackerConfig,
    browser_store: BrowserStateStore,
    lock: threading.RLock,
    stop_event: threading.Event,
) -> None:
    browser_store.stale_observation_seconds = float(config.browser_integration.stale_observation_seconds)
    while not stop_event.is_set():
        try:
            observe_with_browser_context(tracker, browser_store, config, lock)
        except Exception:
            pass
        stop_event.wait(config.poll_interval_seconds)


def _make_tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill=(32, 48, 68, 255))
    draw.rounded_rectangle((15, 31, 23, 49), radius=3, fill=(105, 190, 255, 255))
    draw.rounded_rectangle((28, 21, 36, 49), radius=3, fill=(105, 190, 255, 255))
    draw.rounded_rectangle((41, 13, 49, 49), radius=3, fill=(105, 190, 255, 255))
    return image


def _start_tray(requests: queue.Queue[str]):
    """Expose the data actions and Exit in the notification-area menu."""
    import pystray

    menu = pystray.Menu(
        pystray.MenuItem("Show Data", lambda _icon, _item: requests.put("show")),
        pystray.MenuItem("Reset Data", lambda _icon, _item: requests.put("reset")),
        pystray.MenuItem("Exit", lambda _icon, _item: requests.put("exit")),
    )
    icon = pystray.Icon(
        "windows_foreground_activity_tracker_cli",
        _make_tray_image(),
        "Activity Tracker",
        menu,
    )

    def run_icon() -> None:
        icon.run()

    tray_thread = threading.Thread(target=run_icon, name="activity-tray", daemon=True)
    tray_thread.start()
    return icon, tray_thread


def _standalone_agent_handler(
    request: dict,
    tracker: ActivityTracker,
    browser_store: BrowserStateStore,
    lock: threading.RLock,
    requests: queue.Queue[str],
) -> dict:
    """Handle agent commands for a tracker launched in interactive CLI mode."""
    command = str(request.get("command", "")).casefold()
    if command == "browser_state":
        return ingest_browser_state_request(request, browser_store, tracker.config, lock)
    if command == "ping":
        return {"ok": True, "command": "ping", "running": True}
    if command == "status":
        with lock:
            return build_status_response(tracker, browser_store, tracker.config)
    if command in {"show", "view"}:
        requests.put("show")
        return {"ok": True, "command": "show", "popup_requested": True}
    if command == "reset":
        if not request.get("confirmed"):
            return {
                "ok": False,
                "command": "reset",
                "confirmation_required": True,
                "message": "Use reset --yes to clear all in-memory tracked data.",
            }
        with lock:
            reset_tracker_state(tracker, browser_store, lock)
        return {"ok": True, "command": "reset", "cleared": True}
    if command == "export":
        argument = str(request.get("path", "")).strip()
        if not argument:
            return {"ok": False, "command": "export", "error": "Usage: export <path>"}
        path = Path(argument).expanduser()
        if path.exists() and not request.get("overwrite"):
            return {
                "ok": False,
                "command": "export",
                "confirmation_required": True,
                "path": str(path),
                "message": "Use export <path> --overwrite to replace an existing file.",
            }
        try:
            with lock:
                snapshot = tracker.export_data()
            with path.open("w", encoding="utf-8", newline="\n") as output_file:
                json.dump(snapshot, output_file, ensure_ascii=False, indent=2)
                output_file.write("\n")
        except (OSError, TypeError, ValueError) as exc:
            return {"ok": False, "command": "export", "error": str(exc)}
        return {"ok": True, "command": "export", "path": str(path), "days": len(snapshot["days"])}
    if command in {"exit", "quit"}:
        requests.put("exit")
        return {"ok": True, "command": "exit", "message": "Unexported data will be lost."}
    return {"ok": False, "command": command, "error": "Unknown command. Use help."}


def _run_attached_command(
    command: str,
    argument: str = "",
    *,
    confirmed: bool = False,
    overwrite: bool = False,
) -> dict:
    request = {"command": command, "confirmed": confirmed, "overwrite": overwrite}
    if argument:
        request["path"] = argument
    return send_agent_command(request)


def _run_one_shot(arguments: list[str]) -> int:
    command = arguments[0].casefold()
    if command in {"help", "-h", "--help"}:
        print(
            "Usage: activity_tracker_cli.py <command> [options]\n"
            "Commands: status, show, export <path> [--overwrite], reset --yes, exit"
        )
        return 0

    try:
        if command == "export":
            overwrite = "--overwrite" in arguments[1:]
            path_parts = [part for part in arguments[1:] if part != "--overwrite"]
            response = _run_attached_command(command, " ".join(path_parts).strip(), overwrite=overwrite)
        elif command == "reset":
            response = _run_attached_command(command, confirmed="--yes" in arguments[1:])
        elif command in {"status", "show", "view", "exit", "quit"}:
            response = _run_attached_command(command)
        else:
            response = {"ok": False, "command": command, "error": "Unknown command. Use help."}
    except (AgentChannelUnavailable, TimeoutError, ConnectionError) as exc:
        response = {"ok": False, "error": str(exc)}

    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") else 1


def _run_attached_cli() -> int:
    print("Connected to the running Activity Tracker. Type help for commands.")
    while True:
        try:
            line = input("tracker> ")
        except EOFError:
            return 0
        command, argument = _parse_command(line)
        if not command:
            continue
        if command in {"help", "?"}:
            print("Commands: help, status, show, export <path>, reset, exit")
            continue
        try:
            if command == "reset":
                if input("Type RESET to clear all in-memory tracked data: ").strip().casefold() != "reset":
                    response = {"ok": False, "command": "reset", "cancelled": True}
                else:
                    response = _run_attached_command(command, confirmed=True)
            elif command == "export":
                if not argument:
                    response = {"ok": False, "command": "export", "error": "Usage: export <path>"}
                else:
                    path = Path(argument).expanduser()
                    overwrite = False
                    if path.exists():
                        overwrite = input(f"{path} exists. Type OVERWRITE to replace it: ").strip().casefold() == "overwrite"
                        if not overwrite:
                            response = {"ok": False, "command": "export", "cancelled": True}
                            print(json.dumps(response, ensure_ascii=False, indent=2))
                            continue
                    response = _run_attached_command(command, str(path), overwrite=overwrite)
            else:
                response = _run_attached_command(command, argument)
        except (AgentChannelUnavailable, TimeoutError, ConnectionError) as exc:
            response = {"ok": False, "error": str(exc)}
        print(json.dumps(response, ensure_ascii=False, indent=2))
        if command in {"exit", "quit"}:
            return 0 if response.get("ok") else 1


def _read_cli(commands: TrackerCommands, requests: queue.Queue[str], output: Output) -> None:
    output("Windows Foreground Activity Tracker. Type help for commands.")
    while True:
        try:
            line = input("tracker> ")
        except EOFError:
            requests.put("exit")
            return
        if not commands.execute(line):
            requests.put("exit")
            return


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments:
        return _run_one_shot(arguments)
    try:
        send_agent_command({"command": "ping"})
    except AgentChannelUnavailable:
        pass
    else:
        return _run_attached_cli()

    mutex_handle: int | None = None
    root: tk.Tk | None = None
    stop_event: threading.Event | None = None
    sampler: threading.Thread | None = None
    agent_server: AgentCommandServer | None = None
    tray_icon = None
    tray_thread: threading.Thread | None = None
    cli_thread: threading.Thread | None = None
    try:
        launched_at = datetime.now().astimezone()
        config = TrackerConfig.load(Path(__file__).resolve().parent)
        mutex_handle, already_running = acquire_single_instance_mutex()
        if already_running:
            kernel32.CloseHandle(mutex_handle)
            mutex_handle = None
            print(json.dumps({"ok": False, "error": "The tracker is already running in this Windows session."}))
            return 1

        root = tk.Tk()
        root.withdraw()
        tracker = ActivityTracker(config, started_at=launched_at)
        browser_store = BrowserStateStore(
            stale_observation_seconds=float(config.browser_integration.stale_observation_seconds)
        )
        lock = threading.RLock()
        requests: queue.Queue[str] = queue.Queue()
        window = TrackerWindow(root, tracker, lock, requests, browser_store)
        output_lock = threading.Lock()

        def output(text: str) -> None:
            with output_lock:
                print(text, flush=True)

        commands = TrackerCommands(
            tracker,
            output=output,
            show_callback=lambda: requests.put("show"),
            lock=lock,
            browser_store=browser_store,
        )
        agent_server = AgentCommandServer(
            lambda request: _standalone_agent_handler(request, tracker, browser_store, lock, requests)
        )
        agent_server.start()
        stop_event = threading.Event()
        sampler = threading.Thread(
            target=_sampling_loop,
            args=(tracker, config, browser_store, lock, stop_event),
            name="activity-sampler",
            daemon=True,
        )
        sampler.start()

        try:
            tray_icon, tray_thread = _start_tray(requests)
        except Exception as exc:
            output(json.dumps({"ok": False, "tray_error": str(exc)}))

        cli_thread = threading.Thread(
            target=_read_cli,
            args=(commands, requests, output),
            name="activity-cli",
            daemon=True,
        )
        cli_thread.start()
        try:
            root.mainloop()
        except KeyboardInterrupt:
            output("\nInterrupted; unexported in-memory data will be lost.")
        return 0
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": f"Configuration error: {exc}"}))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        if stop_event is not None:
            stop_event.set()
        if sampler is not None and sampler.is_alive():
            sampler.join(timeout=3)
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass
        if tray_thread is not None and tray_thread.is_alive():
            tray_thread.join(timeout=3)
        if agent_server is not None:
            agent_server.close()
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        if mutex_handle:
            release_single_instance_mutex(mutex_handle)


if __name__ == "__main__":
    sys.exit(main())

"""Console-free system tray entry point for the Windows activity tracker."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Any

import pystray
from PIL import Image, ImageDraw

from tracker_config import ConfigError, TrackerConfig
from tracker_core import ActivityTracker
from tracker_ipc import AgentCommandServer
from windows_api import (
    acquire_single_instance_mutex,
    release_single_instance_mutex,
    show_windows_error,
    take_sample,
)


class _AgentRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.completed = threading.Event()
        self.response: dict[str, Any] | None = None


class TrackerApplication:
    def __init__(
        self,
        root: tk.Tk,
        config: TrackerConfig,
        mutex_handle: int,
        started_at: datetime,
    ) -> None:
        self.root = root
        self.config = config
        self.mutex_handle = mutex_handle
        self.tracker = ActivityTracker(config, started_at=started_at)
        self.commands: queue.Queue[Any] = queue.Queue()
        self.viewer: tk.Toplevel | None = None
        self.viewer_text: tk.Text | None = None
        self.running = True
        self.tray_thread: threading.Thread | None = None

        root.withdraw()
        root.title("Windows Foreground Activity Tracker")
        root.protocol("WM_DELETE_WINDOW", self.shutdown)

        menu = pystray.Menu(
            pystray.MenuItem("Show Data", lambda _icon, _item: self.commands.put("view")),
            pystray.MenuItem("Reset Data", lambda _icon, _item: self.commands.put("reset")),
            pystray.MenuItem("Exit", lambda _icon, _item: self.commands.put("exit")),
        )
        self.icon = pystray.Icon("windows_foreground_activity_tracker", _make_icon(), "Activity Tracker", menu)
        self.agent_server = AgentCommandServer(self._dispatch_agent_command)
        self.agent_server.start()
        try:
            self.tray_thread = threading.Thread(target=self._run_tray, name="activity-tray", daemon=True)
            self.tray_thread.start()
        except Exception:
            self.agent_server.close()
            raise

        self.root.after(0, self._sample)
        self.root.after(500, self._process_commands)

    def _run_tray(self) -> None:
        try:
            self.icon.run()
        except Exception as exc:
            self.commands.put(f"tray_error:{exc}")

    def _sample(self) -> None:
        if not self.running:
            return
        try:
            self.tracker.observe(take_sample())
        except Exception as exc:
            # A malformed observation or transient Win32 failure should not
            # terminate the tray application. A fresh observation will reset
            # the baseline where needed.
            self.commands.put(f"sample_error:{exc}")
        self.root.after(self.config.poll_interval_seconds * 1000, self._sample)

    def _process_commands(self) -> None:
        if not self.running:
            return
        try:
            while True:
                command = self.commands.get_nowait()
                if isinstance(command, _AgentRequest):
                    should_exit = str(command.payload.get("command", "")).casefold() in {"exit", "quit"}
                    try:
                        command.response = self._handle_agent_command(command.payload)
                    except Exception as exc:
                        command.response = {"ok": False, "error": str(exc)}
                    finally:
                        command.completed.set()
                    if should_exit:
                        self.shutdown()
                        return
                elif command == "view":
                    self.open_viewer()
                elif command == "export":
                    self.export_data()
                elif command == "reset":
                    self.reset_data()
                elif command == "exit":
                    self.shutdown()
                    return
                elif command.startswith("tray_error:"):
                    messagebox.showerror("Activity Tracker", command.removeprefix("tray_error:"), parent=self.root)
                    self.shutdown()
                    return
                elif command.startswith("sample_error:"):
                    # Sampling failures are transient and should not interrupt
                    # normal use with repeated dialog boxes.
                    pass
        except queue.Empty:
            pass
        self.root.after(500, self._process_commands)

    def _dispatch_agent_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("command", "")).casefold() == "ping":
            return {"ok": True, "command": "ping", "running": True}
        request = _AgentRequest(payload)
        self.commands.put(request)
        if not request.completed.wait(30):
            return {"ok": False, "error": "The tracker UI did not process the command in time."}
        return request.response or {"ok": False, "error": "The tracker returned no response."}

    def _handle_agent_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = str(payload.get("command", "")).casefold()
        if command == "status":
            snapshot = self.tracker.export_data()
            programs = [program for day in snapshot["days"] for program in day["programs"]]
            return {
                "ok": True,
                "command": "status",
                "running": True,
                "tracking_started_at": snapshot["tracking_started_at"],
                "poll_interval_seconds": snapshot["poll_interval_seconds"],
                "idle_threshold_seconds": snapshot["idle_threshold_seconds"],
                "day_count": len(snapshot["days"]),
                "program_count": len(programs),
                "tracked_seconds": round(sum(item["seconds"] for item in programs), 3),
            }
        if command in {"show", "view"}:
            self.open_viewer()
            return {"ok": True, "command": "show", "popup_requested": True}
        if command == "reset":
            if not payload.get("confirmed"):
                return {
                    "ok": False,
                    "command": "reset",
                    "confirmation_required": True,
                    "message": "Use reset --yes to clear all in-memory tracked data.",
                }
            self.tracker.reset()
            return {"ok": True, "command": "reset", "cleared": True}
        if command == "export":
            argument = str(payload.get("path", "")).strip()
            if not argument:
                return {"ok": False, "command": "export", "error": "Usage: export <path>"}
            path = Path(argument).expanduser()
            if path.exists() and not payload.get("overwrite"):
                return {
                    "ok": False,
                    "command": "export",
                    "confirmation_required": True,
                    "path": str(path),
                    "message": "Use export <path> --overwrite to replace an existing file.",
                }
            try:
                snapshot = self.tracker.export_data()
                with path.open("w", encoding="utf-8", newline="\n") as output_file:
                    json.dump(snapshot, output_file, ensure_ascii=False, indent=2)
                    output_file.write("\n")
            except (OSError, TypeError, ValueError) as exc:
                return {"ok": False, "command": "export", "error": str(exc)}
            return {"ok": True, "command": "export", "path": str(path), "days": len(snapshot["days"])}
        if command in {"exit", "quit"}:
            return {"ok": True, "command": "exit", "message": "Unexported data will be lost."}
        return {"ok": False, "command": command, "error": "Unknown command. Use help."}

    def open_viewer(self) -> None:
        if self.viewer is not None and self.viewer.winfo_exists():
            self.refresh_viewer()
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
        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        horizontal_scrollbar = tk.Scrollbar(text_frame, orient=tk.HORIZONTAL)
        horizontal_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)
        text = tk.Text(
            text_frame,
            wrap=tk.NONE,
            font=("Consolas", 10),
            yscrollcommand=scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
            undo=False,
        )
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=text.yview)
        horizontal_scrollbar.config(command=text.xview)
        text.configure(state=tk.DISABLED)
        self.viewer_text = text

        buttons = tk.Frame(frame, pady=8)
        buttons.pack(fill=tk.X)
        tk.Button(buttons, text="Copy JSON", command=self.copy_json).pack(side=tk.LEFT)
        tk.Button(buttons, text="Refresh", command=self.refresh_viewer).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(buttons, text="Export JSON", command=self.export_data).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(buttons, text="Exit", command=self.shutdown).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(buttons, text="Close", command=self.close_viewer).pack(side=tk.RIGHT)
        self.refresh_viewer()

    def _json_snapshot(self) -> str:
        return json.dumps(self.tracker.export_data(), ensure_ascii=False, indent=2) + "\n"

    def refresh_viewer(self) -> None:
        if self.viewer_text is None or not self.viewer_text.winfo_exists():
            return
        payload = self._json_snapshot()
        self.viewer_text.configure(state=tk.NORMAL)
        self.viewer_text.delete("1.0", tk.END)
        self.viewer_text.insert("1.0", payload)
        self.viewer_text.configure(state=tk.DISABLED)

    def copy_json(self) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._json_snapshot())
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

    def export_data(self) -> None:
        suggested = self.tracker.export_data()["exported_at"]
        stamp = f"{suggested[:10]}_{suggested[11:19].replace(':', '')}"
        filename = f"activity_{stamp}.json"
        dialog_parent = self.viewer if self.viewer is not None else self.root
        path = filedialog.asksaveasfilename(
            parent=dialog_parent,
            title="Export Tracked Data",
            initialdir=str(Path.home()),
            initialfile=filename,
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            confirmoverwrite=True,
        )
        if not path:
            return
        try:
            payload = self.tracker.export_data()
            with open(path, "w", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")
        except (OSError, TypeError, ValueError) as exc:
            messagebox.showerror("Export Tracked Data", f"Could not export JSON:\n{exc}", parent=dialog_parent)
            return
        messagebox.showinfo("Export Tracked Data", f"Activity data exported to:\n{path}", parent=dialog_parent)

    def reset_data(self) -> None:
        if messagebox.askyesno(
            "Reset Tracked Data",
            "Clear all accumulated activity data? This cannot be undone.",
            parent=self.root,
        ):
            self.tracker.reset()

    def shutdown(self) -> None:
        if not self.running:
            return
        self.running = False
        self.close_viewer()
        try:
            self.icon.stop()
        except Exception:
            pass
        if self.tray_thread and self.tray_thread.is_alive():
            self.tray_thread.join(timeout=3)
        self.agent_server.close()
        release_single_instance_mutex(self.mutex_handle)
        self.mutex_handle = 0
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _make_icon() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill=(32, 48, 68, 255))
    draw.rounded_rectangle((15, 31, 23, 49), radius=3, fill=(105, 190, 255, 255))
    draw.rounded_rectangle((28, 21, 36, 49), radius=3, fill=(105, 190, 255, 255))
    draw.rounded_rectangle((41, 13, 49, 49), radius=3, fill=(105, 190, 255, 255))
    return image


def _show_startup_error(title: str, message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message, parent=root)
        root.destroy()
    except Exception:
        show_windows_error(title, message)


def main() -> int:
    mutex_handle: int | None = None
    root: tk.Tk | None = None
    application: TrackerApplication | None = None
    launched_at = datetime.now().astimezone()
    try:
        mutex_handle, already_running = acquire_single_instance_mutex()
        if already_running:
            # We opened the existing mutex but did not acquire ownership.
            import ctypes
            from windows_api import kernel32

            kernel32.CloseHandle(ctypes.c_void_p(mutex_handle))
            # A second launcher is a no-op. The process that owns the mutex
            # continues tracking and serving CLI commands.
            return 0

        config = TrackerConfig.load(Path(__file__).resolve().parent)
        root = tk.Tk()
        application = TrackerApplication(root, config, mutex_handle, launched_at)
        application.mutex_handle = mutex_handle
        mutex_handle = None
        root.mainloop()
        return 0
    except ConfigError as exc:
        _show_startup_error("Activity Tracker Configuration", str(exc))
        return 1
    except Exception as exc:
        _show_startup_error("Activity Tracker", f"Could not start the tracker:\n{exc}")
        return 1
    finally:
        if application is not None and application.running:
            application.shutdown()
        elif root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        if mutex_handle:
            release_single_instance_mutex(mutex_handle)


if __name__ == "__main__":
    sys.exit(main())

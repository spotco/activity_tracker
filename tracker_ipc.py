"""Local command channel for agents controlling a running tray tracker."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from typing import Callable


_IDENTITY = "\\".join(
    part.casefold()
    for part in (
        os.environ.get("USERDOMAIN", ""),
        os.environ.get("USERNAME", ""),
        os.environ.get("SESSIONNAME", ""),
    )
)
_IDENTITY_HASH = hashlib.sha256(_IDENTITY.encode("utf-8")).hexdigest()[:16]
_PIPE_ADDRESS = rf"\\.\pipe\WindowsForegroundActivityTracker-{_IDENTITY_HASH}"
_AUTH_KEY = hashlib.sha256(
    ("WindowsForegroundActivityTracker agent channel v1:" + _IDENTITY).encode("utf-8")
).digest()

CommandHandler = Callable[[dict], dict]


class AgentChannelUnavailable(ConnectionError):
    """Raised when no tray tracker is listening for agent commands."""


class AgentCommandServer:
    """Serve JSON commands over a per-user Windows named pipe."""

    def __init__(self, handler: CommandHandler) -> None:
        self.handler = handler
        self.listener: Listener | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        self.listener = Listener(_PIPE_ADDRESS, family="AF_PIPE", authkey=_AUTH_KEY)
        self.thread = threading.Thread(target=self._serve, name="agent-command-server", daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        listener = self.listener
        if listener is None:
            return
        while not self.stop_event.is_set():
            try:
                connection = listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                if self.stop_event.is_set():
                    return
                continue
            try:
                with connection:
                    try:
                        request = json.loads(connection.recv_bytes(64 * 1024).decode("utf-8"))
                        if not isinstance(request, dict):
                            raise ValueError("The command must be a JSON object.")
                        response = self.handler(request)
                    except Exception as exc:
                        response = {"ok": False, "error": str(exc)}
                    connection.send_bytes(json.dumps(response, ensure_ascii=False).encode("utf-8"))
            except (OSError, EOFError):
                # A client can disconnect while a command is being handled.
                continue

    def close(self) -> None:
        self.stop_event.set()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1)


def send_agent_command(request: dict, *, timeout_seconds: float = 30.0) -> dict:
    """Send one command to the running tracker or raise if it is unavailable."""
    try:
        connection = Client(_PIPE_ADDRESS, family="AF_PIPE", authkey=_AUTH_KEY)
        with connection:
            connection.send_bytes(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            if not connection.poll(timeout_seconds):
                raise TimeoutError("The tracker did not respond to the command in time.")
            response = json.loads(connection.recv_bytes(1024 * 1024).decode("utf-8"))
    except TimeoutError:
        raise
    except (AuthenticationError, OSError, EOFError) as exc:
        raise AgentChannelUnavailable("The tray tracker is not running.") from exc
    if not isinstance(response, dict):
        raise ConnectionError("The tracker returned an invalid response.")
    return response

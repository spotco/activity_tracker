#!/usr/bin/env python3
"""Chromium native messaging host: extension stdin/stdout to tracker named pipe."""

from __future__ import annotations

import json
import os
import struct
import sys
from typing import Any

from tracker_ipc import AgentChannelUnavailable, send_agent_command


_MAX_MESSAGE_BYTES = 64 * 1024


def configure_binary_stdio() -> None:
    """Use binary mode on Windows so native message bytes are not altered."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def read_framed_message(input_stream: Any = sys.stdin.buffer) -> dict[str, Any] | None:
    header = input_stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("Incomplete native message length header.")
    (length,) = struct.unpack("<I", header)
    if length <= 0 or length > _MAX_MESSAGE_BYTES:
        raise ValueError("Native message length out of bounds.")
    payload = input_stream.read(length)
    if len(payload) != length:
        raise ValueError("Incomplete native message body.")
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("Native message must be a JSON object.")
    return message


def write_framed_message(message: dict[str, Any], output_stream: Any = sys.stdout.buffer) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_MESSAGE_BYTES:
        raise ValueError("Native response exceeds maximum size.")
    output_stream.write(struct.pack("<I", len(encoded)))
    output_stream.write(encoded)
    output_stream.flush()


def validate_extension_message(message: dict[str, Any]) -> dict[str, Any]:
    command = str(message.get("type", message.get("command", ""))).casefold()
    if command == "ping":
        return {"command": "ping"}
    if command == "status":
        return {"command": "status"}
    if command != "browser_state":
        raise ValueError("Unsupported native message type.")
    browser = message.get("browser")
    if not isinstance(browser, str) or not browser.strip():
        raise ValueError("browser must be a non-empty string.")
    return {
        "command": "browser_state",
        "browser": browser.strip(),
        "focused": message.get("focused"),
        "hostname": message.get("hostname"),
        "generation": message.get("generation"),
        "session_epoch": message.get("session_epoch"),
        "sequence": message.get("sequence"),
    }


def forward_to_tracker(request: dict[str, Any], *, timeout_seconds: float = 2.0) -> dict[str, Any]:
    try:
        return send_agent_command(request, timeout_seconds=timeout_seconds)
    except AgentChannelUnavailable:
        return {"ok": False, "error": "tracker_unavailable"}
    except TimeoutError:
        return {"ok": False, "error": "tracker_timeout"}


def run_host_loop(
    *,
    input_stream: Any = sys.stdin.buffer,
    output_stream: Any = sys.stdout.buffer,
) -> int:
    while True:
        try:
            message = read_framed_message(input_stream)
        except (ValueError, json.JSONDecodeError, UnicodeError) as exc:
            print(f"native host read error: {exc}", file=sys.stderr)
            return 1
        if message is None:
            return 0
        try:
            request = validate_extension_message(message)
            command = str(request.get("command", "")).casefold()
            if command == "ping":
                write_framed_message({"ok": True, "type": "pong"}, output_stream)
                continue
            if command == "status":
                tracker_response = forward_to_tracker({"command": "ping"})
                write_framed_message(
                    {"ok": tracker_response.get("ok", False), "tracker": tracker_response},
                    output_stream,
                )
                continue
            tracker_response = forward_to_tracker(request)
            write_framed_message(
                {
                    "ok": tracker_response.get("ok", False),
                    "tracker": tracker_response,
                },
                output_stream,
            )
        except ValueError as exc:
            write_framed_message({"ok": False, "error": str(exc)}, output_stream)
        except Exception as exc:
            print(f"native host error: {exc}", file=sys.stderr)
            write_framed_message({"ok": False, "error": "internal_error"}, output_stream)


def main() -> int:
    log_path = os.path.join(os.environ.get("LOCALAPPDATA", "."), "ActivityTracker", "native_host.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write("python host start\n")
    except OSError:
        pass
    try:
        configure_binary_stdio()
        return run_host_loop()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

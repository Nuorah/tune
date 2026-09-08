"""Small JSON protocol shared by the CLI and daemon."""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import Any


MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class ProtocolError(RuntimeError):
    """Raised when a peer sends an invalid protocol message."""


def send_message(sock: socket.socket, message: Mapping[str, Any]) -> None:
    """Send one JSON object terminated by a newline."""
    try:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProtocolError("message is not JSON-serializable") from error

    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message is too large")

    sock.sendall(encoded + b"\n")


def receive_message(sock: socket.socket) -> dict[str, Any]:
    """Receive one newline-terminated JSON object."""
    data = bytearray()

    while True:
        chunk = sock.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(data)))
        if not chunk:
            raise ProtocolError("connection closed before a complete message arrived")

        newline = chunk.find(b"\n")
        if newline >= 0:
            data.extend(chunk[:newline])
            if len(data) > MAX_MESSAGE_BYTES:
                raise ProtocolError("message is too large")
            break

        data.extend(chunk)
        if len(data) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message is too large")

    if not data:
        raise ProtocolError("message is empty")

    try:
        message = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("message is not valid JSON") from error

    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")

    return message

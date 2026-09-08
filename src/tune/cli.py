"""Command-line interface for tune."""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tune.protocol import ProtocolError, receive_message, send_message
from tune.runtime import RuntimeSecurityError, control_socket_path


DAEMON_START_TIMEOUT = 3.0


class CLIError(RuntimeError):
    """An error that is safe to show to the user."""


def _send_request(request: dict[str, Any]) -> dict[str, Any]:
    path = control_socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        send_message(connection, request)
        response = receive_message(connection)

    if not isinstance(response.get("ok"), bool):
        raise CLIError("daemon returned an invalid response")
    return response


def _spawn_daemon() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "tune.daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def _remove_stale_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return

    if not stat.S_ISSOCK(info.st_mode):
        raise CLIError(f"refusing to remove non-socket runtime path: {path}")
    if info.st_uid != os.getuid():
        raise CLIError(f"refusing to remove socket owned by another user: {path}")
    path.unlink()


def _ensure_daemon() -> bool:
    """Ensure the daemon is running; return whether this call started it."""
    path = control_socket_path()
    try:
        _send_request({"command": "status"})
        return False
    except (FileNotFoundError, ConnectionRefusedError):
        _remove_stale_socket(path)
    except TimeoutError:
        raise CLIError("tune daemon is not responding") from None
    except OSError as error:
        raise CLIError(f"could not contact tune daemon: {error.strerror or 'socket error'}") from None

    process = _spawn_daemon()
    deadline = time.monotonic() + DAEMON_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            # Another concurrently started daemon may have won the bind race.
            try:
                _send_request({"command": "status"})
                return False
            except (OSError, ProtocolError, CLIError):
                raise CLIError("tune daemon failed to start") from None

        try:
            _send_request({"command": "status"})
            return True
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.05)
        except TimeoutError:
            raise CLIError("new tune daemon is not responding") from None

    process.terminate()
    raise CLIError("timed out while starting tune daemon")


def _require_success(response: dict[str, Any]) -> dict[str, Any]:
    if response["ok"]:
        return response
    error = response.get("error")
    if not isinstance(error, str) or not error:
        error = "daemon rejected the request"
    raise CLIError(_terminal_safe(error))


def _terminal_safe(value: str) -> str:
    return "".join(character for character in value if character >= " " and character != "\x7f")


def _prompt_credentials(username_argument: str | None) -> tuple[str, str]:
    if not sys.stdin.isatty():
        raise CLIError("connect requires an interactive terminal")

    try:
        username = input("Username: ") if username_argument is None else username_argument
        password = getpass.getpass("Password: ")
    except (EOFError, OSError):
        raise CLIError("could not read credentials from terminal") from None

    if not username.strip():
        raise CLIError("username must not be empty")
    if not password:
        raise CLIError("password must not be empty")
    return username, password


def _command_connect(args: argparse.Namespace) -> int:
    username, password = _prompt_credentials(args.username)
    started_daemon = False
    try:
        started_daemon = _ensure_daemon()
        response = _require_success(
            _send_request(
                {
                    "command": "connect",
                    "url": args.url,
                    "username": username,
                    "password": password,
                }
            )
        )
    except Exception:
        if started_daemon:
            try:
                _send_request({"command": "disconnect"})
            except Exception:
                pass
        raise
    finally:
        password = ""

    print(f"Connected to {response['server']} as {response['username']}")
    return 0


def _command_status(_args: argparse.Namespace) -> int:
    try:
        response = _require_success(_send_request({"command": "status"}))
    except (FileNotFoundError, ConnectionRefusedError):
        print("Not connected")
        return 0

    if response.get("connected") is True:
        print(f"Connected to {response['server']} as {response['username']}")
    else:
        print("Not connected")
    return 0


def _command_disconnect(_args: argparse.Namespace) -> int:
    try:
        _require_success(_send_request({"command": "disconnect"}))
    except (FileNotFoundError, ConnectionRefusedError):
        print("Not connected")
        return 0

    print("Disconnected")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tune",
        description="Session-based command-line client for Subsonic servers",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="connect to a Subsonic server")
    connect.add_argument("url", help="HTTPS base URL of the server")
    connect.add_argument("-u", "--username", help="username (prompted when omitted)")
    connect.set_defaults(handler=_command_connect)

    status = subparsers.add_parser("status", help="show connection status")
    status.set_defaults(handler=_command_status)

    disconnect = subparsers.add_parser("disconnect", help="disconnect and stop the daemon")
    disconnect.set_defaults(handler=_command_disconnect)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (CLIError, ProtocolError, RuntimeSecurityError) as error:
        print(f"tune: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ntune: cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

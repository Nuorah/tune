"""Command-line interface for tune."""

from __future__ import annotations

import argparse
import getpass
import json
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

    if response.get("connected") is not True:
        print("Not connected")
        return 0

    server = response.get("server")
    username = response.get("username")
    if not isinstance(server, str) or not isinstance(username, str):
        raise CLIError("daemon returned invalid connection metadata")
    print(f"Connected to {_terminal_safe(server)} as {_terminal_safe(username)}")
    _display_playback_status(response.get("playback"))
    return 0


def _display_playback_status(playback: Any) -> None:
    if not isinstance(playback, dict):
        raise CLIError("daemon returned an invalid playback status")
    state = playback.get("state")
    if state == "stopped":
        print("Playback stopped")
        return
    if state not in {"playing", "paused"}:
        raise CLIError("daemon returned an invalid playback state")

    artist = playback.get("artist")
    album = playback.get("album")
    title = playback.get("title")
    number = playback.get("number")
    total = playback.get("total")
    if (
        not isinstance(artist, str)
        or not isinstance(album, str)
        or not isinstance(title, str)
        or not isinstance(number, int)
        or isinstance(number, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
    ):
        raise CLIError("daemon returned invalid current-track metadata")

    timing = _format_timing(playback.get("position"), playback.get("duration"))
    label = "Paused" if state == "paused" else "Playing"
    print(
        f"{label} {number}/{total}: {_terminal_safe(artist)} — "
        f"{_terminal_safe(title)} ({_terminal_safe(album)}){timing}"
    )


def _format_timing(position: Any, duration: Any) -> str:
    values = (position, duration)
    if any(
        value is not None
        and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0)
        for value in values
    ):
        raise CLIError("daemon returned invalid playback timing")
    if position is None and duration is None:
        return ""
    left = _format_seconds(position) if position is not None else "--:--"
    right = _format_seconds(duration) if duration is not None else "--:--"
    return f" [{left} / {right}]"


def _format_seconds(value: int | float) -> str:
    seconds = int(value)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def _command_waybar(args: argparse.Namespace) -> int:
    watch = getattr(args, "watch", False)
    interval = getattr(args, "interval", 0.25)
    while True:
        payload = _read_waybar_payload()
        try:
            print(json.dumps(payload, ensure_ascii=False), flush=watch)
        except BrokenPipeError:
            return 0
        if not watch:
            return 0
        time.sleep(interval)


def _read_waybar_payload() -> dict[str, Any]:
    try:
        response = _require_success(_send_request({"command": "status"}))
        return _waybar_payload(response)
    except (CLIError, ProtocolError, RuntimeSecurityError, TimeoutError, OSError) as error:
        detail = _terminal_safe(str(error)) or "Tune is unavailable"
        return {
            "text": "Tune unavailable",
            "tooltip": detail,
            "class": "disconnected",
        }


def _waybar_payload(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("connected") is not True:
        return {
            "text": "Tune disconnected",
            "tooltip": "Tune is not connected",
            "class": "disconnected",
        }

    playback = response.get("playback")
    if not isinstance(playback, dict):
        raise CLIError("daemon returned an invalid playback status")
    state = playback.get("state")
    if state == "stopped":
        return {
            "text": "Playback stopped",
            "tooltip": "Tune is connected",
            "class": "stopped",
        }
    if state not in {"playing", "paused"}:
        raise CLIError("daemon returned an invalid playback state")

    artist = playback.get("artist")
    album = playback.get("album")
    title = playback.get("title")
    number = playback.get("number")
    total = playback.get("total")
    position = playback.get("position")
    duration = playback.get("duration")
    if (
        not isinstance(artist, str)
        or not isinstance(album, str)
        or not isinstance(title, str)
        or not isinstance(number, int)
        or isinstance(number, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
    ):
        raise CLIError("daemon returned invalid current-track metadata")

    timing = _format_timing(position, duration)
    label = "Paused: " if state == "paused" else ""
    payload: dict[str, Any] = {
        "text": (
            f"{label}{_terminal_safe(artist)} — {_terminal_safe(title)}{timing}"
        ),
        "tooltip": (
            f"{_terminal_safe(album)}\nTrack {number} of {total}"
        ),
        "class": state,
    }
    if (
        isinstance(position, (int, float))
        and not isinstance(position, bool)
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration > 0
    ):
        payload["percentage"] = max(0, min(100, round(position / duration * 100)))
    return payload


def _command_albums(args: argparse.Namespace) -> int:
    response = _require_success(
        _send_request(
            {
                "command": "albums",
                "order": "newest" if args.newest else "alphabeticalByName",
                "limit": args.limit,
                "offset": args.offset,
            }
        )
    )
    return _display_albums(response)


def _command_search(args: argparse.Namespace) -> int:
    response = _require_success(
        _send_request(
            {
                "command": "search",
                "query": " ".join(args.query),
                "limit": args.limit,
                "offset": args.offset,
            }
        )
    )
    return _display_albums(response)


def _display_albums(response: dict[str, Any]) -> int:
    albums = response.get("albums")
    if not isinstance(albums, list):
        raise CLIError("daemon returned an invalid album list")
    if not albums:
        print("No albums found")
        return 0

    number_width = len(str(len(albums)))
    for album in albums:
        if not isinstance(album, dict):
            raise CLIError("daemon returned an invalid album entry")
        number = album.get("number")
        name = album.get("name")
        artist = album.get("artist")
        if not isinstance(number, int) or not isinstance(name, str) or not isinstance(artist, str):
            raise CLIError("daemon returned an album with missing metadata")

        details: list[str] = []
        year = album.get("year")
        song_count = album.get("song_count")
        if isinstance(year, int) and not isinstance(year, bool):
            details.append(str(year))
        if isinstance(song_count, int) and not isinstance(song_count, bool):
            noun = "track" if song_count == 1 else "tracks"
            details.append(f"{song_count} {noun}")
        suffix = f" ({', '.join(details)})" if details else ""
        print(
            f"{number:>{number_width}}  "
            f"{_terminal_safe(artist)} — {_terminal_safe(name)}{suffix}"
        )
    return 0


def _command_play(args: argparse.Namespace) -> int:
    response = _require_success(
        _send_request({"command": "play", "number": args.number})
    )
    album = response.get("album")
    artist = response.get("artist")
    song_count = response.get("song_count")
    first_song = response.get("first_song")
    if (
        not isinstance(album, str)
        or not isinstance(artist, str)
        or not isinstance(song_count, int)
        or isinstance(song_count, bool)
        or not isinstance(first_song, str)
    ):
        raise CLIError("daemon returned invalid playback metadata")

    noun = "track" if song_count == 1 else "tracks"
    print(
        f"Playing {_terminal_safe(artist)} — {_terminal_safe(album)} "
        f"({song_count} {noun}); first: {_terminal_safe(first_song)}"
    )
    return 0


def _command_playback_control(args: argparse.Namespace) -> int:
    response = _require_success(_send_request({"command": args.action}))
    if response.get("action") != args.action:
        raise CLIError("daemon returned an invalid playback response")
    print(args.confirmation)
    return 0


def _command_disconnect(_args: argparse.Namespace) -> int:
    try:
        _require_success(_send_request({"command": "disconnect"}))
    except (FileNotFoundError, ConnectionRefusedError):
        print("Not connected")
        return 0

    print("Disconnected")
    return 0


def _waybar_interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not 0.1 <= parsed <= 60:
        raise argparse.ArgumentTypeError("must be between 0.1 and 60 seconds")
    return parsed


def _album_number(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _album_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if not 1 <= parsed <= 500:
        raise argparse.ArgumentTypeError("must be between 1 and 500")
    return parsed


def _album_offset(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


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

    waybar = subparsers.add_parser("waybar", help="print Waybar-compatible JSON status")
    waybar.add_argument("--watch", action="store_true", help="continuously print status updates")
    waybar.add_argument(
        "--interval",
        type=_waybar_interval,
        default=0.25,
        metavar="SECONDS",
        help="watch update interval (default: 0.25)",
    )
    waybar.set_defaults(handler=_command_waybar)

    albums = subparsers.add_parser("albums", help="list albums and cache numbered results")
    albums.add_argument("--newest", action="store_true", help="order newest first")
    albums.add_argument("--limit", type=_album_limit, default=100, help="number to show (1-500)")
    albums.add_argument("--offset", type=_album_offset, default=0, help="number to skip")
    albums.set_defaults(handler=_command_albums)

    search = subparsers.add_parser("search", help="search albums and cache numbered results")
    search.add_argument("query", nargs="+", help="album search query")
    search.add_argument("--limit", type=_album_limit, default=100, help="number to show (1-500)")
    search.add_argument("--offset", type=_album_offset, default=0, help="number to skip")
    search.set_defaults(handler=_command_search)

    play = subparsers.add_parser("play", help="play an album from the last results")
    play.add_argument("number", type=_album_number, help="album result number")
    play.set_defaults(handler=_command_play)

    playback_commands = {
        "pause": ("pause playback", "Paused"),
        "resume": ("resume playback", "Resumed"),
        "toggle": ("toggle pause or resume", "Toggled pause"),
        "next": ("skip to the next track", "Skipped to next track"),
        "previous": ("return to the previous track", "Returned to previous track"),
        "stop": ("stop playback", "Stopped"),
    }
    for command, (help_text, confirmation) in playback_commands.items():
        control = subparsers.add_parser(command, help=help_text)
        control.set_defaults(
            handler=_command_playback_control,
            action=command,
            confirmation=confirmation,
        )

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
    except TimeoutError:
        print("tune: daemon is not responding", file=sys.stderr)
        return 1
    except OSError as error:
        detail = error.strerror or "socket error"
        print(f"tune: could not contact daemon: {detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ntune: cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

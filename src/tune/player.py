"""Playback support built around a private loopback audio proxy and mpv."""

from __future__ import annotations

import os
import secrets
import socket
import stat
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tune.protocol import ProtocolError, receive_message, send_message
from tune.runtime import player_socket_path
from tune.subsonic import AudioStream, SubsonicClient, SubsonicError


COPY_BUFFER_SIZE = 64 * 1024
MPV_START_TIMEOUT = 3.0
MPV_COMMAND_TIMEOUT = 2.0
DEFAULT_VOLUME = 50


class PlayerError(RuntimeError):
    """A safe-to-display playback error."""


@dataclass(frozen=True, slots=True)
class PlaybackStatus:
    active: bool
    paused: bool
    playlist_index: int | None
    position: float | None
    duration: float | None


class _ProxyServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


class StreamProxy:
    """Expose authenticated Subsonic streams through opaque loopback URLs."""

    def __init__(self, client: SubsonicClient) -> None:
        self._token = secrets.token_urlsafe(32)
        handler = _handler_for(client, self._token)
        self._server = _ProxyServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tune-stream-proxy",
            daemon=True,
        )
        self._closed = False
        self._thread.start()

    def url_for(self, song_id: str) -> str:
        if self._closed:
            raise RuntimeError("stream proxy is closed")
        encoded_id = urllib.parse.quote(song_id, safe="")
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/{self._token}/stream/{encoded_id}"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self) -> StreamProxy:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class MpvPlayer:
    """Own and control one isolated, headless mpv process."""

    def __init__(self, executable: str = "mpv", volume: int = DEFAULT_VOLUME) -> None:
        self._executable = executable
        self._process: subprocess.Popen[bytes] | None = None
        self._socket_path = player_socket_path()
        self._volume = _validate_volume(volume)

    def start(self, urls: list[str]) -> None:
        if not urls:
            raise PlayerError("album contains no playable songs")
        if not all(_is_loopback_stream_url(url) for url in urls):
            raise PlayerError("refusing to give mpv a non-loopback stream URL")

        self.close()
        _remove_stale_player_socket(self._socket_path)
        arguments = [
            self._executable,
            "--no-video",
            "--audio-display=no",
            "--force-window=no",
            "--no-terminal",
            "--really-quiet",
            "--idle=yes",
            "--gapless-audio=yes",
            f"--volume={self._volume}",
            f"--input-ipc-server={self._socket_path}",
            "--",
            *urls,
        ]
        try:
            self._process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            self._process = None
            raise PlayerError("mpv is not installed or not in PATH") from None
        except OSError as error:
            self._process = None
            detail = error.strerror or "process error"
            raise PlayerError(f"could not start mpv: {detail}") from None

        deadline = time.monotonic() + MPV_START_TIMEOUT
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self.close()
                raise PlayerError("mpv exited during startup")
            if self._socket_path.exists():
                try:
                    self._command(["get_property", "idle-active"])
                    return
                except PlayerError:
                    pass
            time.sleep(0.05)

        self.close()
        raise PlayerError("timed out while starting mpv")

    def set_volume(self, volume: int) -> None:
        volume = _validate_volume(volume)
        if self._process is not None and self._process.poll() is None:
            self._command(["set_property", "volume", volume])
        self._volume = volume

    def pause(self) -> None:
        self._command(["set_property", "pause", True])

    def resume(self) -> None:
        self._command(["set_property", "pause", False])

    def toggle_pause(self) -> None:
        self._command(["cycle", "pause"])

    def next_track(self) -> None:
        self._command(["playlist-next", "force"])

    def previous_track(self) -> None:
        self._command(["playlist-prev", "force"])

    def stop(self) -> None:
        self._command(["stop"])

    def status(self) -> PlaybackStatus:
        if self._process is None or self._process.poll() is not None:
            return PlaybackStatus(False, False, None, None, None)

        idle = self._command(["get_property", "idle-active"])
        if not isinstance(idle, bool):
            raise PlayerError("mpv returned an invalid playback state")
        if idle:
            return PlaybackStatus(False, False, None, None, None)

        paused = self._command(["get_property", "pause"])
        playlist_index = self._command(["get_property", "playlist-pos"])
        position = self._command(["get_property", "time-pos"])
        duration = self._command(["get_property", "duration"])
        if not isinstance(paused, bool):
            raise PlayerError("mpv returned an invalid pause state")
        if not isinstance(playlist_index, int) or isinstance(playlist_index, bool):
            raise PlayerError("mpv returned an invalid playlist position")
        if position is not None and (
            not isinstance(position, (int, float)) or isinstance(position, bool)
        ):
            raise PlayerError("mpv returned an invalid playback position")
        if duration is not None and (
            not isinstance(duration, (int, float)) or isinstance(duration, bool)
        ):
            raise PlayerError("mpv returned an invalid track duration")

        return PlaybackStatus(
            True,
            paused,
            playlist_index,
            float(position) if position is not None else None,
            float(duration) if duration is not None else None,
        )

    def close(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                self._command(["quit"])
            except PlayerError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        self._process = None
        _remove_stale_player_socket(self._socket_path)

    def _command(self, command: list[Any]) -> Any:
        if self._process is None or self._process.poll() is not None:
            raise PlayerError("nothing is playing")

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(MPV_COMMAND_TIMEOUT)
                connection.connect(str(self._socket_path))
                send_message(connection, {"command": command})
                response = receive_message(connection)
        except (OSError, ProtocolError):
            raise PlayerError("could not communicate with mpv") from None

        if response.get("error") != "success":
            raise PlayerError(f"mpv rejected {command[0]}")
        return response.get("data")


def _validate_volume(volume: int) -> int:
    if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 100:
        raise PlayerError("volume must be an integer between 0 and 100")
    return volume


def _is_loopback_stream_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _remove_stale_player_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PlayerError(f"refusing to remove unsafe mpv socket path: {path}")
    path.unlink()


def _handler_for(client: SubsonicClient, token: str) -> type[BaseHTTPRequestHandler]:
    expected_prefix = f"/{token}/stream/"

    class StreamHandler(BaseHTTPRequestHandler):
        server_version = "tune"
        sys_version = ""

        def do_GET(self) -> None:
            self._serve(include_body=True)

        def do_HEAD(self) -> None:
            self._serve(include_body=False)

        def _serve(self, *, include_body: bool) -> None:
            song_id = self._song_id(expected_prefix)
            if song_id is None:
                self._empty_response(404)
                return

            byte_range = self.headers.get("Range")
            try:
                stream = client.open_stream(song_id, byte_range=byte_range)
            except ValueError:
                self._empty_response(400)
                return
            except SubsonicError:
                self._empty_response(502)
                return
            except Exception:
                self._empty_response(502)
                return

            with stream:
                self.send_response(stream.status)
                self._copy_headers(stream)
                self.send_header("Connection", "close")
                self.end_headers()

                if include_body and stream.status != 416:
                    self._copy_body(stream)

        def _song_id(self, prefix: str) -> str | None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.query or parsed.fragment or not parsed.path.startswith(prefix):
                return None
            encoded_id = parsed.path[len(prefix) :]
            if not encoded_id or "/" in encoded_id or len(encoded_id) > 4096:
                return None
            try:
                song_id = urllib.parse.unquote(encoded_id, errors="strict")
            except UnicodeDecodeError:
                return None
            return song_id or None

        def _copy_headers(self, stream: AudioStream) -> None:
            content_type = _safe_header(stream.content_type)
            content_range = _safe_header(stream.content_range)
            accept_ranges = _safe_header(stream.accept_ranges)
            if content_type is not None:
                self.send_header("Content-Type", content_type)
            if stream.content_length is not None:
                self.send_header("Content-Length", str(stream.content_length))
            if content_range is not None:
                self.send_header("Content-Range", content_range)
            if accept_ranges is not None:
                self.send_header("Accept-Ranges", accept_ranges)

        def _copy_body(self, stream: AudioStream) -> None:
            try:
                while chunk := stream.read(COPY_BUFFER_SIZE):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def _empty_response(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return StreamHandler


def _safe_header(value: str | None) -> str | None:
    if value is None or "\r" in value or "\n" in value:
        return None
    return value

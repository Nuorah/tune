"""The private, session-scoped tune daemon."""

from __future__ import annotations

import os
import signal
import socket
import stat
import struct
from pathlib import Path
from typing import Any

from tune.player import MpvPlayer, PlayerError, StreamProxy
from tune.protocol import ProtocolError, receive_message, send_message
from tune.runtime import RuntimeSecurityError, control_socket_path
from tune.subsonic import Album, Song, SubsonicClient, SubsonicError


class TuneDaemon:
    def __init__(self) -> None:
        self.client: SubsonicClient | None = None
        self.last_albums: list[Album] = []
        self.player: MpvPlayer | None = None
        self.stream_proxy: StreamProxy | None = None
        self.playlist: list[Song] = []
        self.stop_requested = False
        self.listener: socket.socket | None = None
        self.socket_path: Path | None = None
        self.socket_inode: int | None = None

    def run(self) -> None:
        path = control_socket_path()
        if path.exists() or path.is_symlink():
            raise RuntimeError("another tune daemon may already be running")

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_umask = os.umask(0o077)
        try:
            listener.bind(str(path))
        finally:
            os.umask(previous_umask)

        path.chmod(0o600)
        self.listener = listener
        self.socket_path = path
        self.socket_inode = path.stat().st_ino

        listener.listen(8)
        listener.settimeout(0.5)
        self._install_signal_handlers()

        try:
            while not self.stop_requested:
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue

                with connection:
                    connection.settimeout(10)
                    self._serve_connection(connection)
        finally:
            self._cleanup()

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            self._verify_peer(connection)
            request = receive_message(connection)
            response = self._handle_request(request)
        except (ProtocolError, SubsonicError, PlayerError, ValueError) as error:
            response = {"ok": False, "error": str(error)}
        except (ConnectionError, TimeoutError):
            return
        except Exception:
            response = {"ok": False, "error": "internal daemon error"}

        try:
            send_message(connection, response)
        except (ProtocolError, ConnectionError, TimeoutError):
            pass

    def _handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        if command == "connect":
            return self._connect(request)
        if command == "status":
            return self._status()
        if command == "albums":
            return self._albums(request)
        if command == "search":
            return self._search(request)
        if command == "play":
            return self._play(request)
        if command in {"pause", "resume", "next", "previous", "stop"}:
            return self._playback_control(command)
        if command == "disconnect":
            return self._disconnect()
        raise ValueError("unknown command")

    def _connect(self, request: dict[str, Any]) -> dict[str, Any]:
        server_url = request.get("url")
        username = request.get("username")
        password = request.get("password")
        if not all(isinstance(value, str) for value in (server_url, username, password)):
            raise ValueError("connect requires URL, username, and password")

        candidate: SubsonicClient | None = None
        try:
            candidate = SubsonicClient(server_url, username, password)
            candidate.ping()
        except Exception:
            if candidate is not None:
                candidate.wipe_credentials()
            raise
        finally:
            request["password"] = ""
            password = ""

        self._close_playback()
        if self.client is not None:
            self.client.wipe_credentials()
        self.client = candidate
        self.last_albums = []
        return {
            "ok": True,
            "server": candidate.server_url,
            "username": candidate.username,
        }

    def _status(self) -> dict[str, Any]:
        playback = self._playback_status()
        if self.client is None:
            return {"ok": True, "connected": False, "playback": playback}
        return {
            "ok": True,
            "connected": True,
            "server": self.client.server_url,
            "username": self.client.username,
            "playback": playback,
        }

    def _playback_status(self) -> dict[str, Any]:
        if self.player is None:
            return {"state": "stopped"}

        status = self.player.status()
        if not status.active:
            return {"state": "stopped"}
        index = status.playlist_index
        if index is None or not 0 <= index < len(self.playlist):
            raise PlayerError("mpv returned a playlist position outside the current queue")

        song = self.playlist[index]
        return {
            "state": "paused" if status.paused else "playing",
            "artist": song.artist,
            "album": song.album,
            "title": song.title,
            "number": index + 1,
            "total": len(self.playlist),
            "position": status.position,
            "duration": status.duration,
        }

    def _albums(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            raise ValueError("not connected")

        order = request.get("order", "alphabeticalByName")
        limit = request.get("limit", 100)
        offset = request.get("offset", 0)
        if not isinstance(order, str):
            raise ValueError("album order must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("album limit must be an integer")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError("album offset must be an integer")

        albums = self.client.get_albums(order=order, limit=limit, offset=offset)
        return self._cache_album_response(albums)

    def _search(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            raise ValueError("not connected")

        query = request.get("query")
        limit = request.get("limit", 100)
        offset = request.get("offset", 0)
        if not isinstance(query, str):
            raise ValueError("search query must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("album limit must be an integer")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError("album offset must be an integer")

        albums = self.client.search_albums(query, limit=limit, offset=offset)
        return self._cache_album_response(albums)

    def _cache_album_response(self, albums: list[Album]) -> dict[str, Any]:
        self.last_albums = albums
        return {
            "ok": True,
            "albums": [
                {
                    "number": number,
                    "name": album.name,
                    "artist": album.artist,
                    "year": album.year,
                    "song_count": album.song_count,
                }
                for number, album in enumerate(albums, start=1)
            ],
        }

    def _play(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            raise ValueError("not connected")

        number = request.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("album number must be an integer")
        if not 1 <= number <= len(self.last_albums):
            raise ValueError("album number is not in the last results")

        album = self.last_albums[number - 1]
        songs = self.client.get_album(album.id)
        if not songs:
            raise PlayerError("album contains no playable songs")

        created_proxy = False
        if self.stream_proxy is None:
            try:
                self.stream_proxy = StreamProxy(self.client)
            except OSError:
                raise PlayerError("could not start local stream proxy") from None
            created_proxy = True
        if self.player is None:
            self.player = MpvPlayer()

        urls = [self.stream_proxy.url_for(song.id) for song in songs]
        try:
            self.player.start(urls)
        except Exception:
            if created_proxy:
                self.stream_proxy.close()
                self.stream_proxy = None
            raise

        self.playlist = songs
        first = songs[0]
        return {
            "ok": True,
            "album": album.name,
            "artist": album.artist,
            "song_count": len(songs),
            "first_song": first.title,
        }

    def _playback_control(self, command: str) -> dict[str, Any]:
        if self.player is None:
            raise PlayerError("nothing is playing")

        if command == "pause":
            self.player.pause()
        elif command == "resume":
            self.player.resume()
        elif command == "next":
            self.player.next_track()
        elif command == "previous":
            self.player.previous_track()
        elif command == "stop":
            self.player.stop()
        else:
            raise ValueError("unknown playback command")
        return {"ok": True, "action": command}

    def _close_playback(self) -> None:
        if self.player is not None:
            try:
                self.player.close()
            except Exception:
                pass
            self.player = None
        if self.stream_proxy is not None:
            try:
                self.stream_proxy.close()
            except Exception:
                pass
            self.stream_proxy = None
        self.playlist = []

    def _disconnect(self) -> dict[str, Any]:
        self._close_playback()
        if self.client is not None:
            self.client.wipe_credentials()
            self.client = None
        self.last_albums = []
        self.stop_requested = True
        return {"ok": True}

    @staticmethod
    def _verify_peer(connection: socket.socket) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            return
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", credentials)
        if uid != os.getuid():
            raise ProtocolError("connection is owned by another user")

    def _install_signal_handlers(self) -> None:
        def request_stop(_signum: int, _frame: Any) -> None:
            self.stop_requested = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGHUP, request_stop)

    def _cleanup(self) -> None:
        self._close_playback()
        if self.client is not None:
            self.client.wipe_credentials()
            self.client = None

        if self.listener is not None:
            self.listener.close()
            self.listener = None

        path = self.socket_path
        if path is not None and self.socket_inode is not None:
            try:
                info = path.lstat()
                if stat.S_ISSOCK(info.st_mode) and info.st_ino == self.socket_inode:
                    path.unlink()
            except FileNotFoundError:
                pass

        if path is not None:
            try:
                path.parent.rmdir()
            except OSError:
                pass


def main() -> int:
    try:
        TuneDaemon().run()
    except (RuntimeError, RuntimeSecurityError) as error:
        print(f"tuned: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

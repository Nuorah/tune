"""The private, session-scoped tune daemon."""

from __future__ import annotations

import os
import signal
import socket
import stat
import struct
from pathlib import Path
from typing import Any

from tune.protocol import ProtocolError, receive_message, send_message
from tune.runtime import RuntimeSecurityError, control_socket_path
from tune.subsonic import SubsonicClient, SubsonicError


class TuneDaemon:
    def __init__(self) -> None:
        self.client: SubsonicClient | None = None
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
        except (ProtocolError, SubsonicError, ValueError) as error:
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

        if self.client is not None:
            self.client.wipe_credentials()
        self.client = candidate
        return {
            "ok": True,
            "server": candidate.server_url,
            "username": candidate.username,
        }

    def _status(self) -> dict[str, Any]:
        if self.client is None:
            return {"ok": True, "connected": False}
        return {
            "ok": True,
            "connected": True,
            "server": self.client.server_url,
            "username": self.client.username,
        }

    def _disconnect(self) -> dict[str, Any]:
        if self.client is not None:
            self.client.wipe_credentials()
            self.client = None
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

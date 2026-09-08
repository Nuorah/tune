from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import tune.cli as cli
from tune.daemon import TuneDaemon
from tune.protocol import ProtocolError, receive_message, send_message
from tune.runtime import control_socket_path, runtime_dir
from tune.subsonic import Album, SubsonicClient, SubsonicError


class RuntimeTests(unittest.TestCase):
    def test_runtime_directory_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": temporary_directory}):
                path = runtime_dir()
                self.assertEqual(path, Path(temporary_directory) / "tune")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(control_socket_path(), path / "control.sock")


class ProtocolTests(unittest.TestCase):
    def test_message_round_trip(self) -> None:
        first, second = socket.socketpair()
        with first, second:
            send_message(first, {"command": "status", "value": "héllo"})
            self.assertEqual(
                receive_message(second),
                {"command": "status", "value": "héllo"},
            )

    def test_invalid_message_does_not_echo_payload(self) -> None:
        first, second = socket.socketpair()
        secret = "not-json-secret"
        with first, second:
            first.sendall(secret.encode() + b"\n")
            with self.assertRaises(ProtocolError) as raised:
                receive_message(second)
        self.assertNotIn(secret, str(raised.exception))


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers: dict[str, str] = {}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class _FakeOpener:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self.response = response
        self.request_url = ""

    def open(self, request: object, timeout: int) -> _FakeResponse:
        self.request_url = request.full_url  # type: ignore[attr-defined]
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class SubsonicTests(unittest.TestCase):
    def test_ping_uses_salted_token_without_exposing_password(self) -> None:
        body = json.dumps({"subsonic-response": {"status": "ok"}}).encode()
        opener = _FakeOpener(_FakeResponse(body))
        client = SubsonicClient("https://music.example/base/", "alice", "swordfish")
        client._opener = opener  # type: ignore[assignment]

        with mock.patch("tune.subsonic.secrets.token_hex", return_value="abcd1234"):
            client.ping()

        parsed = urllib.parse.urlsplit(opener.request_url)
        query = urllib.parse.parse_qs(parsed.query)
        expected = hashlib.md5(
            b"swordfishabcd1234", usedforsecurity=False
        ).hexdigest()
        self.assertEqual(parsed.path, "/base/rest/ping.view")
        self.assertEqual(query["u"], ["alice"])
        self.assertEqual(query["s"], ["abcd1234"])
        self.assertEqual(query["t"], [expected])
        self.assertNotIn("swordfish", opener.request_url)

    def test_get_albums_parses_metadata_and_pagination(self) -> None:
        body = json.dumps(
            {
                "subsonic-response": {
                    "status": "ok",
                    "albumList2": {
                        "album": [
                            {
                                "id": "album-secret-id",
                                "name": "Kid A",
                                "artist": "Radiohead",
                                "year": 2000,
                                "songCount": 10,
                            }
                        ]
                    },
                }
            }
        ).encode()
        opener = _FakeOpener(_FakeResponse(body))
        client = SubsonicClient("https://music.example", "alice", "swordfish")
        client._opener = opener  # type: ignore[assignment]

        albums = client.get_albums(order="newest", limit=25, offset=50)

        self.assertEqual(
            albums,
            [Album("album-secret-id", "Kid A", "Radiohead", 2000, 10)],
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.request_url).query)
        self.assertEqual(query["type"], ["newest"])
        self.assertEqual(query["size"], ["25"])
        self.assertEqual(query["offset"], ["50"])

    def test_search_albums_uses_server_side_search(self) -> None:
        body = json.dumps(
            {
                "subsonic-response": {
                    "status": "ok",
                    "searchResult3": {
                        "album": [
                            {
                                "id": "search-album-id",
                                "name": "Amnesiac",
                                "artist": "Radiohead",
                                "year": 2001,
                                "songCount": 11,
                            }
                        ]
                    },
                }
            }
        ).encode()
        opener = _FakeOpener(_FakeResponse(body))
        client = SubsonicClient("https://music.example", "alice", "swordfish")
        client._opener = opener  # type: ignore[assignment]

        albums = client.search_albums("radio head", limit=20, offset=5)

        self.assertEqual(
            albums,
            [Album("search-album-id", "Amnesiac", "Radiohead", 2001, 11)],
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.request_url).query)
        self.assertEqual(query["query"], ["radio head"])
        self.assertEqual(query["artistCount"], ["0"])
        self.assertEqual(query["albumCount"], ["20"])
        self.assertEqual(query["albumOffset"], ["5"])
        self.assertEqual(query["songCount"], ["0"])

    def test_http_is_rejected(self) -> None:
        with self.assertRaisesRegex(SubsonicError, "must use HTTPS"):
            SubsonicClient("http://music.example", "alice", "password")

    def test_network_error_does_not_expose_authenticated_url(self) -> None:
        client = SubsonicClient("https://music.example", "alice", "swordfish")
        error = urllib.error.URLError("network exploded")
        opener = _FakeOpener(error)
        client._opener = opener  # type: ignore[assignment]

        with self.assertRaises(SubsonicError) as raised:
            client.ping()

        message = str(raised.exception)
        self.assertNotIn("swordfish", message)
        self.assertNotIn("t=", message)
        self.assertNotIn(opener.request_url, message)


class _FakeSubsonicClient:
    instances: list[_FakeSubsonicClient] = []

    def __init__(self, server_url: str, username: str, password: str) -> None:
        self.server_url = server_url
        self.username = username
        self.password = password
        self.wiped = False
        self.instances.append(self)

    def ping(self) -> None:
        return None

    def get_albums(self, *, order: str, limit: int, offset: int) -> list[Album]:
        return [Album("album-secret-id", "Kid A", "Radiohead", 2000, 10)]

    def search_albums(self, query: str, *, limit: int, offset: int) -> list[Album]:
        return [Album("search-album-id", "Amnesiac", "Radiohead", 2001, 11)]

    def wipe_credentials(self) -> None:
        self.password = ""
        self.wiped = True


class DaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeSubsonicClient.instances.clear()

    def test_connect_status_disconnect_lifecycle(self) -> None:
        daemon = TuneDaemon()
        request = {
            "command": "connect",
            "url": "https://music.example",
            "username": "alice",
            "password": "swordfish",
        }

        with mock.patch("tune.daemon.SubsonicClient", _FakeSubsonicClient):
            connected = daemon._handle_request(request)
            albums = daemon._handle_request(
                {
                    "command": "albums",
                    "order": "newest",
                    "limit": 25,
                    "offset": 0,
                }
            )
            search = daemon._handle_request(
                {
                    "command": "search",
                    "query": "radio head",
                    "limit": 20,
                    "offset": 0,
                }
            )
            status = daemon._handle_request({"command": "status"})
            cached_album_id = daemon.last_albums[0].id
            disconnected = daemon._handle_request({"command": "disconnect"})

        client = _FakeSubsonicClient.instances[0]
        self.assertTrue(connected["ok"])
        self.assertEqual(request["password"], "")
        self.assertTrue(status["connected"])
        self.assertEqual(albums["albums"][0]["number"], 1)
        self.assertNotIn("id", albums["albums"][0])
        self.assertEqual(search["albums"][0]["name"], "Amnesiac")
        self.assertEqual(cached_album_id, "search-album-id")
        self.assertTrue(disconnected["ok"])
        self.assertTrue(client.wiped)
        self.assertIsNone(daemon.client)
        self.assertTrue(daemon.stop_requested)


class CLITests(unittest.TestCase):
    def test_albums_prints_numbered_sanitized_results(self) -> None:
        response = {
            "ok": True,
            "albums": [
                {
                    "number": 1,
                    "name": "Kid A\x1b[31m",
                    "artist": "Radiohead",
                    "year": 2000,
                    "song_count": 10,
                }
            ],
        }
        arguments = argparse.Namespace(newest=False, limit=100, offset=0)
        output = io.StringIO()

        with mock.patch("tune.cli._send_request", return_value=response):
            with redirect_stdout(output):
                result = cli._command_albums(arguments)

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "1  Radiohead — Kid A[31m (2000, 10 tracks)\n")

    def test_search_joins_query_words(self) -> None:
        response = {"ok": True, "albums": []}
        arguments = argparse.Namespace(query=["radio", "head"], limit=20, offset=5)

        with mock.patch("tune.cli._send_request", return_value=response) as send:
            with redirect_stdout(io.StringIO()):
                result = cli._command_search(arguments)

        self.assertEqual(result, 0)
        send.assert_called_once_with(
            {
                "command": "search",
                "query": "radio head",
                "limit": 20,
                "offset": 5,
            }
        )

    def test_album_limit_is_bounded(self) -> None:
        self.assertEqual(cli._album_limit("500"), 500)
        with self.assertRaises(argparse.ArgumentTypeError):
            cli._album_limit("501")


if __name__ == "__main__":
    unittest.main()

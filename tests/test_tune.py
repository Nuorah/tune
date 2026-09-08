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
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import tune.cli as cli
from tune.daemon import TuneDaemon
from tune.player import MpvPlayer, PlaybackStatus, PlayerError, StreamProxy
from tune.protocol import ProtocolError, receive_message, send_message
from tune.runtime import control_socket_path, runtime_dir
from tune.subsonic import Album, AudioStream, Song, SubsonicClient, SubsonicError


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
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.position = 0
        self.closed = False

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self, limit: int) -> bytes:
        chunk = self.body[self.position : self.position + limit]
        self.position += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


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

    def test_open_stream_forwards_range_without_exposing_url(self) -> None:
        response = _FakeResponse(
            b"abcdef",
            status=206,
            headers={
                "Content-Type": "audio/flac",
                "Content-Length": "6",
                "Content-Range": "bytes 10-15/100",
                "Accept-Ranges": "bytes",
            },
        )
        opener = _FakeOpener(response)
        client = SubsonicClient("https://music.example", "alice", "swordfish")
        client._opener = opener  # type: ignore[assignment]

        with client.open_stream("song-id", byte_range="bytes=10-15") as stream:
            self.assertEqual(stream.status, 206)
            self.assertEqual(stream.content_type, "audio/flac")
            self.assertEqual(stream.content_length, 6)
            self.assertEqual(stream.content_range, "bytes 10-15/100")
            self.assertEqual(stream.accept_ranges, "bytes")
            self.assertEqual(stream.read(3), b"abc")
            self.assertEqual(stream.read(3), b"def")
            self.assertFalse(hasattr(stream, "url"))

        self.assertTrue(response.closed)
        request = opener.request_url
        parsed = urllib.parse.urlsplit(request)
        self.assertEqual(parsed.path, "/rest/stream.view")
        self.assertEqual(urllib.parse.parse_qs(parsed.query)["id"], ["song-id"])

    def test_open_stream_rejects_invalid_range(self) -> None:
        client = SubsonicClient("https://music.example", "alice", "swordfish")
        with self.assertRaisesRegex(ValueError, "invalid byte range"):
            client.open_stream("song-id", byte_range="bytes=0-1\r\nX-Evil: yes")

    def test_get_album_preserves_song_order_and_metadata(self) -> None:
        body = json.dumps(
            {
                "subsonic-response": {
                    "status": "ok",
                    "album": {
                        "name": "Kid A",
                        "artist": "Radiohead",
                        "song": [
                            {
                                "id": "song-2",
                                "title": "Kid A",
                                "track": 2,
                                "discNumber": 1,
                                "duration": 284,
                            },
                            {
                                "id": "song-1",
                                "title": "Everything in Its Right Place",
                                "artist": "Radiohead",
                                "album": "Kid A",
                                "track": 1,
                            },
                        ],
                    },
                }
            }
        ).encode()
        opener = _FakeOpener(_FakeResponse(body))
        client = SubsonicClient("https://music.example", "alice", "swordfish")
        client._opener = opener  # type: ignore[assignment]

        songs = client.get_album("album-secret-id")

        self.assertEqual(
            songs,
            [
                Song("song-2", "Kid A", "Radiohead", "Kid A", 2, 1, 284),
                Song(
                    "song-1",
                    "Everything in Its Right Place",
                    "Radiohead",
                    "Kid A",
                    1,
                    None,
                    None,
                ),
            ],
        )
        parsed = urllib.parse.urlsplit(opener.request_url)
        self.assertEqual(parsed.path, "/rest/getAlbum.view")
        self.assertEqual(
            urllib.parse.parse_qs(parsed.query)["id"],
            ["album-secret-id"],
        )
        self.assertFalse(hasattr(songs[0], "url"))

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


class _FakeStreamingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def open_stream(
        self,
        song_id: str,
        *,
        byte_range: str | None = None,
    ) -> AudioStream:
        self.calls.append((song_id, byte_range))
        return AudioStream(
            _FakeResponse(
                b"audio-data",
                status=206,
                headers={
                    "Content-Type": "audio/flac",
                    "Content-Length": "10",
                    "Content-Range": "bytes 2-11/100",
                    "Accept-Ranges": "bytes",
                },
            )
        )


class PlayerTests(unittest.TestCase):
    def test_stream_proxy_forwards_range_and_audio(self) -> None:
        client = _FakeStreamingClient()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        with StreamProxy(client) as proxy:
            url = proxy.url_for("song/id")
            request = urllib.request.Request(url, headers={"Range": "bytes=2-11"})
            with opener.open(request, timeout=2) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers["Content-Type"], "audio/flac")
                self.assertEqual(response.headers["Content-Range"], "bytes 2-11/100")
                self.assertEqual(response.read(), b"audio-data")

        self.assertEqual(client.calls, [("song/id", "bytes=2-11")])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            proxy.url_for("another-song")


class MpvPlayerTests(unittest.TestCase):
    def _player(self, temporary_directory: str) -> MpvPlayer:
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": temporary_directory}):
            return MpvPlayer()

    def test_start_passes_only_loopback_urls_to_mpv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            player = self._player(temporary_directory)
            process = mock.Mock()
            process.poll.return_value = None
            url = "http://127.0.0.1:12345/token/stream/song-id"

            with mock.patch("tune.player.subprocess.Popen", return_value=process) as popen:
                with mock.patch("tune.player._remove_stale_player_socket"):
                    with mock.patch.object(Path, "exists", return_value=True):
                        with mock.patch.object(player, "_command", return_value=False):
                            player.start([url])

            arguments = popen.call_args.args[0]
            self.assertIn("--idle=yes", arguments)
            self.assertIn("--gapless-audio=yes", arguments)
            self.assertIn(url, arguments)
            self.assertFalse(any("music.example" in value for value in arguments))
            player._process = None

    def test_start_rejects_non_loopback_url_before_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            player = self._player(temporary_directory)
            with mock.patch("tune.player.subprocess.Popen") as popen:
                with self.assertRaisesRegex(PlayerError, "non-loopback"):
                    player.start(["https://music.example/rest/stream.view?t=secret"])
            popen.assert_not_called()

    def test_controls_send_expected_ipc_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            player = self._player(temporary_directory)
            with mock.patch.object(player, "_command") as command:
                player.pause()
                player.resume()
                player.next_track()
                player.previous_track()
                player.stop()

            self.assertEqual(
                command.call_args_list,
                [
                    mock.call(["set_property", "pause", True]),
                    mock.call(["set_property", "pause", False]),
                    mock.call(["playlist-next", "force"]),
                    mock.call(["playlist-prev", "force"]),
                    mock.call(["stop"]),
                ],
            )

    def test_status_reads_active_playback_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            player = self._player(temporary_directory)
            process = mock.Mock()
            process.poll.return_value = None
            player._process = process

            with mock.patch.object(
                player,
                "_command",
                side_effect=[False, True, 2, 42.5, 180.0],
            ) as command:
                status = player.status()

            self.assertEqual(status, PlaybackStatus(True, True, 2, 42.5, 180.0))
            self.assertEqual(
                command.call_args_list,
                [
                    mock.call(["get_property", "idle-active"]),
                    mock.call(["get_property", "pause"]),
                    mock.call(["get_property", "playlist-pos"]),
                    mock.call(["get_property", "time-pos"]),
                    mock.call(["get_property", "duration"]),
                ],
            )
            player._process = None

    def test_status_stops_querying_when_mpv_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            player = self._player(temporary_directory)
            process = mock.Mock()
            process.poll.return_value = None
            player._process = process

            with mock.patch.object(player, "_command", return_value=True) as command:
                status = player.status()

            self.assertEqual(status, PlaybackStatus(False, False, None, None, None))
            command.assert_called_once_with(["get_property", "idle-active"])
            player._process = None

    def test_close_quits_and_reaps_mpv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            player = self._player(temporary_directory)
            process = mock.Mock()
            process.poll.return_value = None
            player._process = process

            with mock.patch.object(player, "_command") as command:
                with mock.patch("tune.player._remove_stale_player_socket") as remove:
                    player.close()

            command.assert_called_once_with(["quit"])
            process.wait.assert_called_once_with(timeout=2)
            remove.assert_called_once_with(player._socket_path)
            self.assertIsNone(player._process)


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

    def get_album(self, album_id: str) -> list[Song]:
        return [
            Song("song-1", "Everything in Its Right Place", "Radiohead", "Kid A", 1, 1, 251),
            Song("song-2", "Kid A", "Radiohead", "Kid A", 2, 1, 284),
        ]

    def wipe_credentials(self) -> None:
        self.password = ""
        self.wiped = True


class _FakeStreamProxy:
    def __init__(self, client: _FakeSubsonicClient) -> None:
        self.client = client
        self.closed = False

    def url_for(self, song_id: str) -> str:
        return f"http://127.0.0.1:12345/token/stream/{song_id}"

    def close(self) -> None:
        self.closed = True


class _FakePlayer:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.actions: list[str] = []
        self.paused = False
        self.stopped = False
        self.playlist_index = 0
        self.closed = False

    def start(self, urls: list[str]) -> None:
        self.urls = urls

    def pause(self) -> None:
        self.actions.append("pause")
        self.paused = True

    def resume(self) -> None:
        self.actions.append("resume")
        self.paused = False

    def next_track(self) -> None:
        self.actions.append("next")
        self.playlist_index += 1

    def previous_track(self) -> None:
        self.actions.append("previous")
        self.playlist_index -= 1

    def stop(self) -> None:
        self.actions.append("stop")
        self.stopped = True

    def status(self) -> PlaybackStatus:
        if self.stopped:
            return PlaybackStatus(False, False, None, None, None)
        return PlaybackStatus(True, self.paused, self.playlist_index, 42.5, 251.0)

    def close(self) -> None:
        self.closed = True


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

        with (
            mock.patch("tune.daemon.SubsonicClient", _FakeSubsonicClient),
            mock.patch("tune.daemon.StreamProxy", _FakeStreamProxy),
            mock.patch("tune.daemon.MpvPlayer", _FakePlayer),
        ):
            connected = daemon._handle_request(request)
            albums = daemon._handle_request(
                {
                    "command": "albums",
                    "order": "newest",
                    "limit": 25,
                    "offset": 0,
                }
            )
            played = daemon._handle_request({"command": "play", "number": 1})
            player = daemon.player
            proxy = daemon.stream_proxy
            playing_status = daemon._handle_request({"command": "status"})
            controls = [
                daemon._handle_request({"command": command})
                for command in ("pause", "resume", "next", "previous", "stop")
            ]
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
        self.assertEqual(status["playback"], {"state": "stopped"})
        self.assertEqual(
            playing_status["playback"],
            {
                "state": "playing",
                "artist": "Radiohead",
                "album": "Kid A",
                "title": "Everything in Its Right Place",
                "number": 1,
                "total": 2,
                "position": 42.5,
                "duration": 251.0,
            },
        )
        self.assertEqual(albums["albums"][0]["number"], 1)
        self.assertNotIn("id", albums["albums"][0])
        self.assertEqual(played["first_song"], "Everything in Its Right Place")
        self.assertEqual(
            player.urls,
            [
                "http://127.0.0.1:12345/token/stream/song-1",
                "http://127.0.0.1:12345/token/stream/song-2",
            ],
        )
        self.assertEqual(
            [response["action"] for response in controls],
            ["pause", "resume", "next", "previous", "stop"],
        )
        self.assertEqual(player.actions, ["pause", "resume", "next", "previous", "stop"])
        self.assertTrue(player.closed)
        self.assertTrue(proxy.closed)
        self.assertEqual(search["albums"][0]["name"], "Amnesiac")
        self.assertEqual(cached_album_id, "search-album-id")
        self.assertTrue(disconnected["ok"])
        self.assertTrue(client.wiped)
        self.assertIsNone(daemon.client)
        self.assertTrue(daemon.stop_requested)


class CLITests(unittest.TestCase):
    def test_status_prints_connection_and_paused_track(self) -> None:
        response = {
            "ok": True,
            "connected": True,
            "server": "https://music.example",
            "username": "alice",
            "playback": {
                "state": "paused",
                "artist": "Radiohead",
                "album": "Kid A",
                "title": "Optimistic",
                "number": 6,
                "total": 10,
                "position": 62.9,
                "duration": 315.0,
            },
        }
        output = io.StringIO()

        with mock.patch("tune.cli._send_request", return_value=response):
            with redirect_stdout(output):
                result = cli._command_status(argparse.Namespace())

        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue(),
            "Connected to https://music.example as alice\n"
            "Paused 6/10: Radiohead — Optimistic (Kid A) [1:02 / 5:15]\n",
        )

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

    def test_play_sends_number_and_prints_metadata(self) -> None:
        response = {
            "ok": True,
            "album": "Kid A",
            "artist": "Radiohead",
            "song_count": 10,
            "first_song": "Everything in Its Right Place",
        }
        output = io.StringIO()

        with mock.patch("tune.cli._send_request", return_value=response) as send:
            with redirect_stdout(output):
                result = cli._command_play(argparse.Namespace(number=3))

        self.assertEqual(result, 0)
        send.assert_called_once_with({"command": "play", "number": 3})
        self.assertEqual(
            output.getvalue(),
            "Playing Radiohead — Kid A (10 tracks); first: Everything in Its Right Place\n",
        )

    def test_playback_control_sends_action(self) -> None:
        arguments = argparse.Namespace(action="pause", confirmation="Paused")
        output = io.StringIO()

        with mock.patch(
            "tune.cli._send_request",
            return_value={"ok": True, "action": "pause"},
        ) as send:
            with redirect_stdout(output):
                result = cli._command_playback_control(arguments)

        self.assertEqual(result, 0)
        send.assert_called_once_with({"command": "pause"})
        self.assertEqual(output.getvalue(), "Paused\n")

    def test_album_number_must_be_positive(self) -> None:
        self.assertEqual(cli._album_number("1"), 1)
        with self.assertRaises(argparse.ArgumentTypeError):
            cli._album_number("0")

    def test_album_limit_is_bounded(self) -> None:
        self.assertEqual(cli._album_limit("500"), 500)
        with self.assertRaises(argparse.ArgumentTypeError):
            cli._album_limit("501")


if __name__ == "__main__":
    unittest.main()

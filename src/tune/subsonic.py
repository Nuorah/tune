"""Minimal Subsonic API client used by the daemon."""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


API_VERSION = "1.16.1"
CLIENT_NAME = "tune"
MAX_RESPONSE_BYTES = 1024 * 1024


class SubsonicError(RuntimeError):
    """A safe-to-display Subsonic connection or protocol error."""


@dataclass(frozen=True, slots=True)
class Album:
    id: str
    name: str
    artist: str
    year: int | None
    song_count: int | None


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class SubsonicClient:
    """A small HTTPS-only client retaining its password in memory."""

    def __init__(self, server_url: str, username: str, password: str) -> None:
        self.server_url = _normalize_server_url(server_url)
        self.username = username.strip()
        if not self.username:
            raise SubsonicError("username must not be empty")
        if not password:
            raise SubsonicError("password must not be empty")

        self._password = bytearray(password.encode("utf-8"))
        self._opener = urllib.request.build_opener(_NoRedirects())

    def wipe_credentials(self) -> None:
        """Best-effort overwrite of the retained password buffer."""
        for index in range(len(self._password)):
            self._password[index] = 0
        self._password.clear()

    def ping(self) -> None:
        """Authenticate with the server and verify a successful response."""
        self._request("ping")

    def get_albums(
        self,
        *,
        order: str = "alphabeticalByName",
        limit: int = 100,
        offset: int = 0,
    ) -> list[Album]:
        """Return a page of albums using Subsonic's ID3 album endpoint."""
        if order not in {"alphabeticalByName", "newest"}:
            raise ValueError("unsupported album order")
        if not 1 <= limit <= 500:
            raise ValueError("album limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("album offset must not be negative")

        payload = self._request(
            "getAlbumList2",
            {"type": order, "size": limit, "offset": offset},
        )
        album_list = payload.get("albumList2")
        if not isinstance(album_list, dict):
            return []
        return _parse_albums(album_list.get("album"))

    def search_albums(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Album]:
        """Search albums using the server's matching and ranking behavior."""
        query = query.strip()
        if not query:
            raise ValueError("search query must not be empty")
        if not 1 <= limit <= 500:
            raise ValueError("album limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("album offset must not be negative")

        payload = self._request(
            "search3",
            {
                "query": query,
                "artistCount": 0,
                "albumCount": limit,
                "albumOffset": offset,
                "songCount": 0,
            },
        )
        results = payload.get("searchResult3")
        if not isinstance(results, dict):
            return []
        return _parse_albums(results.get("album"))

    def _request(
        self,
        endpoint: str,
        parameters: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        response = self._request_json(endpoint, parameters)
        payload = response.get("subsonic-response")
        if not isinstance(payload, dict):
            raise SubsonicError("server returned an invalid Subsonic response")

        if payload.get("status") != "ok":
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                raise SubsonicError(f"server rejected the request: {error['message']}")
            raise SubsonicError("server rejected the request")
        return payload

    def _request_json(
        self,
        endpoint: str,
        parameters: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self._build_url(endpoint, parameters),
            headers={"Accept": "application/json", "User-Agent": "tune/0.1"},
        )

        try:
            with self._opener.open(request, timeout=15) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_RESPONSE_BYTES:
                            raise SubsonicError("server response is too large")
                    except ValueError:
                        pass

                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise SubsonicError("server redirects are not allowed") from None
            raise SubsonicError(f"server returned HTTP status {error.code}") from None
        except urllib.error.URLError:
            raise SubsonicError("could not securely reach the server") from None
        except TimeoutError:
            raise SubsonicError("connection to the server timed out") from None

        if len(body) > MAX_RESPONSE_BYTES:
            raise SubsonicError("server response is too large")

        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SubsonicError("server returned invalid JSON") from None

        if not isinstance(decoded, dict):
            raise SubsonicError("server returned an invalid JSON response")
        return decoded

    def _build_url(
        self,
        endpoint: str,
        parameters: Mapping[str, str | int] | None = None,
    ) -> str:
        salt = secrets.token_hex(8)
        password = bytes(self._password)
        token = hashlib.md5(password + salt.encode("ascii"), usedforsecurity=False).hexdigest()
        query_parameters: dict[str, str | int] = dict(parameters or {})
        query_parameters.update(
            {
                "u": self.username,
                "t": token,
                "s": salt,
                "v": API_VERSION,
                "c": CLIENT_NAME,
                "f": "json",
            }
        )
        query = urllib.parse.urlencode(query_parameters)
        return f"{self.server_url}/rest/{endpoint}.view?{query}"


def _parse_albums(raw_albums: Any) -> list[Album]:
    if raw_albums is None:
        return []
    if not isinstance(raw_albums, list):
        raise SubsonicError("server returned an invalid album list")

    albums: list[Album] = []
    for item in raw_albums:
        if not isinstance(item, dict):
            raise SubsonicError("server returned an invalid album")
        album_id = item.get("id")
        name = item.get("name")
        artist = item.get("artist")
        if not all(isinstance(value, str) for value in (album_id, name, artist)):
            raise SubsonicError("server returned an album with missing metadata")

        year = item.get("year")
        song_count = item.get("songCount")
        albums.append(
            Album(
                id=album_id,
                name=name,
                artist=artist,
                year=year if isinstance(year, int) else None,
                song_count=song_count if isinstance(song_count, int) else None,
            )
        )
    return albums


def _normalize_server_url(server_url: str) -> str:
    value = server_url.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        raise SubsonicError("server URL is invalid") from None

    if parsed.scheme.lower() != "https":
        raise SubsonicError("server URL must use HTTPS")
    if not parsed.hostname:
        raise SubsonicError("server URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise SubsonicError("server URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise SubsonicError("server URL must not contain a query or fragment")

    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", parsed.netloc, path, "", ""))

"""Minimal Subsonic API client used by the daemon."""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


API_VERSION = "1.16.1"
CLIENT_NAME = "tune"
MAX_RESPONSE_BYTES = 1024 * 1024


class SubsonicError(RuntimeError):
    """A safe-to-display Subsonic connection or protocol error."""


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
        response = self._request_json("ping")
        payload = response.get("subsonic-response")
        if not isinstance(payload, dict):
            raise SubsonicError("server returned an invalid Subsonic response")

        if payload.get("status") != "ok":
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                raise SubsonicError(f"server rejected the connection: {error['message']}")
            raise SubsonicError("server rejected the connection")

    def _request_json(self, endpoint: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self._build_url(endpoint),
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

    def _build_url(self, endpoint: str) -> str:
        salt = secrets.token_hex(8)
        password = bytes(self._password)
        token = hashlib.md5(password + salt.encode("ascii"), usedforsecurity=False).hexdigest()
        query = urllib.parse.urlencode(
            {
                "u": self.username,
                "t": token,
                "s": salt,
                "v": API_VERSION,
                "c": CLIENT_NAME,
                "f": "json",
            }
        )
        return f"{self.server_url}/rest/{endpoint}.view?{query}"


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

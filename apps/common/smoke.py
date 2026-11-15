"""
Helpers for smoke tests that exercise a *running* CourtControl instance over
real HTTP (session login, CSRF, JSON, multipart uploads) — as opposed to the
in-process test suite. Standard library only (the HTTP side), so they run
anywhere the project runs.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from http.cookiejar import CookieJar
from typing import Any

LOGIN_PATH = "/api-auth/login/"
LOGOUT_PATH = "/api-auth/logout/"


class SmokeFailure(Exception):
    """A smoke check failed (or the service could not be reached)."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


@dataclass
class SmokeResponse:
    status: int
    body: bytes
    content_type: str

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as exc:
            raise SmokeFailure(
                f"Expected a JSON body, got {self.content_type or 'no content type'}: "
                f"{self.body[:200]!r}"
            ) from exc

    def describe(self) -> str:
        """Short form for failure messages: status + (truncated) body."""
        text = " ".join(self.body.decode("utf-8", "replace").split())  # one line
        return f"HTTP {self.status}: {text[:200]}{'…' if len(text) > 200 else ''}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects as responses (e.g. the 302 that signals a successful login)."""

    def redirect_request(self, *args, **kwargs):
        return None


class SmokeClient:
    """HTTP client with a cookie jar: keeps the session and CSRF cookies like a browser."""

    def __init__(self, base_url: str, *, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirect()
        )

    # --- cookies -----------------------------------------------------------

    def cookie(self, name: str) -> str | None:
        return next((c.value for c in self.cookies if c.name == name), None)

    @property
    def session_key(self) -> str | None:
        return self.cookie("sessionid")

    # --- requests ----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        form: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        params: dict[str, str] | None = None,
        csrf: bool = False,
    ) -> SmokeResponse:
        """`files`: multipart/form-data upload, {field: (filename, content, content type)}."""
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"Accept": "application/json"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif files is not None:
            data, headers["Content-Type"] = _multipart(files)
        if csrf:
            # Django checks the token, and (over HTTPS) that Referer is same-origin.
            headers["X-CSRFToken"] = self.cookie("csrftoken") or ""
            headers["Referer"] = f"{self.base_url}/"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return SmokeResponse(resp.status, resp.read(), resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as resp:  # 3xx (no redirects followed), 4xx, 5xx
            return SmokeResponse(resp.code, resp.read(), resp.headers.get("Content-Type", ""))
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SmokeFailure(f"Cannot reach {url}: {reason}. Is the server running?") from exc

    def get(self, path: str, params: dict[str, str] | None = None) -> SmokeResponse:
        return self.request("GET", path, params=params)

    def post_json(self, path: str, body: Any) -> SmokeResponse:
        return self.request("POST", path, json_body=body, csrf=True)

    def patch_json(self, path: str, body: Any) -> SmokeResponse:
        return self.request("PATCH", path, json_body=body, csrf=True)

    def delete(self, path: str, params: dict[str, str] | None = None) -> SmokeResponse:
        return self.request("DELETE", path, params=params, csrf=True)

    # --- session auth --------------------------------------------------------

    def login(self, email: str, password: str) -> SmokeResponse:
        """Log in through DRF's session login form. Success = 302 + a session cookie."""
        self.get(LOGIN_PATH)  # sets the csrftoken cookie
        response = self.request(
            "POST",
            LOGIN_PATH,
            form={
                "username": email,  # the field is named username but takes the email
                "password": password,
                "csrfmiddlewaretoken": self.cookie("csrftoken") or "",
            },
            csrf=True,
        )
        # Django rotates the CSRF token on login; the jar now holds the new one.
        return response

    def logout(self) -> SmokeResponse:
        return self.request("POST", LOGOUT_PATH, form={}, csrf=True)


def _multipart(files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"smoke-{secrets.token_hex(12)}"
    body = b""
    for field, (filename, content, content_type) in files.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + content + b"\r\n"
    return body + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def sessions_of(user) -> list[str]:
    """
    Session keys of `user` — e.g. a smoke user left over by a run killed before
    its cleanup. Sessions can only be matched by decoding them, so first narrow
    down by time: they were created right after the user, hence expire around
    date_joined + SESSION_COOKIE_AGE (indexed column; 1h of slack).
    """
    from django.conf import settings
    from django.contrib.auth import SESSION_KEY
    from django.contrib.sessions.models import Session

    earliest = user.date_joined + timedelta(seconds=settings.SESSION_COOKIE_AGE)
    candidates = Session.objects.filter(expire_date__gte=earliest, expire_date__lt=earliest + timedelta(hours=1))
    return [s.pk for s in candidates if s.get_decoded().get(SESSION_KEY) == str(user.pk)]

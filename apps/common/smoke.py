"""
Helpers for smoke tests that exercise a *running* CourtControl instance over
real HTTP (session login, CSRF, JSON) — as opposed to the in-process test
suite. Standard library only, so they run anywhere the project runs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
        csrf: bool = False,
    ) -> SmokeResponse:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
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

    def get(self, path: str) -> SmokeResponse:
        return self.request("GET", path)

    def post_json(self, path: str, body: Any) -> SmokeResponse:
        return self.request("POST", path, json_body=body, csrf=True)

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

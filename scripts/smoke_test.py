#!/usr/bin/env python3
"""
Happy-path smoke test against a running CourtControl instance.

    python3 scripts/smoke_test.py            # defaults: http://localhost:8000, seed player ana
    make smoke                                # same, via the Makefile

Steps:
  1. log in (session auth via /api-auth/login/)
  2. list tomorrow's available slots
  3. book one slot (and check it disappeared from the listing)
  4. book another slot that is still inside the club's cancellation window,
     cancel it, and check the slot is available again

Standard library only. Exits 0 on success, 1 on the first failed step.
Note: step 3's booking is kept (that's the happy path); run `make seed-reset`
to clean up after many runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar

GREEN, RED, DIM, RESET = ("\033[32m", "\033[31m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("",) * 4


class SmokeTestFailure(Exception):
    pass


def step(message: str) -> None:
    print(f"\n{message}")


def ok(message: str) -> None:
    print(f"  {GREEN}✔{RESET} {message}")


def detail(message: str) -> None:
    print(f"    {DIM}{message}{RESET}")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestFailure(message)


class Client:
    """Tiny session-auth JSON client (cookies + Django CSRF)."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def cookie(self, name: str) -> str | None:
        return next((c.value for c in self.cookies if c.name == name), None)

    def request(self, method: str, path: str, *, params=None, json_body=None, form=None):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "Referer": f"{self.base_url}/"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if method != "GET" and self.cookie("csrftoken"):
            headers["X-CSRFToken"] = self.cookie("csrftoken")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                return response.status, _parse(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _parse(error.read())
        except urllib.error.URLError as error:
            raise SmokeTestFailure(
                f"Can't reach {self.base_url} ({error.reason}). Is the server running? (make run)"
            ) from error

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path, body=None):
        return self.request("POST", path, json_body=body or {})


def _parse(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")[:300]


def expect(status: int, body, expected: int, action: str):
    check(status == expected, f"{action}: expected HTTP {expected}, got {status}: {body}")
    return body


# --- steps ---------------------------------------------------------------------------


def login(client: Client, email: str, password: str) -> None:
    step(f"1. Log in as {email}")
    client.get("/api-auth/login/")  # sets the csrftoken cookie
    check(client.cookie("csrftoken") is not None, "No CSRF cookie from /api-auth/login/.")
    client.request(
        "POST",
        "/api-auth/login/",
        form={
            "csrfmiddlewaretoken": client.cookie("csrftoken"),
            "username": email,  # Django's login form field; holds the email
            "password": password,
        },
    )
    check(
        client.cookie("sessionid") is not None,
        f"Login failed for {email} — wrong credentials? (seed players use password 'player123')",
    )
    status, body = client.get("/api/v1/reservations/")
    expect(status, body, 200, "Authenticated API call")
    ok("Logged in (session cookie set, API accepts it)")


def list_slots(client: Client, day: date) -> list[dict]:
    """All courts with their available slots for `day` (follows pagination)."""
    courts, page = [], "/api/v1/courts/"
    params = {"date": day.isoformat()}
    while page:
        status, body = client.request("GET", page, params=params)
        expect(status, body, 200, f"List courts for {day}")
        courts.extend(body["results"])
        page, params = body["next"], None  # `next` already carries the query string
    return courts


def slot_still_available(client: Client, day: date, court_id: str, start: str) -> bool:
    courts = list_slots(client, day)
    court = next((c for c in courts if c["id"] == court_id), None)
    return court is not None and any(s["start"] == start for s in court["available_slots"])


def book(client: Client, court: dict, slot: dict) -> dict:
    start = datetime.fromisoformat(slot["start"])  # already in the club's timezone
    end = datetime.fromisoformat(slot["end"])
    status, body = client.post(
        "/api/v1/reservations/",
        {
            "court": court["id"],
            "date": start.date().isoformat(),
            "start_time": start.strftime("%H:%M"),
            "end_time": end.strftime("%H:%M"),
        },
    )
    return expect(status, body, 201, f"Book {court['name']} at {slot['start']}")


def describe(court: dict, slot: dict) -> str:
    return f"{court['club']['name']} · {court['name']} · {slot['start'][11:16]}–{slot['end'][11:16]} ({slot['start'][:10]})"


def run(base_url: str, email: str, password: str, day: date) -> None:
    client = Client(base_url)
    login(client, email, password)

    step(f"2. List available slots for {day}")
    courts = list_slots(client, day)
    candidates = [(court, slot) for court in courts for slot in court["available_slots"]]
    check(candidates, f"No available slots on {day} for {email}. Seed data loaded? (make seed)")
    ok(f"{len(courts)} courts, {len(candidates)} free slots")

    step("3. Book a slot")
    court, slot = candidates[0]
    booking = book(client, court, slot)
    check(booking["status"] == "CONFIRMED", f"Expected CONFIRMED, got {booking['status']}")
    ok(f"Booked {describe(court, slot)}")
    detail(f"id={booking['id']}  status={booking['status']}  price={booking['total_price']}")
    check(not slot_still_available(client, day, court["id"], slot["start"]),
          "Booked slot is still listed as available.")
    ok("Slot no longer listed as available")

    step("4. Book another slot and cancel it")
    now = datetime.now(timezone.utc)

    def cancellable(item) -> bool:  # deadline = start - club window, must not have passed
        c, s = item
        window = timedelta(hours=c["club"]["cancellation_window_hours"])
        return datetime.fromisoformat(s["start"]) - window > now + timedelta(minutes=1)

    others = [item for item in candidates if item != (court, slot) and cancellable(item)]
    check(
        others,
        f"No other slot on {day} is still inside its club's cancellation window. "
        "Try a later day: --date YYYY-MM-DD",
    )
    court2, slot2 = others[0]
    booking2 = book(client, court2, slot2)
    check(booking2["can_cancel"], f"New booking reports can_cancel=false: {booking2}")
    ok(f"Booked {describe(court2, slot2)}")
    detail(f"id={booking2['id']}  cancellation deadline={booking2['cancellation_deadline']}")

    status, cancelled = client.post(
        f"/api/v1/reservations/{booking2['id']}/cancel/", {"reason": "smoke test"}
    )
    expect(status, cancelled, 200, "Cancel booking")
    check(cancelled["state"] == "cancelled", f"Expected state 'cancelled', got {cancelled['state']}")
    ok(f"Cancelled (state={cancelled['state']}, reason='{cancelled['cancellation_reason']}')")
    check(slot_still_available(client, day, court2["id"], slot2["start"]),
          "Cancelled slot did not become available again.")
    ok("Slot is available again")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--email", default="ana@example.com")
    parser.add_argument("--password", default="player123")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=1),
                        help="Day to book (default: tomorrow)")
    args = parser.parse_args()

    print(f"Smoke test against {args.base_url}")
    try:
        run(args.base_url, args.email, args.password, args.date)
    except SmokeTestFailure as failure:
        print(f"\n{RED}✖ FAILED:{RESET} {failure}")
        return 1
    print(f"\n{GREEN}✔ Happy path OK{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

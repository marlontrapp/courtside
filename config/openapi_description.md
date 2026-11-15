Tennis court booking and scheduling. Every endpoint lives under `/api/v1/`
and needs a logged-in user (any user, not only staff) — except the account
endpoints (sign up, log in, password reset).

## Accounts and authentication (session)

Accounts (tag *Accounts*, `/api/v1/auth/…`):

- **Sign up** with an email, a name and — optionally — a password: a 6-digit
  code is emailed, and verifying it creates the account and logs in.
- **Log in** with the password, or with a **code sent by email** (accounts
  without a password always do).
- **Forgot password:** a code sent by email lets you set a new password (or a
  first one); every other session is logged out.

Codes expire after 10 minutes, allow 5 tries and work once; each request
returns a `challenge_id` to send back with the code. Answers never reveal
whether an email has an account.

Logging in starts a **session** (cookie) that authenticates every request.
`POST`, `PUT`, `PATCH` and `DELETE` — including the account endpoints,
before logging in — need the CSRF token in the `X-CSRFToken` header (from
`GET /api/v1/auth/csrf/` or the `csrftoken` cookie) and, over HTTPS, a
same-origin `Referer`. Not logged in → `403`. In a browser, DRF's browsable
API also has a **Log in** link (`/api-auth/login/`).

With curl, keep the session in a cookie jar:

```bash
BASE=http://localhost:8000
csrf() { awk '$6 == "csrftoken" {print $7}' cookies.txt; }
curl -s -c cookies.txt "$BASE/api/v1/auth/csrf/" -o /dev/null        # the csrftoken cookie
curl -s -b cookies.txt -c cookies.txt -X POST "$BASE/api/v1/auth/login/" \
  -H "Content-Type: application/json" -H "X-CSRFToken: $(csrf)" -H "Referer: $BASE/" \
  -d '{"email": "player@example.com", "password": "<password>"}'     # the account; a session

curl -s -b cookies.txt -G "$BASE/api/v1/courts/" --data-urlencode "date=2030-01-08"

curl -s -b cookies.txt -X POST "$BASE/api/v1/reservations/" \
  -H "Content-Type: application/json" -H "X-CSRFToken: $(csrf)" -H "Referer: $BASE/" \
  -d '{"court": "<court id>", "date": "2030-01-08", "start_time": "18:00", "end_time": "19:00"}'
```

(The CSRF token changes when you log in: read it from the cookie again, as
`csrf()` does.)

## Conventions

- **Players** appear as `{"id": "<user id>", "first_name": "…", "last_name":
  "…"}`. **No endpoint returns a player's email** — except your own, in your
  account (`GET /api/v1/me/` and the login responses). Input that refers to
  players — booking `guests`, the reservations' `player` filter — takes the
  player **id** (as returned by the player search) or an email.
- **Times** are wall-clock in each **club's timezone**: dates and times you
  send (`date`, `start_time`, …) and the ISO-8601 datetimes returned (with
  the club's offset). A slot ending at midnight is written `"24:00"`.
- **Errors:** `400` with the invalid fields (`{"field": ["message"]}`, or
  `non_field_errors`), `403` not logged in / not allowed, `404` unknown — or
  not yours to see (a members-only court you can't book, a club you don't
  administer), `409` a conflict (the message says why).
- **Pagination:** lists are paginated by `?page=` (25 per page):
  `{"count", "next", "previous", "results"}`. The searches skip the total
  count: `{"next", "previous", "results"}`.

## Clubs: self-service registration with approval

Any logged-in user can **register a club** (`POST /api/v1/clubs/`). It starts
**awaiting approval** (`approval_status: PENDING`): hidden from players —
search, courts listing, booking — and visible only to its club admins and
site staff. Whoever registers it becomes its **club admin**, so they can set
it up meanwhile — settings, **courts** (`/api/v1/clubs/<club>/courts/`), day
templates, schedules, booking rules — and it goes live once approved. Site
staff review the queue (`GET /api/v1/clubs/pending/`) and **approve** or
**reject** (with a reason) — the registrant is emailed the decision. At most
3 clubs awaiting approval per user. Clubs registered by site staff are
approved right away.

## Schedules

A court's bookable slots come from three pieces, managed by club admins:

- **Day templates** ("slot configurations") — a club's named slot lists,
  e.g. *Workdays* or *Weekend*: slots of any length, gaps allowed, never
  overlapping.
- **Weekly schedule versions** — which template each weekday of a court uses,
  valid from `valid_from` until `valid_until` (empty = no end). A court's
  versions never overlap: together they're the history and plan of its
  schedule.
- **One-time overrides** — a court + a date → another template, or closed,
  with a `type` (`MAINTENANCE`, `WEATHER`, `TOURNAMENT`, `EVENT`, `HOLIDAY`,
  `OTHER`) and `notes` members see (courts listing `notices`, booking
  errors): *"Junior club championship until 14:00"* is an override whose
  template only has the afternoon slots; *"Flooded"* is a closed one.

Slots of a court on a date = the override for that date if any, else the
version covering the date → the template of that weekday (no template =
closed). Availability and booking validation both use this.

**History** (per club-local day): anything that applied to a day *before
today* never changes — versions can only start today or later (the one in
effect is cut short; with `valid_until`, the interrupted one resumes
afterwards), past versions and overrides can't be deleted, and a template's
slots freeze once it has applied to a past day (schedule a new template
instead). Today is still editable.

**Future bookings:** a change that would leave a future booking (one that
fitted the old slots) no longer fitting is refused with `409` listing those
bookings — pick a later date, or resend with `"force": true` (`?force=true`
on `DELETE`) to **cancel** them; the response lists `cancelled_reservations`.
Started or past bookings are never touched.

Only **club admins** (a current club-admin role at the club) and site staff
can use the schedule endpoints; for anyone else the club or court doesn't
exist (`404`).

## Booking rules

A club can make its slots — or the whole club — bookable only when a booking
meets a rule: doubles only, women only, an age range, at most N bookings per
week in prime time, one upcoming booking per player, …

- **Where** — a rule belongs to a club and is linked to any of: `courts`,
  `day_templates` (slot configurations: the days a court uses one of them)
  and `slots` (single slots of a day template: bookings covering one of
  them). Nothing linked = **the whole club**. Each kind of link narrows the
  rule (court **and** template **and** slot), any of the linked ones within a
  kind: courts 1–2 + the 19:00 slot = that slot on those two courts only.
  `valid_from` / `valid_until` (optional) limit when it's in force.
- **What** — a `kind` and its `params`; `GET /api/v1/booking-rule-kinds/`
  describes them: `booking_types` (e.g. doubles only), `player_gender`
  (women only), `player_age` (age on the match day), `max_bookings_per_period`
  (per day / week Mon–Sun / month) and `max_active_bookings` (upcoming at a
  time).
- **Players** — gender and age rules check the booker and, with
  `applies_to: all_players` (default), every **named** guest; missing
  profile data means not eligible. Count limits apply to the booker **and to
  every guest who is a member of the club**: the booking counts towards each
  of their limits, and a member's bookings count whether they booked or were
  a guest. `counts`: `matching` (default — only bookings the rule covers,
  e.g. only prime-time ones) or `club` (every booking at the club); bookings
  not cancelled or expired count.
- **Enforcement** — checked when booking: every broken rule is reported in a
  `400`. Messages never reveal a guest's personal data ("at least one of your
  guests doesn't meet it"). Rules apply to new bookings only: changing or
  deleting a rule never affects existing bookings.
- **Visible** in the courts listing: each slot's `rules` (ids) and each
  court's `booking_rules` (`name`, `summary`).
- **Deleting** a court, day template or slot linked to a rule is refused
  (`409` listing the rules) unless forced (`force=true`): the links are
  removed, and a rule left without any link of a kind it had (e.g. its only
  court) is **deactivated** rather than silently covering more — the
  response lists `unlinked_booking_rules` and `deactivated_booking_rules`.

A **singles court** isn't a rule but a property of the court
(`is_singles_only`): no doubles, at most 2 players (the booker and 1 guest).

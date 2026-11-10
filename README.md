# CourtControl

Production-ready Django backend for tennis court control and scheduling.

## Stack

- Python 3.13 · Django 5.2 LTS · Django REST Framework
- PostgreSQL 18 (`psycopg` v3) with `btree_gist` overlap protection
- Celery + Celery Beat · Redis 7
- Docker Compose

## Quick start

```bash
# 1. Environment
cp .env.example .env

# 2. Build images
docker compose build

# 3. Start the stack (web runs migrations on startup)
docker compose up -d

# 4. Create a superuser for /admin/
docker compose exec web python manage.py createsuperuser

# 5. After model changes
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate
```

Open:

- Admin: http://localhost:8000/admin/
- Health: http://localhost:8000/health/
- API: http://localhost:8000/api/v1/courts/

Host ports (mapped to avoid local conflicts): Postgres `5433`, Redis `6380`, Django `8000`.

## Services

| Service         | Role                                      |
|-----------------|-------------------------------------------|
| `db`            | PostgreSQL 18                             |
| `redis`         | Broker + cache                            |
| `web`           | Django (`runserver` with hot-reload)      |
| `celery_worker` | Async task worker                         |
| `celery_beat`   | Periodic scheduler (hold expiry, reminders) |

## Running locally without Docker

```bash
make install-postgres   # once, if PostgreSQL isn't installed
make install-redis      # once, optional — needed for Celery
make run
```

`make run` starts the whole stack in the foreground and streams every
service's output with a prefix (`web   |`, `worker|`, `beat  |`):

| Service | Details |
|---|---|
| PostgreSQL | private cluster on `127.0.0.1:54329` (data in `.pgdata/`) |
| Redis | private instance on `127.0.0.1:63790` (`.redisdata/`, no persistence) |
| Django | `migrate`, then `runserver` on http://127.0.0.1:8000 (hot reload) |
| Celery | `worker` + `beat` (DatabaseScheduler), same commands as docker-compose |

After migrating, it creates a **development superuser** if it doesn't exist
yet — **`admin@example.com` / `admin123`** — usable at `/admin/` and
`/api-auth/login/`. The password is deliberately weak (for local testing
only; no password validation is applied). Existing users are never
modified, so a password you change is kept across runs. Override with
`DEV_SUPERUSER_EMAIL` / `DEV_SUPERUSER_PASSWORD`.

### Seed data

`make run` also loads development seed data (skip with `SEED=0`; run it on
its own with `make seed`, or `make seed-reset` to delete and recreate it).
Players all use the password **`player123`**:

| Login | Scenario to test |
|---|---|
| `ana@example.com` | No memberships → sees only open clubs (Ibirapuera, Lisboa) |
| `bruno@example.com` | Member of Jardins → also sees its courts |
| `carla@example.com` | Member of Jardins **and** Morumbi → sees all four active clubs |
| `diego@example.com` | **Suspended** at Jardins → open clubs only |
| `elena@example.com` | **Expired** at Morumbi → open clubs only |
| `manager@example.com` | Club admin (`ADMIN` role) of every seed club — try the schedule / override endpoints |
| `admin@example.com` / `admin123` | Superuser (admin) |

| Club | Access | Highlights |
|---|---|---|
| Ibirapuera Tennis Center | open | 5 courts: clay w/ lighting, indoor hard, hard without lighting, one inactive |
| Jardins Country Club | members-only | clay, grass, indoor synthetic; 06:00–23:00 |
| Morumbi Racquet Club | members-only | 90-minute slot grid, closed on Sundays |
| Lisboa Tennis Club | open | `Europe/Lisbon` timezone (slot offsets differ) |
| Old Town Courts | open, **inactive** | never listed |

The schedule is generated relative to **today**: confirmed, pending,
cancelled (doesn't block) and completed bookings, doubles with guests, a
lesson, a member bringing a non-member guest, one-time overrides (maintenance,
a closed day, and a junior tournament on two Ibirapuera courts in four days),
a booking
starting within the hour (picked up by the match-reminder task) and a stale
payment hold (expired by the next expiry-task run). Re-running keeps your
edits to clubs/courts/users and your own reservations, and regenerates only
the seed schedule; `make seed-reset` wipes all reservations on seed courts.
The command (`python manage.py seed_dev_data`) refuses to run with
`DEBUG=False` unless `--force` is given.

Press **Ctrl-C** to stop everything: services get a graceful shutdown
(Celery's warm shutdown included), then Redis and PostgreSQL are stopped —
unless they were already running before `make run`, in which case they are
left as they were. If any service crashes, the rest of the stack is stopped
too. Use `make stop` to stop a database/Redis left running (e.g. after
`make db-start`).

- No `redis-server` installed → `make run` still starts PostgreSQL + Django
  and prints a warning; Celery worker/beat are skipped.
- `DATABASE_URL`, `REDIS_URL` and `CELERY_*` are exported for the local
  services, overriding the docker hostnames in `.env` (no edits needed).
- Overrides: `RUN_ADDR=0.0.0.0:8000` (listen on all interfaces — also add
  the host to `DJANGO_ALLOWED_HOSTS`), `REDIS_PORT`, `PG_PORT`,
  `REDIS_BIN=/path/to/redis-server` (`valkey-server` also works).
- As root, PostgreSQL runs as the dedicated `courtcontrol-pg` user (see
  Testing); Django, Celery and Redis run as the invoking user (Celery prints
  a superuser warning, which is expected).

## API

REST endpoints live under `/api/v1/` (DRF; session auth, authenticated users only).

### Authentication (session)

Any user — not only staff — can log in at **`/api-auth/login/`**. In a browser,
open an endpoint (e.g. http://localhost:8000/api/v1/courts/) and use the
**Log in** link of DRF's browsable API. With curl, keep the session in a
cookie jar (the login form field is `username`, but it takes the email):

```bash
BASE=http://localhost:8000
curl -s -c cookies.txt "$BASE/api-auth/login/" -o /dev/null          # get csrftoken cookie
CSRF=$(awk '$6 == "csrftoken" {print $7}' cookies.txt)
curl -s -b cookies.txt -c cookies.txt -X POST "$BASE/api-auth/login/" \
  -H "Referer: $BASE/api-auth/login/" \
  --data-urlencode "csrfmiddlewaretoken=$CSRF" \
  --data-urlencode "username=player@example.com" \
  --data-urlencode "password=<password>" \
  -o /dev/null -w "HTTP %{http_code}\n"                               # 302 = logged in

curl -s -b cookies.txt -G "$BASE/api/v1/courts/" \
  --data-urlencode "date=2030-01-08" \
  --data-urlencode "time_from=18:00" --data-urlencode "time_to=21:00"
```

GET requests only need the cookie; POST/PUT/DELETE (including
`/api-auth/logout/`) also need the `X-CSRFToken` header or form field.

### `GET /api/v1/courts/` — list bookable courts with available slots

Returns active courts the user may book — courts of **open** clubs plus
**members-only** clubs where the user has an active membership — each with
its free time slots.

| Param | Example | Meaning |
|---|---|---|
| `surface_type` | `CLAY,HARD` | Court type; comma-separated = any of |
| `is_indoor` | `true` / `false` | Indoor / outdoor |
| `club` | `<uuid>,<uuid>` | Club ids; comma-separated = any of |
| `date` | `2030-01-08` | A single day |
| `date_from` + `date_to` | `2030-01-08` / `2030-01-14` | Inclusive range, **max 7 days** |
| `time_from` / `time_to` | `18:00` / `21:00` | Only slots fully inside this time of day (each day) |

Window resolution: no date/time params → **today**; `date` → all free slots
that day; date + time → only slots inside the time window. Dates/times are
wall-clock in each club's timezone; past dates are rejected (`400`).

Different params are AND-combined. When any date/time param is given,
courts with **no** free slot in that window are omitted — e.g. a court
taken by a tournament at the searched time doesn't match the search;
without them, all matching courts are listed with today's slots (possibly
empty).

Each court lists:

- `available_slots` — its slots (from its schedule — see *Schedules* below)
  that haven't started and don't overlap an active reservation;
- `notices` — the one-time overrides in the window, with their `type`
  (`MAINTENANCE`, `WEATHER`, `TOURNAMENT`, `EVENT`, `HOLIDAY`, `OTHER`) and
  `notes`, telling members why a day differs from the usual schedule (e.g.
  no morning slots because of a tournament).

```json
{
  "count": 1, "next": null, "previous": null,
  "results": [{
    "id": "…", "name": "Court 1", "surface_type": "CLAY", "is_indoor": false,
    "has_lighting": true, "hourly_rate": "100.00", "lighting_fee": "20.00",
    "club": {"id": "…", "name": "Smoke Club", "slug": "smoke-club",
             "timezone": "America/Sao_Paulo", "access_type": "OPEN"},
    "available_slots": [
      {"start": "2030-01-08T08:00:00-03:00", "end": "2030-01-08T09:00:00-03:00"}
    ],
    "notices": [
      {"date": "2030-01-08", "type": "TOURNAMENT", "notes": "Junior club championship until 14:00"}
    ]
  }]
}
```

### `POST /api/v1/clubs/` — register a club

Registers a club from just its **name** and **address** (both required,
non-blank). Everything else starts at its default — `timezone`
`America/Sao_Paulo`, `access_type` `OPEN`, `cancellation_window_hours` 24,
active — and is set later by editing the club; other fields sent in the
body are ignored. Names needn't be unique, but slugs are: the slug is the
name (`tennis-club`); if taken, the name + address
(`tennis-club-rua-augusta-100`); if that's taken too, the name + address + a
number (`tennis-club-rua-augusta-100-2`, `-3`, …).

```json
{"name": "Ibirapuera Tennis Center", "address": "Av. Pedro Álvares Cabral, São Paulo"}
```

| Status | When |
|---|---|
| `201` | Registered — returns the club (`id`, `name`, `slug`, `address`, `timezone`, `access_type`, `cancellation_window_hours`, `is_active`, `created_at`) |
| `400` | `name` or `address` missing/blank, or `name` longer than 255 characters |

```bash
curl -s -b cookies.txt -X POST "$BASE/api/v1/clubs/" \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: $(awk '$6 == "csrftoken" {print $7}' cookies.txt)" \
  -H "Referer: $BASE/" \
  -d '{"name": "Ibirapuera Tennis Center", "address": "Av. Pedro Álvares Cabral, São Paulo"}'
```

### `GET` / `PATCH /api/v1/clubs/<uuid>/` — view / edit a club

`GET` returns the club (same shape as registration, plus `updated_at`); any
logged-in user may fetch it. `PATCH` edits it — **staff only** (there is no
per-club "owner" yet; see note below). Every setting can be changed **except
`name` and `address`** — changing those is a support request, rejected with
a `400` pointing at support. Send any subset of the editable fields:

```json
{"timezone": "Europe/Lisbon", "access_type": "MEMBERS_ONLY",
 "cancellation_window_hours": 12, "is_active": true}
```

| Status | When |
|---|---|
| `200` | Updated — returns the full club |
| `400` | Invalid value (e.g. an unknown timezone, a negative `cancellation_window_hours`), or `name`/`address` included |
| `403` | Not logged in, or logged in but not staff |
| `404` | Unknown club |

```bash
curl -s -b cookies.txt -X PATCH "$BASE/api/v1/clubs/<uuid>/" \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: $(awk '$6 == "csrftoken" {print $7}' cookies.txt)" \
  -H "Referer: $BASE/" \
  -d '{"cancellation_window_hours": 12}'
```

Changing a club's `timezone` only affects how *new* wall-clock times are
interpreted — existing reservations stay at the same instant, so their
displayed local time shifts by the zone change. Changing `access_type` to
`MEMBERS_ONLY` doesn't retroactively affect existing reservations either
(only new listing/booking checks). Neither is blocked; both are a
deliberate choice left to whoever edits the club.

### Schedules: day templates, weekly versions, one-time overrides

A court's bookable slots come from three pieces (all times wall-clock in the
club's timezone):

- **Day templates** — a club's named slot lists, e.g. *Workdays* or
  *Weekend*: slots of any length, gaps allowed, **never overlapping**. A slot
  ending at midnight is written `"24:00"` (stored as `23:59:59.999999`).
- **Weekly schedule versions** — which template each weekday of a court uses,
  valid from `valid_from` until `valid_until` (empty = no end). A court's
  versions never overlap; together they're the **history and plan** of its
  schedule.
- **One-time overrides** — a court + a date → another template, or closed,
  with a `type` (`MAINTENANCE`, `WEATHER`, `TOURNAMENT`, `EVENT`, `HOLIDAY`,
  `OTHER`) and `notes` members see. This is also how a court is made
  unbookable for a reason: *"Junior club championship until 14:00"* is an
  override whose template only has the afternoon slots; *"Flooded"* is a
  closed one. Members see the type and notes in the courts listing
  (`notices`) and in booking errors.

Slots of a court on a date = the override for that date if any, else the
version covering the date → the template of that weekday (no template =
closed). Availability and booking validation both use this.

**History** (per club-local day): anything that applied to a day *before
today* never changes — versions can only start today or later (the one in
effect is cut short; with `valid_until`, the interrupted one resumes
afterwards), past versions and overrides can't be deleted, and a template's
slots freeze once it has applied to a past day (schedule a new template
instead). Today is still editable.

**Future bookings**: a change that would leave an existing future booking
(one that fitted the old slots) no longer fitting is refused with **`409`**
listing those bookings — pick a later start date, or resend with
`"force": true` (`?force=true` on `DELETE`) to **cancel** them; the response
lists `cancelled_reservations`. Started or past bookings are never touched.
(Players aren't notified yet — there's no cancellation notification.)

Only **club admins** (a current `ADMIN` `ClubStaffRole` at the club) and site
staff can use these endpoints; for anyone else the club/court is reported as
not existing (`404`). In the Django admin they're read-only.

| Endpoint | |
|---|---|
| `GET`/`POST /api/v1/clubs/<club>/day-templates/` | List / create `{"name": "Workdays", "slots": [{"start": "07:00", "end": "08:00"}, …]}` |
| `GET`/`PATCH`/`DELETE /api/v1/clubs/<club>/day-templates/<id>/` | Rename anytime; change `slots` until the template is `locked` (`409`); delete if unused (`409`) |
| `GET`/`POST /api/v1/courts/<court>/schedules/` | All versions (`state`: past/current/planned) / add a version (below) |
| `GET`/`DELETE /api/v1/courts/<court>/schedules/<id>/` | Delete a version starting today or later; the previous one extends over its period |
| `POST /api/v1/courts/<court>/schedules/copy/` | `{"courts": [<id>, …], "valid_from": "…", "valid_until": null}` — the source's weekly schedule on `valid_from` (all 7 days) becomes a new version on each target; same club; all-or-nothing |
| `GET /api/v1/courts/<court>/overrides/?date_from=&date_to=` | From today on by default |
| `GET`/`PUT`/`DELETE /api/v1/courts/<court>/overrides/<YYYY-MM-DD>/` | `{"day_template": <id> \| null, "type": "TOURNAMENT", "notes": "Junior club championship until 14:00"}`; today or later |

Adding a version — weekdays are grouped by template; weekdays not sent keep
the template they have on `valid_from`, `null` closes them; without
`valid_until` it lasts until the next planned version:

```json
{"valid_from": "2030-03-01",
 "days": [{"weekdays": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"], "day_template": "<workdays id>"},
          {"weekdays": ["SATURDAY", "SUNDAY"], "day_template": "<weekend id>"}],
 "force": false}
```

A `409` for bookings in the way:

```json
{"detail": "1 future booking(s) wouldn't fit the new schedule.",
 "conflicting_reservations": [{"id": "…", "court": "…", "court_name": "Court 1",
   "start": "2030-03-07T18:00:00-03:00", "end": "2030-03-07T19:00:00-03:00",
   "primary_player": "ana@example.com", "status": "CONFIRMED"}],
 "hint": "Pick a later start date, or send force=true to cancel these bookings."}
```

### `POST /api/v1/reservations/` — book a court

Books a court for the logged-in user. `date`, `start_time` and `end_time`
are wall-clock values in the **club's timezone** (same as the listing
filters); the time must be free and match the court's weekly slots — i.e.
exactly what `GET /api/v1/courts/` returns as `available_slots`. Several
consecutive slots can be booked at once (e.g. 18:00–20:00): the booking
starts when a slot starts, ends when a slot ends, and can't span a gap
between slots.

```json
{
  "court": "<court uuid>",
  "date": "2030-01-08",
  "start_time": "18:00",
  "end_time": "19:00",
  "booking_type": "SINGLES",          // SINGLES (default) | DOUBLES | LESSON
  "includes_lighting": true,          // optional; court must have lighting
  "guests": ["friend@example.com"],   // optional; registered players, max 3
  "notes": "optional"
}
```

Payment isn't implemented yet, so bookings are **`CONFIRMED`
immediately** (and the confirmation task is enqueued after the transaction
commits). Responses:

| Status | When |
|---|---|
| `201` | Booked — returns the reservation (`start`/`end` ISO-8601 in the club timezone, `total_price`, `guests`, …) |
| `400` | Invalid input, or the slot isn't bookable (past, not matching the court's slots — if a one-time override changed that day, the error says why, e.g. "… — Tournament: Junior club championship until 14:00." —, lighting unavailable, unknown guest). A court you can't book — including members-only courts when you're not a member — is reported as not existing. |
| `409` | The slot is already taken (also when two people book it at the same moment) |

With curl (session cookie from the login above; POSTs need the CSRF token):

```bash
curl -s -b cookies.txt -X POST "$BASE/api/v1/reservations/" \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: $(awk '$6 == "csrftoken" {print $7}' cookies.txt)" \
  -H "Referer: $BASE/" \
  -d '{"court": "<court uuid>", "date": "2030-01-08", "start_time": "18:00", "end_time": "19:00"}'
```

### `GET /api/v1/reservations/` — my bookings

Lists the logged-in user's reservations — the ones they booked **and** the
ones they're a guest in (`role`: `owner` | `guest`). Paginated like the
courts listing. Different params are AND-combined; comma-separated values
within one param are OR-ed. Dates/times are wall-clock in each club's
timezone.

| Param | Example | Meaning |
|---|---|---|
| `state` | `upcoming,pending_payment` | Lifecycle state (see below) |
| `date` | `2030-01-08` | Bookings on that day |
| `date_from` / `date_to` | `2030-01-01` | Inclusive; either side may be omitted (no range limit) |
| `time_from` / `time_to` | `18:00` / `21:00` | Starts at/after, ends at/before |
| `player` | `ana@example.com` | Bookings where that player plays (booker or guest) |
| `role` | `owner` \| `guest` | What you are in the booking |
| `club` / `court` | `<uuid>,<uuid>` | Where |
| `booking_type` | `SINGLES,DOUBLES` | Type |
| `ordering` | `start`, `-start` (default), `created`, `price` | Sort |

`state` is derived from the stored `status` and the clock, because played
bookings are never flipped to `COMPLETED` automatically:

| `state` | Meaning |
|---|---|
| `upcoming` | Confirmed and not finished yet (includes a match in progress) |
| `pending_payment` | Held, awaiting payment |
| `completed` | Marked completed, or confirmed and already over |
| `cancelled` | Cancelled |
| `expired` | Payment hold expired (or its slot already passed) |

Each item has the same shape as the `POST` response, including `status`
(stored), `state`, `role`, `primary_player` and `guests`. Example — my next
matches, soonest first:

```bash
curl -s -b cookies.txt -G "$BASE/api/v1/reservations/" \
  --data-urlencode "state=upcoming" --data-urlencode "ordering=start"
```

Each reservation also includes `can_cancel` (for the requesting user) and
`cancellation_deadline` (ISO, club timezone) so clients can show the policy
before the user tries.

### `POST /api/v1/reservations/{id}/cancel/` — cancel a booking

Body (optional): `{"reason": "Rain forecast"}`. Cancelling keeps the
reservation (status `CANCELLED`, with `cancelled_at` and
`cancellation_reason`) and frees the slot for others; the booker and guests
are notified (Celery task, sent after commit).

**Policy — configured per club** (`cancellation_window_hours`, default **24**):

| Booking | Can be cancelled |
|---|---|
| Confirmed | until `start − cancellation_window_hours` (`0` = until the start) |
| Pending payment (unpaid hold) | any time before the start |
| Started, finished, expired, completed | never |

Only the player who made the booking can cancel it. Guests cannot.

| Status | When |
|---|---|
| `200` | Cancelled (also when repeating the call on a booking you already cancelled — safe to retry) |
| `403` | You're a guest in this booking |
| `404` | No such booking, or you're not in it |
| `409` | Can't be cancelled — the message explains why (e.g. deadline passed, with the club's policy and the deadline) |

```bash
curl -s -b cookies.txt -X POST "$BASE/api/v1/reservations/<uuid>/cancel/" \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: $(awk '$6 == "csrftoken" {print $7}' cookies.txt)" \
  -H "Referer: $BASE/" \
  -d '{"reason": "Rain forecast"}'
```

## Testing

Tests follow the standard Django layout — a `tests/` package per app,
discovered automatically by `manage.py test`:

```
apps/
├── common/tests/          # shared factories + project smoke tests
│   ├── factories.py       # plain-function model factories (no extra deps)
│   └── test_smoke.py
├── users/tests/
│   └── test_models.py     # User manager, email auth, PlayerProfile
├── courts/tests/
│   ├── test_models.py     # slugs, constraints, clean() validation
│   ├── test_club_registration.py  # POST /api/v1/clubs/, unique slugs
│   ├── test_club_edit.py          # GET/PATCH /api/v1/clubs/<uuid>/
│   ├── test_club_staff_role.py    # ClubStaffRole constraints/history, current(), admin
│   ├── test_schedule_services.py  # timeline/history, overrides, templates, copy, booking conflicts/force
│   ├── test_schedule_api.py       # schedule endpoints, 409/force, 24:00, permissions
│   ├── test_override_notices.py   # overrides' type/notes: search, notices, booking errors
│   └── test_smoke_club_registration.py  # smoke command vs. a live server
└── bookings/tests/
    ├── test_models.py         # ExclusionConstraint overlap guard
    ├── test_services.py       # slot validation, pricing, lifecycle
    ├── test_cancellation.py   # cancel policy, service, POST …/cancel/
    └── test_tasks.py          # Celery task bodies (run synchronously)
config/
└── settings_test.py       # fast hashers, eager Celery, locmem cache
```

### Running tests locally (no Docker)

`make` + `scripts/test_env.sh` bootstrap everything: a `.venv` with
`requirements.txt`, and a **private, project-local PostgreSQL cluster**
in `.pgdata/` listening only on `127.0.0.1:54329` (it never touches a
system PostgreSQL service, and Redis is not needed).

Prerequisites: Python ≥ 3.10 (3.13 preferred) and PostgreSQL ≥ 14 server
binaries with `btree_gist` (contrib). If PostgreSQL is missing:

```bash
make install-postgres      # brew / apt / dnf / pacman / apk (uses sudo)
```

Then:

```bash
make test                                   # full suite (first run installs deps + inits DB)
make test TEST=apps.bookings                # one app
make test TEST=apps.bookings.tests.test_services.ValidateSlotTests ARGS="-v 2"
make test-fast                              # --keepdb --parallel
```

`make test` starts PostgreSQL if needed and stops it again afterwards
(set `KEEP_DB_RUNNING=1` to leave it up). Other targets:

| Target                 | Purpose                                            |
|------------------------|----------------------------------------------------|
| `make setup`           | Install deps and start the DB without running tests |
| `make smoke`           | Smoke-test the stack, starting it if needed (see below) |
| `make db-start` / `db-stop` / `db-status` | Manage the local cluster        |
| `make db-shell`        | `psql` into the local database                     |
| `make db-reset`        | Delete `.pgdata` (e.g. after upgrading PostgreSQL) |
| `make clean`           | Delete `.pgdata` and `.venv`                       |

Overrides: `PG_PORT=54330` (port clash), `PG_BIN=/path/to/pg/bin`
(non-standard install), `PYTHON=python3.13`.

**Running as root** (e.g. containers/sandboxes with only a root user, Linux):
PostgreSQL refuses to run as root, so on first use the script creates a
dedicated system user with `useradd` — `courtcontrol-pg`: no login shell,
no password, no sudo — and runs only the server binaries (`initdb`,
`pg_ctl`) as that user via `runuser` (or `setpriv`). Since a project under
`/root` isn't reachable by other users, the cluster lives in
`/var/lib/courtcontrol-testdb/<project>-<id>/` (owned by that user, mode
700) instead of `.pgdata/`. Everything else (venv, pip, tests) runs as
root. Override with `PG_OS_USER` / `PG_ROOT_BASE`; remove the user with
`userdel courtcontrol-pg` if you no longer need it.

The `.env` file can stay as-is: the script exports `DATABASE_URL`, which
takes precedence over `.env` values.

### Running tests with Docker Compose

```bash
docker compose exec web python manage.py test --settings=config.settings_test
docker compose exec web python manage.py test apps.bookings --settings=config.settings_test
```

### Smoke test against a running instance

`smoke_club_registration` exercises the club registration and editing flow
against a **running** server over real HTTP — health check, session login
with CSRF, `POST /api/v1/clubs/` (defaults, ignored extra fields, the name →
name + address → number slug rule, `400`s, `403` when anonymous/logged out),
`PATCH /api/v1/clubs/<uuid>/` (403 for a non-staff user, a staff edit
applied and stored, name/address rejected with the support message) — and
checks what the server stored by reading the database.

```bash
make smoke          # exit code 0 = passed (ARGS="-v 0" for quiet)
```

If no server is listening on `RUN_ADDR`, `make smoke` first starts the stack
exactly like `make run` (but with `SEED=0` — the smoke test doesn't need seed
data; set `SEED=1` to load it), waits for it (up to `SMOKE_START_TIMEOUT`,
default 300 s), runs the smoke test and then stops what it started —
services, and PostgreSQL/Redis if they weren't running before (kept with
`KEEP_DB_RUNNING=1`). The stack's output goes to a temporary log, deleted
when the test passes and kept (path printed) when it fails. With a stack
already running (`make run` in another terminal), it's used and left running.
Setting `SMOKE_BASE_URL` targets that server instead and never starts one.

With Docker Compose (runs inside the `web` container, next to the server):

```bash
docker compose exec web python manage.py smoke_club_registration --base-url http://localhost:8000
```

It must use the **same database as the server** (same `DATABASE_URL`): it
creates its test user there and cleans up there. It only ever deletes what
it owns — the user `smoke-test@courtcontrol.invalid` (new random password
each run), clubs named exactly `Smoke Test Club (courtcontrol-smoke)`, and
that user's sessions. Cleanup runs **first** (removing leftovers of a run
that was killed mid-way) and **again at the end**, also when a check fails.
One run at a time per database.

It refuses to run when `DEBUG=False` unless `--force` is given (e.g. a
post-deploy check on staging). Other options: `--base-url` (default
`http://127.0.0.1:8000`; `make smoke` uses `RUN_ADDR` or `SMOKE_BASE_URL`),
`--timeout`. Ctrl-C still cleans up (exit code 130). The command itself is tested against a live server thread in
`apps/courts/tests/test_smoke_club_registration.py`.

Conventions:

- New test modules must be named `test_*.py` to be auto-discovered.
- Create fixture data through `apps/common/tests/factories.py` instead of
  calling `objects.create(...)` with boilerplate in each test.
- Unit-test the service layer with Celery dispatch mocked
  (`mock.patch("apps.bookings.services.send_booking_confirmation_task")`);
  test task bodies directly in `test_tasks.py`.

## Domain apps

- `apps.users` — email-based `User` + `PlayerProfile`
- `apps.courts` — `Club` (open or members-only), `ClubMembership` (user ↔ club, many-to-many), `Court`, schedules (below); access rules in `apps/courts/services.py`
- `apps.courts` schedules — `DayTemplate` + `DayTemplateSlot` (a club's named slot lists; GiST exclusion constraint against overlapping slots), `CourtSchedule` (a court's weekly-schedule versions with `valid_from`/`valid_until`; exclusion constraint against overlapping periods) + `CourtScheduleDay` (weekday → template), `ScheduleOverride` (court + date → template or closed). Resolution in `apps/courts/schedule.py`, changes (history + booking rules) in `apps/courts/services.py`.
- `apps.courts.ClubStaffRole` — the roles a user holds in running a club (membership isn't one — it stays in `ClubMembership`):
  - `ADMIN` (*Club admin*) — full control over the club: courts, calendar blocks, overriding bookings, …
  - `GROUNDSKEEPER` — court upkeep: sees the schedule, marks courts under repair or unplayable (weather, environment).
  - `FRONT_DESK` (*Front desk*) — on-site desk: books for in-person players, records no-shows, warns players who break the rules.

  These describe responsibilities only — **the permissions they grant aren't enforced yet** (club edits are still limited to site-wide staff, `is_staff`). Each record has `start_date`, optional `end_date` (the role's last day, inclusive; empty = no end) and `is_active`. Records are kept as **history**: at most **one *active* record per user + club + role** (a partial unique index, `WHERE is_active`), any number of inactive ones. To re-grant a role, deactivate the current record and add a new one (on the club's admin page this can be done in one save). A user can hold several roles at the same club at once. The uniqueness rule is on `is_active` only: a record whose `end_date` has passed still blocks a new one until it's deactivated. The database also enforces `end_date >= start_date` and the allowed roles. `ClubStaffRole.objects.current(on=date)` / `role.is_current(on)` = in effect that day (default: today in the project timezone).
- `apps.bookings` — `Reservation` with PostgreSQL `ExclusionConstraint` preventing overlapping active bookings

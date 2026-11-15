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
| `manager@example.com` | Club admin (`ADMIN` role) of every seed club — try the schedule / override / booking rule endpoints |
| `admin@example.com` / `admin123` | Superuser (admin) |

| Club | Access | Highlights |
|---|---|---|
| Ibirapuera Tennis Center | open | 5 courts: clay w/ lighting, indoor hard, hard without lighting (a **singles court**), one inactive |
| Jardins Country Club | members-only | clay, grass, indoor synthetic; 06:00–23:00 |
| Morumbi Racquet Club | members-only | 90-minute slot grid, closed on Sundays |
| Lisboa Tennis Club | open | `Europe/Lisbon` timezone (slot offsets differ) |
| Old Town Courts | open, **inactive** | never listed |

Example **booking rules** (Ana, Carla and Elena are female; Bruno and Diego male):

| Club | Rule |
|---|---|
| Ibirapuera | *Women's evening* — courts 1–2, the 19:00 and 20:00 slots: women only |
| Jardins | *Prime time: 1 a week* — the 18:00–22:00 slots: one prime-time booking per week (members' guest bookings count too) |
| Morumbi | *One upcoming booking* — one upcoming booking at the club per player (members' guest bookings count too) |

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
  and prints a warning; Celery worker/beat are skipped, and tasks (e.g.
  booking notifications) run **inline** in the web process
  (`CELERY_TASK_ALWAYS_EAGER=True`) — without it every booking blocked while
  Celery retried Redis — and the cache is in-process (`CACHE_URL=locmem://`,
  used by the login rate limits). Periodic tasks (hold expiry, reminders,
  code cleanup) don't run.
- `DATABASE_URL`, `REDIS_URL` and `CELERY_*` are exported for the local
  services, overriding the docker hostnames in `.env` (no edits needed).
- Overrides: `RUN_ADDR=0.0.0.0:8000` (listen on all interfaces — also add
  the host to `DJANGO_ALLOWED_HOSTS`), `REDIS_PORT`, `PG_PORT`,
  `REDIS_BIN=/path/to/redis-server` (`valkey-server` also works).
- As root, PostgreSQL runs as the dedicated `courtcontrol-pg` user (see
  Testing); Django, Celery and Redis run as the invoking user (Celery prints
  a superuser warning, which is expected).

## API

The API is documented in **[`docs/openapi.yaml`](docs/openapi.yaml)** (OpenAPI 3):
every endpoint with its parameters, request and response bodies, status
codes and examples, plus an overview — authentication (session login + CSRF,
with a curl walkthrough), conventions (players, times, errors, pagination),
schedules and booking rules. Open it in any OpenAPI viewer — e.g. import it
in [Swagger Editor](https://editor.swagger.io/), Redoc, Postman or Insomnia.
In a browser, DRF's browsable API also works: log in at
http://localhost:8000/api-auth/login/ and open http://localhost:8000/api/v1/courts/.

The schema is **generated from the code** (drf-spectacular): the views,
serializers and django-filter filtersets, plus the documentation next to
them — each app's `openapi.py` (summaries, descriptions, status codes,
examples) and `config/openapi_description.md` (the overview). After changing
any of them, regenerate it:

```bash
make openapi        # writes docs/openapi.yaml; fails on any schema warning
```

`apps/common/tests/test_openapi.py` fails when the committed file is out of
date or an endpoint is undocumented.

## Testing

Tests follow the standard Django layout — a `tests/` package per app,
discovered automatically by `manage.py test` — and run with
`config/settings_test.py` (fast hashers, eager Celery, in-memory cache and
file storage).

### Running tests locally (no Docker)

`make` + `scripts/test_env.sh` bootstrap everything: a `.venv` with
`requirements.txt`, and a **private, project-local PostgreSQL cluster**
in `.pgdata/` listening only on `127.0.0.1:54329` (it never touches a
system PostgreSQL service, and Redis is not needed).

Prerequisites: Python ≥ 3.10 (3.13 preferred) and PostgreSQL ≥ 14 server
binaries with the contrib extensions `btree_gist`, `pg_trgm` and `unaccent`
(created by the migrations; all three are "trusted", so the database owner
can create them without superuser rights). If PostgreSQL is missing:

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

`make test-fast` drops the parallel workers' database clones
(`test_courtcontrol_1`, `_2`, …) before running: with `--keepdb` Django
migrates the kept test database but would reuse stale clones, so tests
failed after every new migration (often as "cannot pickle 'traceback'
object").

`make test` starts PostgreSQL if needed and stops it again afterwards
(set `KEEP_DB_RUNNING=1` to leave it up). Other targets:

| Target                 | Purpose                                            |
|------------------------|----------------------------------------------------|
| `make setup`           | Install deps and start the DB without running tests |
| `make smoke`           | Smoke-test the stack, starting it if needed (see below) |
| `make openapi`         | Regenerate the OpenAPI schema `docs/openapi.yaml` from the views |
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

`make smoke` runs two smoke commands against a **running** server over real
HTTP (session login with CSRF), each checking what the server stored by
reading the same database:

- `smoke_club_registration` — club registration, review and editing:
  health check, `POST /api/v1/clubs/` (pending, the registrant is its club
  admin, defaults, ignored extra fields, the name → name + address → number
  slug rule, `400`s, `403` when anonymous/logged out), a pending club hidden
  from another user (`404`), `PATCH /api/v1/clubs/<uuid>/` by its admin
  (applied and stored; name/address rejected with the support message),
  staff `GET /clubs/pending/`, `POST …/reject/` and `…/approve/`, then the
  approved club visible to others but not editable by them (`403`).
- `smoke_api` — the happy path of the club set-up, search, profile,
  booking-rule, booking and account endpoints, with a smoke staff member,
  club admin, booker and guest: the admin registers a club (staff approve
  it), manages its courts (`POST` / `GET` / `PATCH` / `DELETE
  /clubs/<club>/courts/`; one a singles court) and adds a day template and
  weekly schedules, then `GET /clubs/search/` (by address),
  `GET /players/search/` (no emails), `PUT` + `DELETE /me/profile/picture/`
  (a multipart upload; checks the stored file), `GET /booking-rule-kinds/`,
  booking rules `POST` / `GET` list / `GET` / `PATCH` / `DELETE`,
  `GET /courts/` (`is_singles_only`, rule ids on slots), `POST
  /reservations/` with the guest **by player id** (no emails back), `GET
  /reservations/?player=<id>` as the guest, and `DELETE
  /day-templates/<id>/?force=true` on a template linked to a rule (the rule
  is deactivated).

```bash
make smoke          # both; exit code 0 = both passed (ARGS="-v 0" for quiet)
```

Both run even if the first fails.

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
docker compose exec web python manage.py smoke_api --base-url http://localhost:8000
```

They must use the **same database as the server** (same `DATABASE_URL`) —
and `smoke_api` the same file storage: they create their data there and
clean up there. They only ever delete what they own:
`smoke_club_registration` — the users `smoke-test@courtcontrol.invalid`
and `smoke-test-other@courtcontrol.invalid`, clubs named exactly `Smoke
Test Club (courtcontrol-smoke)`, those users' sessions; `smoke_api` — the users `smoke-api-{staff,admin,booker,guest,signup}@courtcontrol.invalid`,
their sessions and picture files, and the club named exactly `Smoke API
Club (courtcontrol-smoke)` with its courts, schedules, rules and the
bookings on its courts. Users get a new random password each run. Cleanup
runs **first** (removing leftovers of a run that was killed mid-way) and
**again at the end**, also when a check fails. One run at a time per
database.

It refuses to run when `DEBUG=False` unless `--force` is given (e.g. a
post-deploy check on staging). Other options: `--base-url` (default
`http://127.0.0.1:8000`; `make smoke` uses `RUN_ADDR` or `SMOKE_BASE_URL`),
`--timeout`. Ctrl-C still cleans up (exit code 130). The commands themselves are tested against a live server thread in
`apps/courts/tests/test_smoke_club_registration.py` and
`apps/common/tests/test_smoke_api.py`.

Conventions:

- New test modules must be named `test_*.py` to be auto-discovered.
- Create fixture data through `apps/common/tests/factories.py` instead of
  calling `objects.create(...)` with boilerplate in each test.
- Unit-test the service layer with Celery dispatch mocked
  (`mock.patch("apps.bookings.services.send_booking_confirmation_task")`);
  test task bodies directly in `test_tasks.py`.

## Domain apps

- `apps.users` — email-based `User` + `PlayerProfile` (1:1 with the user; guests in bookings are users too, so every player has one); player search (name + location) in `apps/users/search.py`, shared search helpers in `apps/common/search.py`:
  - **Name** — the user's `first_name` / `last_name` (`profile.full_name`); not duplicated on the profile.
  - `skill_level` — NTRP rating (the player level).
  - `birth_date` — optional; must not be in the future (`clean()`) nor before 1900 (DB check). `profile.age(on=None)` = whole years on a date (default: today).
  - `gender` — optional, self-declared: `FEMALE`, `MALE`, `OTHER`, `PREFER_NOT_TO_SAY`; empty = not answered yet. DB check constraint on the allowed values.
  - `dominant_hand` (`RIGHT` / `LEFT`), `backhand_style` (`ONE_HANDED` / `TWO_HANDED`), `preferred_surface` (the court surfaces: `CLAY`, `HARD`, `GRASS`, `SYNTHETIC`) — optional; empty = not informed. Allowed values are also enforced by DB check constraints.
  - `racket` — free text, brand and model (e.g. *Babolat Pure Aero 98*).
  - `location` — free text, where the player plays: neighbourhood, city, state, country (e.g. *Pinheiros, São Paulo - SP, Brasil*). Optional; shown to other players and searchable, so city level — no street address.
  - `search_name` / `search_document` — normalized search copies maintained by database triggers (see *Implementation notes*); not edited in Python.
  - `picture_key` — the picture's **key** in the `player_pictures` file storage (local disk in development; S3, a CDN, … in production), set / cleared by `PUT` / `DELETE /api/v1/me/profile/picture/`, up to 1024 chars (S3's limit); not a URL — `clean()` rejects URLs and absolute paths, so switching storage/CDN only changes how URLs are built, not the data.
  - Club membership is **not** on the profile — it's per club, in `ClubMembership` (status, tier, expiry). The profile's old `is_member` / `membership_tier` fields were removed (migration `users.0003`).
- `apps.courts` — `Club` (open or members-only; self-registered clubs await approval — `approval_status`, see *Implementation notes*), `ClubMembership` (user ↔ club, many-to-many), `Court` (`is_singles_only`: no doubles, at most 2 players), schedules (below); access rules in `apps/courts/services.py`; club search (name + address) in `apps/courts/search.py`
- `apps.courts` schedules — `DayTemplate` + `DayTemplateSlot` (a club's named slot lists; GiST exclusion constraint against overlapping slots), `CourtSchedule` (a court's weekly-schedule versions with `valid_from`/`valid_until`; exclusion constraint against overlapping periods) + `CourtScheduleDay` (weekday → template), `ScheduleOverride` (court + date → template or closed). Resolution in `apps/courts/schedule.py`, changes (history + booking rules) in `apps/courts/services.py`.
- `apps.courts.ClubStaffRole` — the roles a user holds in running a club (membership isn't one — it stays in `ClubMembership`):
  - `ADMIN` (*Club admin*) — full control over the club: courts, calendar blocks, overriding bookings, …
  - `GROUNDSKEEPER` — court upkeep: sees the schedule, marks courts under repair or unplayable (weather, environment).
  - `FRONT_DESK` (*Front desk*) — on-site desk: books for in-person players, records no-shows, warns players who break the rules.

  These describe responsibilities only — **the permissions they grant aren't enforced yet** (club edits are still limited to site-wide staff, `is_staff`). Each record has `start_date`, optional `end_date` (the role's last day, inclusive; empty = no end) and `is_active`. Records are kept as **history**: at most **one *active* record per user + club + role** (a partial unique index, `WHERE is_active`), any number of inactive ones. To re-grant a role, deactivate the current record and add a new one (on the club's admin page this can be done in one save). A user can hold several roles at the same club at once. The uniqueness rule is on `is_active` only: a record whose `end_date` has passed still blocks a new one until it's deactivated. The database also enforces `end_date >= start_date` and the allowed roles. `ClubStaffRole.objects.current(on=date)` / `role.is_current(on)` = in effect that day (default: today in the project timezone).
- `apps.bookings` — `Reservation` with PostgreSQL `ExclusionConstraint` preventing overlapping active bookings; `BookingRule` (club conditions on bookings: links to club / courts / slot configurations / slots + kind) with the kinds in `apps/bookings/rules.py`

## Implementation notes

- **Club registration** (`courts.services.register_club`): any logged-in
  user can register a club; it's `PENDING` — `Club.objects.active()` and
  `Court.objects.bookable()` (search, listings, booking) only include
  approved clubs — and its registrant gets the club-admin role, so they can
  set it up. Site staff approve / reject it via the API
  (`/api/v1/clubs/pending/`, `…/approve/`, `…/reject/`) or the Django admin
  (an *Approve* action; reject in the club's form, with a reason); the
  registrant is emailed. `CLUB_REGISTRATION_MAX_PENDING` (3) pending clubs per
  user; `CLUB_REVIEW_NOTIFY_EMAILS` (comma-separated, empty by default) are
  emailed new registrations. Clubs created by staff tools (admin, seed) are
  approved by default. Club admins (the registrant included) manage the
  club's **courts** through the API (`/api/v1/clubs/<club>/courts/`), so a
  club can be set up entirely by its admin — club, courts, day templates,
  schedules, booking rules — and goes live once approved. Club edits are open
  to the club's admins and site staff.
- **Courts** (`courts.services.create_court` / `update_court` /
  `delete_court`): deactivating a court with future bookings is refused
  (`409` listing them) unless forced, which cancels them; a court with any
  bookings can't be deleted (they're history — deactivate it); one linked to
  booking rules is deleted only with `force` (see *Booking rules*).

- **Accounts** (`apps/users/auth.py`, endpoints under `/api/v1/auth/`): sign
  up, log in with a password or a 6-digit code sent by email (the password is
  optional), reset the password. **Emails** go through `EMAIL_URL`: by
  default the console backend — in development the codes are **printed in
  the server's output** (`make run`); in production set e.g.
  `EMAIL_URL=smtp+tls://user:password@smtp.example.com:587` and
  `DEFAULT_FROM_EMAIL`. Codes (`EmailChallenge`) are stored as a keyed hash,
  expire (`AUTH_CODE_TTL_MINUTES`, 10), allow `AUTH_CODE_MAX_ATTEMPTS` (5)
  tries, work once; a new one cancels the previous. Per email: one code a
  minute per purpose, five an hour (`AUTH_CODE_RESEND_SECONDS`,
  `AUTH_CODE_MAX_PER_HOUR`); per client IP: `AUTH_THROTTLE_RATE` (30/min)
  on the account endpoints. Answers never reveal whether an email has an
  account; a signup creates the account only when its code is verified, with
  that request's name and password (someone else's signup for your email
  can't set your password). The account endpoints check CSRF even before
  login. Expired codes are purged daily (Celery beat).

- **No emails in responses.** Players appear as `{"id", "first_name",
  "last_name"}` everywhere; `apps/common/tests/test_no_email_in_responses.py`
  walks every `/api/v1/` endpoint and fails if a response contains an email
  — or if a new endpoint isn't walked.
- **Club and player search** (PostgreSQL `pg_trgm` + `unaccent`): the club's
  name + address and the player's name + location have normalized copies
  (accents removed, lower case) — a stored generated column on `Club`
  (courts migration `0005`), trigger-maintained columns on `PlayerProfile`
  (the name lives in `users_user`, so a generated column can't combine them;
  users migration `0004`) — with a GIN trigram index each; every query word
  is a `LIKE '%word%'` the index serves, normalized by the same `IMMUTABLE`
  SQL function. Ranking is a cheap `CASE` on the stored name; pagination
  skips `COUNT(*)`: one query per request. The real columns keep their
  accents. Details: `apps/common/search.py`, `apps/courts/search.py`,
  `apps/users/search.py`. Measured with 200,000 rows: a specific club or
  player 1–15 ms; words matching tens of thousands 30–60 ms; a word most rows
  share (`brasil`, 116k players) ≈ 210 ms — cost grows with the matches.
  Typo tolerance would need a separate word list (fuzzy matching against
  whole documents measured 150–200 ms).
- **Booking rules** (`apps/bookings/rules.py`): to add a kind, subclass
  `RuleKind` — `key`, `label`, `description`, a `Params` serializer (validates
  and documents the params), `summary()` and `check()` — and decorate it with
  `@register`; params are JSON, so no migration, and the API, validation,
  the kinds endpoint and the listing pick it up. Count-based kinds set
  `locks_player = True`: the players they count (booker, member guests) are
  locked with transaction-level advisory locks (fixed order), so concurrent
  bookings can't both pass a limit. Links to courts / day templates / slots
  are `RESTRICT` join tables; forced deletions go through
  `unlink_from_rules()`, which deactivates rules that would otherwise widen.
  Courts: `DELETE /api/v1/clubs/<club>/courts/<id>/?force=true`, or the
  courts' Django admin *Delete selected courts, unlinking booking rules
  (force)* action.
- **Profile pictures** (`apps/users/services.py`): saved under a new key per
  upload (`players/<user id>/picture-<random>.<ext>`) in the
  `player_pictures` storage (`settings.STORAGES`) — local disk (`MEDIA_ROOT`,
  served at `/media/` only with `DEBUG`) until pointed at S3 / a CDN (e.g.
  django-storages); the key stays valid either way. The size limit is
  `PLAYER_PICTURE_MAX_SIZE_KB` (env, default 2048). Images are decoded and
  re-encoded without metadata (EXIF incl. GPS, XMP, comments), orientation
  applied, ICC profile kept. The previous picture is deleted after commit (a
  failed delete is only logged).

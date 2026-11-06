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

Host ports (mapped to avoid local conflicts): Postgres `5433`, Redis `6380`, Django `8000`.

## Services

| Service         | Role                                      |
|-----------------|-------------------------------------------|
| `db`            | PostgreSQL 18                             |
| `redis`         | Broker + cache                            |
| `web`           | Django (`runserver` with hot-reload)      |
| `celery_worker` | Async task worker                         |
| `celery_beat`   | Periodic scheduler (hold expiry, reminders) |

## Domain apps

- `apps.users` — email-based `User` + `PlayerProfile`
- `apps.courts` — `Club`, `Court`, `OperatingHours`, `MaintenanceBlock`
- `apps.bookings` — `Reservation` with PostgreSQL `ExclusionConstraint` preventing overlapping active bookings

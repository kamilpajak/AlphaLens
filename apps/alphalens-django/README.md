# alphalens-django

Read/write API for thematic briefs. Replaces `alphalens/api/` (FastAPI).

## Quickstart

```bash
cd apps/alphalens-django
uv venv --python 3.13
uv pip install -e ".[dev]"
docker compose -f ../../deploy/docker/django-dev/compose.yaml up -d
cp .env.example .env
python manage.py migrate
python manage.py runserver
curl http://127.0.0.1:8000/healthz
```

## Layout

- `config/` — settings (base/dev/prod), urls, asgi
- `briefs/` — domain app (models, DRF api, parquet ingest)
- `auth_cf/` — Cloudflare Access JWT middleware
- `core/` — shared utilities

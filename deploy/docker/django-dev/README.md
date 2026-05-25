# django-dev infrastructure

Local Postgres 16 for the `alphalens-django` app.

```bash
docker compose -f deploy/docker/django-dev/compose.yaml up -d
docker compose -f deploy/docker/django-dev/compose.yaml ps
```

Default credentials match `.env.example`: user/pass `alphalens`, db `alphalens`, on `127.0.0.1:5432`. Data persists in the named volume `alphalens_pgdata`.

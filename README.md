# gridworks-fleet-index-service

Fleet Index Service (FIS): the authority-plane service the RabbitMQ broker
calls to authorize every connection and every topic publish, enforcing a
single authorized runtime instance per (GNodeId, run).

FIS runs colocated with the broker, on the same box, with its own small
Postgres. The broker reaches it over localhost; if FIS is unreachable, no
new connections are admitted and existing ones survive. That is deliberate:
the service fails closed.

## Scope

One FIS instance serves **one universe** — it holds a mirror of that
universe's grid-node-registry, and a registry serves exactly one universe.
**Runs** partition inside that scope: a run is one execution of time against
a universe, with its own broker vhost (`<universe>__<run>`) and its own lease
state, and the lease key is (principal, run). So one FIS serves every run its
broker hosts, and the same GNode identity may legitimately hold simultaneous
leases on different runs.

## Layout

```
src/fis/
  api.py        the HTTP surface the broker's auth backend calls
  cli.py        the `fis` console script
  settings.py   deploy config — one .env, one FIS_ prefix
  db/           engine + session factory
```

## Running it

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
docker compose up -d          # Postgres on localhost:5437
uv run alembic upgrade head   # apply the schema
uv run fis api
```

That runs against the **dev universe on this machine** — `d1`, local
Postgres — which is the default with no `.env` at all. A deployed box
overrides it: copy `template.env` to `.env` and set the universe and
database for that box. Alembic reads the same `FIS_DB_URL`, so there is no
second place to keep the connection string in sync.

`FIS_UNIVERSE` takes the bare universe token (`hw1`), not the broker vhost
(`hw1__2`, which is `<universe>__<run>`); the vhost form is rejected at boot.

## Development

`./ci.sh` runs the full gate (lint, format check, tests) — the same thing CI
runs, so a green run locally means a green push.

The schema tests need a real Postgres. They spin up an ephemeral one with
testcontainers by default, and **self-skip** when Docker can't provide it —
so watch the skip count, not just the pass count. To run them against the
compose Postgres instead (faster, and the reliable path when testcontainers
won't start):

```bash
FIS_TEST_PG_URL=postgresql+psycopg://fis:fispass@localhost:5437/fis uv run pytest
```

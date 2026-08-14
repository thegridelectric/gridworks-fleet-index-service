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
cp template.env .env      # then fill in FIS_UNIVERSE and FIS_DB_URL
uv run fis api
```

`FIS_UNIVERSE` and `FIS_DB_URL` are required with no defaults — a plausible
default would point the service at the wrong universe or the wrong database,
and neither failure is loud.

## Development

`./ci.sh` runs the full gate (lint, format check, tests) — the same thing CI
runs, so a green run locally means a green push.

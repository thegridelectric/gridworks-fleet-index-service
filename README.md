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

## Sema

Message boundaries in this repo are governed by
**[Sema](https://github.com/thegridelectric/sema)** — a vocabulary registry
for structured messages exchanged between independent systems. Sema defines
versioned types, enums, and formats expressed as JSON Schema; these act as
**boundary contracts**, making the structure and semantics of serialized
messages explicit and mechanically verifiable. Sema applies only at system
boundaries — it does not prescribe runtime architecture, database design,
or internal object models. Schema ids live under
`https://schemas.electricity.works`.

This repo works from a vendored snapshot at `src/fis/sema` — generated,
never hand-edit. Regenerate with `scripts/regen_sema_snapshot.sh` from
`src/fis/sema_seed_request.yaml`; see `src/fis/sema/README.md` for the
working rules.

## Layout

```
src/fis/
  api.py           the HTTP surface the broker's auth backend calls; its
                   lifespan runs the registry reconcile
  gate.py          the /auth verdicts
  mirror.py        applying registry snapshots to the mirror
  gnr_client.py    reading the grid-node-registry over its HTTP read façade
  rabbit_admin.py  closing connections through the broker's management API
  cli.py           the `fis` console script (`api`, `principal`)
  principals.py    minting and administering principals
  settings.py      deploy config — one .env, one FIS_ prefix
  db/              models, engine + session factory
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

FIS mirrors its universe's grid-node-registry over the registry's HTTP read
façade (`FIS_GNR_URL`, default a registry on this machine): the whole
universe is pulled at boot and every `FIS_GNR_RECONCILE_S` seconds, and a
GNode the mirror does not yet know is looked up by id when it first
connects. The registry being unreachable never stops FIS — it keeps
serving the mirror it has.

## Principals

A principal is a durable identity that belongs to a core piece of the
GridWorks platform and is allowed to connect to the broker. It comes in
two kinds. A **GNode** principal is a node in the grid topology: a house's
scada, the leaf transactive node that bids for it, a market maker. Its id
is its GNodeId, the same id the registry holds. A **Service** principal is
platform infrastructure that is not a node in the topology: the
grid-node-registry, the ear that audits the broker, the journalkeeper that
persists what the fleet says, the weather forecast service. Its id is a
UUID that FIS mints. In both cases the id is the certificate's CN, and the
row is created before the certificate is cut, so the two can never
disagree:

```bash
uv run fis principal create --kind Service --display-name gnr   # prints the id
uv run fis principal create --kind GNode --g-node-id <GNodeId>
gwcert key add --common-name <id>                               # on certbot
uv run fis principal list
uv run fis principal suspend <id>    # emergency eviction; `activate` lifts it
```

## Deploying

FIS runs on the broker box as a native systemd service from a clean
checkout of a pushed `main` SHA, the same shape as the other GridWorks
services. The `service/` directory ships the unit and the aliases; the
box's homedir README says what runs there.

Box setup, once (as root): create the `fis` login (docker group, so it can
run the Postgres container), clone this repo into its homedir as a full
clone on `main`, `uv sync --frozen`, copy `template.env` to `.env` and set
the box's values, start Postgres, migrate, then install the unit:

```bash
cp /home/fis/gridworks-fleet-index-service/service/fis-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fis-api
echo '. ~/gridworks-fleet-index-service/service/bash_aliases' >> /home/fis/.bashrc
```

The `.env` values that differ from dev: `FIS_UNIVERSE` (the box's
universe, `hw1`), `FIS_DB_URL` (the on-box Postgres, loopback), `FIS_GNR_URL`
(the universe's registry façade, `https://gnr.electricity.works`), and the
broker management credentials `FIS_RABBIT_MGMT_USER` /
`FIS_RABBIT_MGMT_PASSWORD` (the broker's default user, from the same
secret the broker's own `.env` holds). Bind stays loopback: the broker
calls FIS on `localhost:8080` from the same box.

Postgres on the box is the same `postgres:16` container as dev, started
by hand with the data directory on the box's data mount and the password
that `FIS_DB_URL` carries, published on loopback only:

```bash
docker run -d --name fis-postgres --restart unless-stopped \
  -e POSTGRES_USER=fis -e POSTGRES_PASSWORD='…' -e POSTGRES_DB=fis \
  -p 127.0.0.1:5437:5432 -v /mnt/pgdata/fis:/var/lib/postgresql/data postgres:16
uv run alembic upgrade head
```

Boot order matters and is a rule, not a unit dependency: the broker is a
docker-supervised container, not a systemd unit, so nothing orders it
after FIS. With the gate overlay on, a broker that cannot reach FIS admits
no fleet connection, so FIS is started and answers
`curl http://127.0.0.1:8080/ping` before the overlay is applied, and
`fisrestart` is a moment of denied connects, never a moment of admitted
wrong ones.

Operate on the box (aliases from `service/bash_aliases`; the matching
sudoers drop-in grants exactly these):

```
fisstart / fisstop / fisrestart   # sudo systemctl … fis-api
fisstatus                         # systemctl status fis-api
fislog                            # journalctl -u fis-api -f (FIS logs to stdout)
```

Update the code: `FIS_HOST=<box> ./deploy.sh` from a laptop puts the box
on the pushed tip of `main`, syncs deps, migrates, restarts, and checks
`/ping` over ssh. A change to `service/fis-api.service` is a unit copy
redone by root plus `systemctl daemon-reload`; a `git pull` does not change
the live unit.

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

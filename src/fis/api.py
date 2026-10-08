"""HTTP surface — the endpoints RabbitMQ's `auth_backend_http` calls.

Unlike the registry's public read façade, this is a **private** surface: the
broker calls it over localhost from the same box, and nothing else should
reach it. There is no CORS middleware and no public documentation audience —
the only client is the broker, and the contract is
`rabbitmq-auth-backend-http`'s, not a sema one.

`/auth/user` is the gate. The response is plain-text `allow`/`deny` — the
stock http backend's contract, not JSON. `/auth/{vhost,resource,topic}` are
the per-connection and per-publish checks.

The app's lifespan owns the registry reconcile: a background task that
seeds the mirror from gnr at boot and re-pulls on an interval.
Boot never waits on gnr — an unreachable registry is a logged warning and
the last-known mirror serves.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from fis.auth_event import record_auth_event
from fis.db.session import SessionLocal
from fis.gate import (
    Decision,
    GateReason,
    GateResult,
    MalformedRequest,
    decide_resource,
    decide_topic,
    decide_user,
    decide_vhost,
    parse_user_request,
    user_response,
)
from fis.gnr_client import RegistryReader, default_registry
from fis.mirror import reconcile_once
from fis.rabbit_admin import ConnectionKiller, default_killer
from fis.settings import GnrSettings, Settings

logger = logging.getLogger(__name__)


async def _form_and_query(request: Request) -> dict[str, str]:
    """The broker's params, whether it POSTs a form or GETs a query string.

    rmqbot pins `auth_http.http_method = post`; reading both keeps FIS honest
    against either broker configuration.
    """
    params = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update({k: v for k, v in form.items() if isinstance(v, str)})
    return params


def create_app(
    killer: ConnectionKiller | None = None,
    universe: str | None = None,
    registry: RegistryReader | None = None,
    *,
    reconcile: bool = True,
) -> FastAPI:
    # Resolved once at app build. Tests pass a fake killer, registry and
    # universe, and switch the reconcile loop off; the running service
    # resolves all of it from the box's settings.
    resolved_universe = universe if universe is not None else Settings().universe

    def get_killer() -> ConnectionKiller:
        return killer if killer is not None else default_killer()

    def get_registry() -> RegistryReader:
        return registry if registry is not None else default_registry()

    async def reconcile_loop(interval_s: int) -> None:
        while True:
            await run_in_threadpool(
                reconcile_once,
                SessionLocal,
                get_registry(),
                get_killer(),
                universe=resolved_universe,
            )
            await asyncio.sleep(interval_s)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = (
            asyncio.create_task(reconcile_loop(GnrSettings().reconcile_s))
            if reconcile
            else None
        )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Fleet Index Service", lifespan=lifespan)

    @app.get("/ping")
    def ping() -> dict:
        return {"status": "ok"}

    @app.api_route("/auth/user", methods=["GET", "POST"])
    async def auth_user(request: Request) -> PlainTextResponse:
        params = await _form_and_query(request)
        req = parse_user_request(params)
        if req is None:
            # No principal named: nothing to decide for and nothing to record.
            logger.info("auth/user deny: no principal in the request")
            return PlainTextResponse(Decision.Deny.value)
        if isinstance(req, MalformedRequest):
            malformed = GateResult(Decision.Deny, GateReason.Malformed)
            logger.info(
                "auth/user deny (malformed) principal=%s transport=%s",
                req.principal_id,
                req.transport.value,
            )

            def record_malformed() -> None:
                with SessionLocal() as session:
                    record_auth_event(session, req, malformed)

            return PlainTextResponse(
                Decision.Deny.value, background=BackgroundTask(record_malformed)
            )
        result: GateResult | None = None

        def run() -> str:
            nonlocal result
            with SessionLocal() as session:
                result = decide_user(
                    session,
                    req,
                    get_killer(),
                    get_registry(),
                    universe=resolved_universe,
                )
            logger.info(
                "auth/user %s (%s) principal=%s run=%s instance=%s",
                result.decision.value,
                result.reason.value,
                req.principal_id,
                req.run,
                req.instance_id,
            )
            return user_response(result, req)

        def record() -> None:
            assert result is not None  # set by run() before the response
            with SessionLocal() as session:
                record_auth_event(session, req, result)

        body = await run_in_threadpool(run)
        # The verdict goes back first; the audit record rides a background
        # task so the gate's latency carries no second database write.
        return PlainTextResponse(body, background=BackgroundTask(record))

    @app.api_route("/auth/vhost", methods=["GET", "POST"])
    async def auth_vhost(request: Request) -> PlainTextResponse:
        params = await _form_and_query(request)
        username = params.get("username", "")
        vhost = params.get("vhost", "")
        tags = params.get("tags", "")
        result = decide_vhost(tags=tags, vhost=vhost)
        logger.info(
            "auth/vhost %s (%s) principal=%s vhost=%s tags=%r",
            result.decision.value,
            result.reason.value,
            username,
            vhost,
            tags,
        )
        return PlainTextResponse(result.decision.value)

    @app.api_route("/auth/resource", methods=["GET", "POST"])
    async def auth_resource() -> PlainTextResponse:
        # allow-all; no request state is consulted.
        return PlainTextResponse(decide_resource().decision.value)

    @app.api_route("/auth/topic", methods=["GET", "POST"])
    async def auth_topic(request: Request) -> PlainTextResponse:
        params = await _form_and_query(request)
        username = params.get("username", "")
        permission = params.get("permission", "")
        routing_key = params.get("routing_key", "")

        def run() -> str:
            with SessionLocal() as session:
                result = decide_topic(
                    session,
                    username=username,
                    permission=permission,
                    routing_key=routing_key,
                )
            logger.info(
                "auth/topic %s (%s) principal=%s permission=%s rk=%s",
                result.decision.value,
                result.reason.value,
                username,
                permission,
                routing_key,
            )
            return result.decision.value

        return PlainTextResponse(await run_in_threadpool(run))

    return app


app = create_app()

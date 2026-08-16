"""HTTP surface — the endpoints RabbitMQ's `auth_backend_http` calls.

Unlike the registry's public read façade, this is a **private** surface: the
broker calls it over localhost from the same box, and nothing else should
reach it. There is no CORS middleware and no public documentation audience —
the only client is the broker, and the contract is
`rabbitmq-auth-backend-http`'s, not a sema one.

`/auth/user` lands here with the gate (build step 3). The response is
plain-text `allow`/`deny` — the stock http backend's contract, not JSON.
`/auth/{vhost,resource,topic}` follow with build step 4.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from fis.db.session import SessionLocal
from fis.gate import (
    Decision,
    decide_resource,
    decide_topic,
    decide_user,
    decide_vhost,
    parse_user_request,
)
from fis.rabbit_admin import ConnectionKiller, default_killer
from fis.settings import Settings

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
    killer: ConnectionKiller | None = None, universe: str | None = None
) -> FastAPI:
    app = FastAPI(title="Fleet Index Service")

    # Resolved once at app build. Tests pass a fake killer + universe; the
    # running service resolves both from the box's settings.
    resolved_universe = universe if universe is not None else Settings().universe

    def get_killer() -> ConnectionKiller:
        return killer if killer is not None else default_killer()

    @app.get("/ping")
    def ping() -> dict:
        return {"status": "ok"}

    @app.api_route("/auth/user", methods=["GET", "POST"])
    async def auth_user(request: Request) -> PlainTextResponse:
        params = await _form_and_query(request)
        req = parse_user_request(params)
        if req is None:
            logger.info("auth/user deny: malformed request")
            return PlainTextResponse(Decision.Deny.value)

        def run() -> str:
            with SessionLocal() as session:
                result = decide_user(
                    session, req, get_killer(), universe=resolved_universe
                )
            logger.info(
                "auth/user %s (%s) principal=%s run=%s instance=%s",
                result.decision.value,
                result.reason.value,
                req.principal_id,
                req.run,
                req.instance_id,
            )
            return result.decision.value

        body = await run_in_threadpool(run)
        return PlainTextResponse(body)

    @app.api_route("/auth/vhost", methods=["GET", "POST"])
    async def auth_vhost(request: Request) -> PlainTextResponse:
        params = await _form_and_query(request)
        username = params.get("username", "")
        vhost = params.get("vhost", "")

        def run() -> str:
            with SessionLocal() as session:
                result = decide_vhost(session, username=username, vhost=vhost)
            logger.info(
                "auth/vhost %s (%s) principal=%s vhost=%s",
                result.decision.value,
                result.reason.value,
                username,
                vhost,
            )
            return result.decision.value

        return PlainTextResponse(await run_in_threadpool(run))

    @app.api_route("/auth/resource", methods=["GET", "POST"])
    async def auth_resource() -> PlainTextResponse:
        # v1 allow-all; no request state is consulted.
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

"""HTTP surface — the endpoints RabbitMQ's `auth_backend_http` calls.

Unlike the registry's public read façade, this is a **private** surface: the
broker calls it over localhost from the same box, and nothing else should
reach it. There is no CORS middleware and no public documentation audience —
the only client is the broker, and the contract is
`rabbitmq-auth-backend-http`'s, not a sema one.

`/auth/{user,vhost,resource,topic}` land with the gate (build step 3).
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Fleet Index Service")

    @app.get("/ping")
    def ping() -> dict:
        return {"status": "ok"}

    return app


app = create_app()

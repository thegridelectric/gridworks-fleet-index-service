"""Reading the grid-node-registry over its HTTP read façade.

FIS is a pure HTTP service: it joins no broker and never opens gnr's
Postgres. Everything it knows about the registry arrives through this
client — the whole-universe forest the reconcile loop pulls, and the
single-node lookup the gate reads through on a mirror miss.

Every response is decoded strictly through the vendored snapshot codec, so
a registry speaking a version FIS does not pin fails loudly here rather
than seeding the mirror with a shape the gate has not been tested against.

`RegistryReader` is a `Protocol` so the reconcile loop and the gate run
against a fake in the dev battery; `GnrHttpClient` is the real
implementation and `default_registry()` builds it from settings.
"""

from __future__ import annotations

import logging
import uuid
from functools import lru_cache
from typing import Protocol

import httpx

from fis.sema.codec import default_codec
from fis.sema.property_format import LeftRightDot, UUID4Str
from fis.sema.types import GNodeForest, GNodeForestRequest, GNodeGt
from fis.settings import GnrSettings

logger = logging.getLogger(__name__)


class RegistryReader(Protocol):
    def get_forest(self, roots: list[LeftRightDot]) -> GNodeForest | None:
        """The forest under `roots`, or `None` when the registry could not
        be reached or answered with something the codec rejects. `None`
        means "keep serving the last-known mirror", never "the forest is
        empty" — an empty forest comes back as a forest with no nodes."""
        ...

    def get_by_id(self, g_node_id: UUID4Str) -> GNodeGt | None:
        """One GNode by its immutable id, or `None` when the registry does
        not know it or could not be reached. The gate maps both to
        not-in-registry: an unreachable registry admits nothing new."""
        ...


class GnrHttpClient:
    """`RegistryReader` over gnr's read façade (`gnr.api`).

    Routes follow the house pattern: the forest query is a POST whose body
    is the `g.node.forest.request` word; the point lookup is a GET with the
    id in the path (the sanctioned scalar exception).
    """

    def __init__(
        self,
        base_url: str,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Tests hand in an httpx.MockTransport so the wire round-trip (word
        # out, word back through the codec) runs without a registry.
        self.transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    def get_forest(self, roots: list[LeftRightDot]) -> GNodeForest | None:
        request = GNodeForestRequest(roots=roots, request_id=str(uuid.uuid4()))
        try:
            with self._client() as client:
                resp = client.post(
                    f"{self.base_url}/gnr/g-node-forest-request",
                    json=request.to_dict(),
                )
                resp.raise_for_status()
                return default_codec.from_dict(resp.json(), expect=GNodeForest)
        except Exception as e:  # noqa: BLE001 -- unreachable or undecodable: serve the last-known mirror
            logger.warning("gnr forest pull for %s failed: %s", roots, e)
            return None

    def get_by_id(self, g_node_id: UUID4Str) -> GNodeGt | None:
        try:
            with self._client() as client:
                resp = client.get(f"{self.base_url}/gnr/g-node-by-id/{g_node_id}")
                if resp.status_code == httpx.codes.NOT_FOUND:
                    logger.info("gnr does not know GNodeId %s", g_node_id)
                    return None
                resp.raise_for_status()
                return default_codec.from_dict(resp.json(), expect=GNodeGt)
        except Exception as e:  # noqa: BLE001 -- unreachable or undecodable: nothing new is admitted
            logger.warning("gnr lookup of %s failed: %s", g_node_id, e)
            return None


@lru_cache(maxsize=1)
def default_registry() -> GnrHttpClient:
    """The service's registry reader, built once from the box's `.env`."""
    cfg = GnrSettings()
    return GnrHttpClient(base_url=cfg.url, timeout=cfg.timeout_s)

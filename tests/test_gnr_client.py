"""The registry client: the wire round-trip through the codec, and the two
ways a lookup comes back empty (unknown id, registry unreachable).

Hermetic: an `httpx.MockTransport` plays the registry, so the words that
cross the wire are exercised without a running gnr.
"""

from __future__ import annotations

import json

import httpx

from fis.gnr_client import GnrHttpClient
from fis.sema.codec import default_codec
from fis.sema.enums import BaseGNodeClass, GNodeStatus
from fis.sema.types import GNodeForest, GNodeForestRequest, GNodeGt

KEENE_ID = "dcb05390-5bca-40ef-b63d-7908ccb33d9b"
KEENE = GNodeGt(
    g_node_id=KEENE_ID,
    alias="d1.isone.me.versant.keene",
    base_class=BaseGNodeClass.MarketMaker,
    g_node_class="MarketMaker",
    status=GNodeStatus.Active,
    position_point_id="c1b8f2e4-9f9b-4a2c-8f0e-2a4d9b6a1c3e",
)
FOREST = GNodeForest(roots=["d1"], nodes=[KEENE], edges=[], send_time_ms=1762634100033)


def _client(handler) -> GnrHttpClient:
    return GnrHttpClient(
        "http://gnr.test", timeout=1.0, transport=httpx.MockTransport(handler)
    )


def test_get_forest_sends_the_request_word_and_decodes_the_forest() -> None:
    seen: list[GNodeForestRequest] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/gnr/g-node-forest-request"
        seen.append(
            default_codec.from_dict(
                json.loads(request.content), expect=GNodeForestRequest
            )
        )
        return httpx.Response(200, json=FOREST.to_dict())

    forest = _client(handler).get_forest(["d1"])
    assert forest == FOREST
    assert seen[0].roots == ["d1"]


def test_get_forest_returns_none_when_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert _client(handler).get_forest(["d1"]) is None


def test_get_forest_rejects_an_undecodable_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"TypeName": "g.node.forest", "Version": "999"})

    assert _client(handler).get_forest(["d1"]) is None


def test_get_by_id_decodes_the_node() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/gnr/g-node-by-id/{KEENE_ID}"
        return httpx.Response(200, json=KEENE.to_dict())

    assert _client(handler).get_by_id(KEENE_ID) == KEENE


def test_get_by_id_unknown_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no GNode"})

    assert _client(handler).get_by_id(KEENE_ID) is None

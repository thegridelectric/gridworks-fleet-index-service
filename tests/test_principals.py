"""Minting principals, against a real Postgres: the row comes first and its
id is the cert CN, so the two ids can never disagree."""

from __future__ import annotations

import pytest

from fis.db.models import PrincipalKind, PrincipalSql, PrincipalStatus
from fis.principals import (
    PrincipalExists,
    PrincipalUnknown,
    create_principal,
    list_principals,
    set_principal_status,
)
from fis.sema.property_format import is_uuid4_str

pytestmark = pytest.mark.integration

BEECH_ID = "19ee09df-80ba-437b-b6c1-1eebe9d34801"


def test_service_principal_mints_its_own_uuid4(session) -> None:
    row = create_principal(
        session, kind=PrincipalKind.Service, g_node_id=None, display_name="gnr"
    )
    assert is_uuid4_str(row.id) == row.id
    assert row.status is PrincipalStatus.Active
    assert session.get(PrincipalSql, row.id).display_name == "gnr"


def test_gnode_principal_is_its_gnode_id(session) -> None:
    row = create_principal(
        session, kind=PrincipalKind.GNode, g_node_id=BEECH_ID, display_name=None
    )
    assert row.id == BEECH_ID
    assert row.kind is PrincipalKind.GNode


def test_gnode_requires_its_id_and_service_refuses_one(session) -> None:
    with pytest.raises(ValueError, match="GNodeId"):
        create_principal(
            session, kind=PrincipalKind.GNode, g_node_id=None, display_name=None
        )
    with pytest.raises(ValueError, match="mints its own"):
        create_principal(
            session, kind=PrincipalKind.Service, g_node_id=BEECH_ID, display_name=None
        )
    with pytest.raises(ValueError):
        create_principal(
            session, kind=PrincipalKind.GNode, g_node_id="not-a-uuid", display_name=None
        )


def test_duplicate_id_refused(session) -> None:
    create_principal(
        session, kind=PrincipalKind.GNode, g_node_id=BEECH_ID, display_name=None
    )
    with pytest.raises(PrincipalExists):
        create_principal(
            session, kind=PrincipalKind.GNode, g_node_id=BEECH_ID, display_name=None
        )


def test_suspend_and_activate(session) -> None:
    create_principal(
        session, kind=PrincipalKind.GNode, g_node_id=BEECH_ID, display_name=None
    )
    assert (
        set_principal_status(session, BEECH_ID, PrincipalStatus.Suspended).status
        is PrincipalStatus.Suspended
    )
    assert (
        set_principal_status(session, BEECH_ID, PrincipalStatus.Active).status
        is PrincipalStatus.Active
    )
    with pytest.raises(PrincipalUnknown):
        set_principal_status(
            session, "5e971ce0-0000-4000-8000-000000000001", PrincipalStatus.Active
        )


def test_list_is_oldest_first(session) -> None:
    a = create_principal(
        session, kind=PrincipalKind.Service, g_node_id=None, display_name="a"
    )
    b = create_principal(
        session, kind=PrincipalKind.Service, g_node_id=None, display_name="b"
    )
    assert [row.id for row in list_principals(session)] == [a.id, b.id]

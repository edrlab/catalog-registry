"""The permissions matrix and the status state machine are data: pin every cell."""

import pytest

from registry.domain.enums import ActorRole, CatalogStatus
from registry.domain.lifecycle import (
    ACTIONS,
    STATUS_ACTIONS,
    TRANSITION_ACTIONS,
    TRANSITIONS,
    UNDO_ACTIONS,
    build_allowed_actions,
    resolve_transition_target,
    resolve_undo_action,
)
from registry.domain.permissions import ROLE_PERMISSIONS, build_permissions

pytestmark = pytest.mark.unit

ALL_PERMISSIONS = {
    "backoffice.view",
    "catalog.view",
    "catalog.create",
    "catalog.edit",
    "catalog.publish",
    "catalog.unpublish",
    "catalog.archive",
    "catalog.unarchive",
    "catalog.delete",
    "catalog.recommend",
    "country.view",
    "country.manage_subdivisions",
    "user.view",
    "user.change_role",
    "user.deactivate",
}


def test_a_viewer_can_only_look() -> None:
    assert build_permissions(ActorRole.VIEWER) == {
        "backoffice.view",
        "catalog.view",
        "country.view",
    }


def test_an_editor_manages_catalogs_but_not_countries_or_users() -> None:
    editor = build_permissions(ActorRole.EDITOR)

    assert {p for p in ALL_PERMISSIONS if p.startswith("catalog.")} <= editor
    assert not {"country.manage_subdivisions", "user.view", "user.change_role"} & editor


def test_an_admin_has_every_permission() -> None:
    assert build_permissions(ActorRole.ADMIN) == ALL_PERMISSIONS


def test_every_role_is_covered() -> None:
    assert set(ROLE_PERMISSIONS) == set(ActorRole)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CatalogStatus.DRAFT, ["edit", "publish", "delete"]),
        (CatalogStatus.PUBLISHED, ["edit", "unpublish", "archive", "recommend"]),
        (CatalogStatus.UNPUBLISHED, ["edit", "publish", "archive"]),
        (CatalogStatus.ARCHIVED, ["unarchive"]),
    ],
)
def test_an_editor_is_offered_exactly_what_the_lifecycle_allows(
    status: CatalogStatus, expected: list[str]
) -> None:
    assert build_allowed_actions(status, build_permissions(ActorRole.EDITOR)) == expected


@pytest.mark.parametrize("status", list(CatalogStatus))
def test_a_viewer_is_offered_nothing(status: CatalogStatus) -> None:
    assert build_allowed_actions(status, build_permissions(ActorRole.VIEWER)) == []


def test_only_a_draft_can_be_deleted_and_an_archived_catalog_cannot_be_edited() -> None:
    assert [s for s, actions in STATUS_ACTIONS.items() if "delete" in actions] == [
        CatalogStatus.DRAFT
    ]
    assert "edit" not in STATUS_ACTIONS[CatalogStatus.ARCHIVED]


def test_the_lifecycle_names_only_known_actions_for_every_status() -> None:
    assert set(STATUS_ACTIONS) == set(CatalogStatus)
    assert all(actions <= set(ACTIONS) for actions in STATUS_ACTIONS.values())


# The lifecycle contract's table, row by row. Every (status, action) pair not listed is refused.
D, P, U, A = (
    CatalogStatus.DRAFT,
    CatalogStatus.PUBLISHED,
    CatalogStatus.UNPUBLISHED,
    CatalogStatus.ARCHIVED,
)
CONTRACT_TRANSITIONS = {
    (D, "publish"): P,
    (P, "unpublish"): U,
    (P, "archive"): A,
    (U, "publish"): P,
    (U, "archive"): A,
    (A, "unarchive"): U,
}


@pytest.mark.parametrize("status", list(CatalogStatus))
@pytest.mark.parametrize("action", TRANSITION_ACTIONS)
def test_every_status_and_action_pair_matches_the_contract(
    status: CatalogStatus, action: str
) -> None:
    assert resolve_transition_target(status, action) == CONTRACT_TRANSITIONS.get((status, action))


def test_nothing_returns_to_draft_and_unarchive_never_publishes() -> None:
    assert D not in TRANSITIONS.values()
    assert resolve_transition_target(A, "unarchive") is U


def test_the_transitions_offered_are_exactly_the_ones_accepted() -> None:
    """`allowed_actions` and the endpoint read different tables; they must never disagree."""
    for status in CatalogStatus:
        offered = STATUS_ACTIONS[status] & set(TRANSITION_ACTIONS)
        accepted = {action for (source, action) in TRANSITIONS if source is status}
        assert offered == accepted, status


@pytest.mark.parametrize(
    ("status", "action", "undo"),
    [
        (P, "unpublish", "publish"),
        (U, "publish", "unpublish"),
        (U, "archive", "unarchive"),
        (P, "archive", None),
        (D, "publish", None),
        (A, "unarchive", None),
    ],
)
def test_the_undo_rule_matches_the_contract(
    status: CatalogStatus, action: str, undo: str | None
) -> None:
    assert resolve_undo_action(status, action) == undo


def test_an_undo_always_lands_exactly_on_the_previous_status() -> None:
    for (status, action), undo in UNDO_ACTIONS.items():
        assert resolve_transition_target(TRANSITIONS[(status, action)], undo) is status

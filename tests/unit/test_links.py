"""Link ordering and validation."""

import pytest

from registry.domain.enums import LinkRel
from registry.domain.links import (
    LINK_REL_PRIORITY,
    find_self_link,
    has_browsable_rel,
    order_links,
)

pytestmark = pytest.mark.unit


class Stub:
    def __init__(self, rel: LinkRel, tag: str) -> None:
        self.rel = rel
        self.tag = tag


def rel_of(link: Stub) -> LinkRel:
    return link.rel


def test_every_link_rel_has_a_priority() -> None:
    """A rel added to the enum without a priority is a KeyError at render time."""
    assert set(LINK_REL_PRIORITY) == set(LinkRel)


def test_self_sorts_first_however_it_arrives() -> None:
    links = [Stub(LinkRel.ALTERNATE, "a"), Stub(LinkRel.CATALOG, "c"), Stub(LinkRel.SELF, "s")]

    assert [link.tag for link in order_links(links, rel_of=rel_of)] == ["s", "c", "a"]


def test_two_links_with_the_same_rel_keep_their_input_order() -> None:
    links = [Stub(LinkRel.ALTERNATE, "first"), Stub(LinkRel.ALTERNATE, "second")]

    assert [link.tag for link in order_links(links, rel_of=rel_of)] == ["first", "second"]


def test_find_self_link_returns_none_when_absent() -> None:
    assert find_self_link([Stub(LinkRel.CATALOG, "c")], rel_of=rel_of) is None


@pytest.mark.parametrize(
    ("rels", "expected"),
    [
        ([LinkRel.CATALOG], True),
        ([LinkRel.SHELF], True),
        ([LinkRel.CATALOG, LinkRel.ICON], True),
        ([LinkRel.ICON], False),
        ([LinkRel.ALTERNATE], False),
        # `self` is synthesised, so it is neither required nor sufficient on input.
        ([LinkRel.SELF], False),
    ],
)
def test_has_browsable_rel(rels: list[LinkRel], expected: bool) -> None:
    assert has_browsable_rel(rels) is expected

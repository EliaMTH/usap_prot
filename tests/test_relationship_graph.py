"""
The typed, direction-neutral city-object graph (profile 0.3.0).

What the previous design got wrong, and what each test here pins:

  - traversal followed a hardcoded list of four CityGML *2.0* tokens, so a
    3.0 property was recorded and then never followed;
  - edges were parent/child, so a peer relation had to borrow hierarchy slots
    and could only be read one way;
  - the type was free text with no namespace, so two spellings of the same
    property could not be told apart or resolved back to a definition.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import assert_package_valid, make_pkg
from usap import USAPError, USAPPackage
from usap.core import _descendants_cte

CORE_NS = "http://www.opengis.net/citygml/3.0"
CON_NS = "http://www.opengis.net/citygml/construction/3.0"
TRAN_NS = "http://www.opengis.net/citygml/transportation/3.0"


def _graph(pkg: USAPPackage) -> dict[str, int]:
    """
    building -> roof (containment), building -> neighbour (peer),
    road_a -> road_b (peer, a network successor).
    """
    ids = {
        uid: pkg.create_city_object(object_uid=uid)
        for uid in ("building", "roof", "neighbour", "road_a", "road_b")
    }

    pkg.link_city_objects(
        ids["building"], ids["roof"], "boundary",
        code_space=CORE_NS, category="containment",
    )
    pkg.link_city_objects(
        ids["building"], ids["neighbour"], "adjacentTo",
        code_space=CORE_NS, category="peer",
    )
    pkg.link_city_objects(
        ids["road_a"], ids["road_b"], "successor",
        code_space=TRAN_NS, category="peer",
    )

    return ids


def _uids(rows: list[dict]) -> set[str]:
    return {row["object_uid"] for row in rows}


# ---------------------------------------------------------------------------
# Category drives traversal, and stays a default rather than a policy
# ---------------------------------------------------------------------------


def test_category_is_a_default_not_a_policy(pkg: USAPPackage) -> None:
    _graph(pkg)

    # Default: containment only. The neighbour is related, not a part.
    assert _uids(pkg.list_city_objects(descendants_of="building")) == {
        "building",
        "roof",
    }

    # Widening the category reaches it...
    assert _uids(
        pkg.list_city_objects(
            descendants_of="building",
            relationship_categories=("containment", "peer"),
        )
    ) == {"building", "roof", "neighbour"}

    # ...and naming the type exactly reaches it while excluding the part,
    # which is what makes the default a choice rather than a limitation.
    assert _uids(
        pkg.list_city_objects(
            descendants_of="building",
            relationship_types=[("adjacentTo", CORE_NS)],
        )
    ) == {"building", "neighbour"}


def test_one_query_can_name_several_link_types(pkg: USAPPackage) -> None:
    # Types from two different namespaces in a single query: a (name,
    # code_space) pair per entry, because one code_space argument cannot
    # span modules.
    ids = _graph(pkg)

    pkg.link_city_objects(
        ids["roof"], ids["road_a"], "fillingSurface",
        code_space=CON_NS, category="containment",
    )

    assert _uids(
        pkg.list_city_objects(
            descendants_of="building",
            relationship_types=[
                ("boundary", CORE_NS),
                ("fillingSurface", CON_NS),
            ],
        )
    ) == {"building", "roof", "road_a"}


def test_empty_type_filter_matches_nothing(pkg: USAPPackage) -> None:
    _graph(pkg)

    assert _uids(
        pkg.list_city_objects(descendants_of="building", relationship_types=[])
    ) == {"building"}


def test_unknown_type_or_category_raises(pkg: USAPPackage) -> None:
    # A typo must not quietly answer "this object has no parts".
    _graph(pkg)

    with pytest.raises(USAPError, match="not registered"):
        pkg.list_city_objects(
            descendants_of="building", relationship_types=["boundry"]
        )

    with pytest.raises(USAPError, match="Unknown relationship category"):
        pkg.list_city_objects(
            descendants_of="building", relationship_categories=["parts"]
        )


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def test_traversal_direction(pkg: USAPPackage) -> None:
    _graph(pkg)

    # 'out' from the building reaches the roof; 'in' from the building does
    # not, because nothing points at it.
    assert _uids(
        pkg.list_city_objects(descendants_of="roof", direction="in")
    ) == {"roof", "building"}

    assert _uids(
        pkg.list_city_objects(descendants_of="roof", direction="out")
    ) == {"roof"}

    assert _uids(
        pkg.list_city_objects(descendants_of="roof", direction="both")
    ) == {"roof", "building"}


def test_one_hop_direction_and_default_type_filter(pkg: USAPPackage) -> None:
    _graph(pkg)

    # related_to defaults to EVERY link type: a one-hop "what is related to
    # this" that hid peer links would be a trap.
    assert _uids(pkg.list_city_objects(related_to="building")) == {
        "roof",
        "neighbour",
    }

    assert _uids(pkg.list_city_objects(related_to="roof", direction="in")) == {
        "building"
    }

    assert pkg.list_city_objects(related_to="roof", direction="out") == []


def test_unknown_direction_raises(pkg: USAPPackage) -> None:
    _graph(pkg)

    with pytest.raises(USAPError, match="Unknown direction"):
        pkg.list_city_objects(descendants_of="building", direction="sideways")


# ---------------------------------------------------------------------------
# related_city_objects: edges, including ones leaving the document
# ---------------------------------------------------------------------------


def test_related_city_objects_returns_edges(pkg: USAPPackage) -> None:
    _graph(pkg)

    edges = pkg.related_city_objects("building")

    assert {(e["to_object_uid"], e["relationship_type"]) for e in edges} == {
        ("roof", "boundary"),
        ("neighbour", "adjacentTo"),
    }

    boundary = next(e for e in edges if e["relationship_type"] == "boundary")

    assert boundary["code_space"] == CORE_NS
    assert boundary["category"] == "containment"
    assert boundary["direction"] == "out"

    both = pkg.related_city_objects("roof", direction="both")

    assert len(both) == 1
    assert both[0]["direction"] == "in"


def test_external_target_is_stored_and_only_visible_as_an_edge(
    pkg: USAPPackage,
) -> None:
    # An xlink that leaves the document is a real, typed, directed statement.
    # It has no city-object row, so list_city_objects cannot represent it and
    # related_city_objects is the only way to see it — which is the whole
    # reason that method exists.
    building = pkg.create_city_object(object_uid="building")

    pkg.link_city_objects(
        building,
        None,
        "boundary",
        to_external_uri="https://example.org/other.gml#roof_9",
        code_space=CORE_NS,
        category="containment",
    )

    assert _uids(pkg.list_city_objects(descendants_of="building")) == {
        "building"
    }

    edges = pkg.related_city_objects("building")

    assert len(edges) == 1
    assert edges[0]["to_city_object_id"] is None
    assert edges[0]["to_external_uri"] == "https://example.org/other.gml#roof_9"

    assert pkg.related_city_objects("building", include_external=False) == []

    # Reported, but not an error: the package is valid, it just points
    # somewhere this file cannot resolve.
    report = pkg.validate_report()
    codes = {issue.code for issue in report.issues}

    assert "UNRESOLVED_RELATIONSHIP_TARGET" in codes
    assert "ORPHAN_RELATIONSHIP_TO" not in codes
    assert report.is_ok


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registering_is_idempotent_and_enriching(pkg: USAPPackage) -> None:
    first = pkg.register_relationship_type("boundary", code_space=CORE_NS)

    assert pkg.register_relationship_type("boundary", code_space=CORE_NS) == first

    # A category arriving later is filled in, so a package can be classified
    # after its edges already exist.
    pkg.register_relationship_type(
        "boundary", code_space=CORE_NS, category="containment"
    )

    types = {
        (t["local_name"], t["code_space"]): t
        for t in pkg.list_relationship_types()
    }

    assert types[("boundary", CORE_NS)]["category"] == "containment"

    # Contradicting it does not silently win.
    with pytest.raises(USAPError, match="cannot re-register"):
        pkg.register_relationship_type(
            "boundary", code_space=CORE_NS, category="peer"
        )


def test_same_name_in_two_code_spaces_stays_distinct(pkg: USAPPackage) -> None:
    # The identity that free text used to lose: 'boundary' from core and from
    # an ADE are two types, and neither shadows the other.
    core = pkg.register_relationship_type("boundary", code_space=CORE_NS)
    ade = pkg.register_relationship_type(
        "boundary", code_space="https://example.org/ade"
    )

    assert core != ade
    assert pkg.resolve_relationship_type("boundary", code_space=CORE_NS) == core

    # And a name is never matched across code spaces.
    with pytest.raises(USAPError, match="not registered"):
        pkg.resolve_relationship_type(
            "boundary", code_space="https://example.org/other"
        )


def test_list_relationship_types_counts_edges(pkg: USAPPackage) -> None:
    _graph(pkg)
    pkg.register_relationship_type("neverUsed", code_space=CORE_NS)

    counts = {t["local_name"]: t["edge_count"] for t in pkg.list_relationship_types()}

    assert counts["boundary"] == 1
    assert counts["neverUsed"] == 0

    used = [t["local_name"] for t in pkg.list_relationship_types(include_unused=False)]

    assert "neverUsed" not in used

    peers = [
        t["local_name"] for t in pkg.list_relationship_types(category="peer")
    ]

    assert sorted(peers) == ["adjacentTo", "successor"]


def test_type_cache_is_dropped_when_a_transaction_rolls_back(
    pkg: USAPPackage,
) -> None:
    # An auto-registration inside a failed transaction no longer exists, and a
    # cache still handing out its id would make the next insert die on the
    # foreign key.
    building = pkg.create_city_object(object_uid="building")
    roof = pkg.create_city_object(object_uid="roof")

    with pytest.raises(RuntimeError):
        with pkg.transaction():
            pkg.link_city_objects(
                building, roof, "provisional",
                code_space=CORE_NS, category="containment",
            )
            raise RuntimeError("caller fails after linking")

    assert pkg.list_relationship_types() == []

    # The same name must register cleanly afterwards.
    pkg.link_city_objects(
        building, roof, "provisional",
        code_space=CORE_NS, category="containment",
    )

    assert _uids(pkg.list_city_objects(descendants_of="building")) == {
        "building",
        "roof",
    }
    assert_package_valid(pkg)


# ---------------------------------------------------------------------------
# Schema constraints and the query plan
# ---------------------------------------------------------------------------


def test_exactly_one_endpoint_is_enforced(pkg: USAPPackage) -> None:
    building = pkg.create_city_object(object_uid="building")
    roof = pkg.create_city_object(object_uid="roof")

    with pytest.raises(USAPError, match="exactly one"):
        pkg.link_city_objects(
            building, roof, "boundary", to_external_uri="#elsewhere"
        )

    with pytest.raises(USAPError, match="exactly one"):
        pkg.link_city_objects(building, None, "boundary")


def test_duplicate_type_identity_is_rejected_by_the_database(
    pkg: USAPPackage,
) -> None:
    # NULLs are distinct in a SQLite unique index, so the identity index has
    # to COALESCE — otherwise ('boundary', NULL) could be stored twice.
    pkg.conn.execute(
        "INSERT INTO usap_relationship_type (local_name) VALUES ('boundary')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        pkg.conn.execute(
            "INSERT INTO usap_relationship_type (local_name) VALUES ('boundary')"
        )


def test_descendant_walk_uses_its_index(pkg: USAPPackage) -> None:
    # The 400x regression detector. _descendants_cte's CROSS JOIN pins the
    # join order; a plain JOIN makes SQLite rescan every edge per recursive
    # step. Nothing in the body may become a scan.
    _graph(pkg)

    for direction, expected_index in [
        ("out", "usap_rel_by_from_graph"),
        ("in", "usap_rel_by_to_graph"),
    ]:
        plan = " ".join(
            str(row[3])
            for row in pkg.conn.execute(
                "EXPLAIN QUERY PLAN "
                + _descendants_cte(1, direction)
                + " SELECT * FROM objects",
                (1, "usap_default", 1),
            )
        )

        assert f"USING INDEX {expected_index}" in plan, plan
        assert "SCAN usap_city_object_relationship" not in plan, plan


def test_both_directions_terminate_and_do_not_duplicate(
    pkg: USAPPackage,
) -> None:
    # UNION (not UNION ALL) in the recursive term both deduplicates and stops
    # the walk oscillating between the two endpoints of one edge.
    ids = _graph(pkg)
    pkg.link_city_objects(
        ids["roof"], ids["building"], "boundary",
        code_space=CORE_NS, category="containment",
    )

    rows = pkg.list_city_objects(descendants_of="building", direction="both")

    assert _uids(rows) == {"building", "roof"}
    assert len(rows) == 2

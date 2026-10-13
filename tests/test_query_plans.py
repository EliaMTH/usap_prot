"""
Query plans that the profile-0.6.0 indexes exist for.

Each test records the SQL a public call actually executes (executed_plans in
conftest) and checks that SQLite answers it by index. Without ANALYZE
statistics the planner's choice does not depend on how many rows a table
holds, so a handful of rows gets the same plan as the Catania package; what a
regression costs there is noted per test.

Asserting on EXPLAIN QUERY PLAN text, these may need updating on a SQLite
upgrade that rewords the plan.
"""

from __future__ import annotations

from conftest import executed_plans, make_mesh_part
from usap import USAPPackage
from usap.constants import DEFAULT_BLOCK_SIZE

CORE_NS = "http://www.opengis.net/citygml/3.0"
BLDG_NS = "http://www.opengis.net/citygml/building/3.0"

# The aliases the queries below give usap_annotation (a, x) and
# usap_annotation_object (ao). A scan of any of them is the regression.
ANNOTATION_ALIASES = {"a", "x", "ao", "usap_annotation", "usap_annotation_object"}


def _scanned(plans: list[str]) -> set[str]:
    """Whatever SQLite reads in full: the target of every 'SCAN <name>' line."""
    names = set()

    for plan in plans:
        for line in plan.splitlines():
            words = line.split()

            if words[:1] == ["SCAN"] and len(words) > 1:
                names.add(words[1])

    return names


def _building_with_annotations(pkg: USAPPackage) -> dict[str, int]:
    """
    A building with a roof and a part, two containment types between them (as
    any real vocabulary has), and annotations reaching the roof both through
    primary_city_object_id and through usap_annotation_object alone.
    """
    roof_class = pkg.create_semantic_class("test", "test:Roof", "Roof")
    ids = {
        uid: pkg.create_city_object(object_uid=uid, gml_id=f"gml_{uid}")
        for uid in ("building", "roof", "part")
    }
    pkg.link_city_objects(
        ids["building"], ids["roof"], "boundary",
        code_space=CORE_NS, category="containment",
    )
    pkg.link_city_objects(
        ids["building"], ids["part"], "buildingPart",
        code_space=BLDG_NS, category="containment",
    )

    pkg.create_annotation(
        annotation_uid="by_primary",
        semantic_class_id=roof_class,
        primary_city_object_id=ids["roof"],
    )
    linked = pkg.create_annotation(
        annotation_uid="by_link_only",
        semantic_class_id=roof_class,
    )
    pkg.link_annotation_to_object(linked, ids["roof"])

    return ids


def test_new_packages_carry_the_0_6_0_indexes(pkg: USAPPackage) -> None:
    expected = {
        "usap_annotation_by_primary_object": ("primary_city_object_id",),
        "usap_annotation_object_by_object": ("city_object_id", "relation_type"),
        "usap_city_object_by_gml_id": ("gml_id",),
    }

    for name, columns in expected.items():
        info = pkg.conn.execute(f"PRAGMA index_info({name})").fetchall()

        assert tuple(row["name"] for row in info) == columns, name

    # Not UNIQUE: a duplicate gml_id is the DUPLICATE_GML_ID validation
    # finding, not an insert the schema refuses.
    unique = {
        row["name"]: row["unique"]
        for row in pkg.conn.execute("PRAGMA index_list(usap_city_object)")
    }

    assert unique["usap_city_object_by_gml_id"] == 0


def test_city_object_annotations_are_found_by_index(pkg: USAPPackage) -> None:
    # elements_for_city_object reaches annotations through their primary
    # object and through usap_annotation_object. Neither column was indexed,
    # so every call read both tables whole: 5 ms per roof on Catania, ~22 s to
    # select each of its 4036 roofs once, against 0.06 ms with the indexes.
    _building_with_annotations(pkg)

    for call in (
        lambda: pkg.elements_for_city_object("roof", include_descendants=False),
        lambda: pkg.elements_for_city_object("building"),
        lambda: pkg.elements_for_city_objects(["building", "roof"]),
    ):
        plans = executed_plans(pkg, call)
        text = "\n".join(plans)

        assert not _scanned(plans) & ANNOTATION_ALIASES, text
        assert "usap_annotation_by_primary_object" in text, text
        assert "usap_annotation_object_by_object" in text, text


def test_list_annotations_by_object_is_answered_by_index(pkg: USAPPackage) -> None:
    # The object filter used to be "primary = ? OR EXISTS (...)", which SQLite
    # evaluates row by row: a scan of every annotation whatever the indexes
    # (30 ms per call on Catania). As IN (... UNION ...) each half is one
    # index lookup (0.02 ms).
    ids = _building_with_annotations(pkg)

    for call in (
        lambda: pkg.list_annotations(city_object_uid="roof"),
        lambda: pkg.list_annotations(city_object_id=ids["roof"]),
        lambda: pkg.list_annotations(
            city_object_uid="roof", status="accepted", limit=1
        ),
    ):
        plans = executed_plans(pkg, call)
        text = "\n".join(plans)

        assert not _scanned(plans) & ANNOTATION_ALIASES, text
        assert "usap_annotation_by_primary_object" in text, text
        assert "usap_annotation_object_by_object" in text, text


def test_resolving_a_name_is_a_multi_index_or(pkg: USAPPackage) -> None:
    # resolve_city_object matches object_uid OR gml_id. With gml_id unindexed
    # the OR could use no index at all, and every resolution -- once per batch
    # entry, once per related_to/descendants_of by name -- scanned every city
    # object: 4 ms each on Catania, ~3 minutes of a 47,666-entry batch build.
    _building_with_annotations(pkg)

    for reference in ("roof", "gml_roof"):
        plan = "\n".join(
            executed_plans(pkg, lambda: pkg.resolve_city_object(reference))
        )

        assert "MULTI-INDEX OR" in plan, plan
        assert "usap_city_object_by_gml_id" in plan, plan
        assert "usap_city_object" not in _scanned([plan]), plan


def _candidate_lookup(pkg: USAPPackage) -> list[str]:
    """
    Plan lines of the prefilter query annotations_for_elements runs for a
    sparse selection over ten blocks -- above the eight VALUES rows from which
    a plain JOIN turns the loops around.
    """
    part = make_mesh_part(pkg, element_count=10 * DEFAULT_BLOCK_SIZE)
    selection = [i * DEFAULT_BLOCK_SIZE + 1 for i in range(10)]

    plans = executed_plans(
        pkg, lambda: pkg.annotations_for_elements(part, "face", selection)
    )

    return next(p for p in plans if "CONSTANT ROWS" in p).splitlines()


def _selection_is_outside(lines: list[str]) -> bool:
    scan_sel = next(i for i, line in enumerate(lines) if line.startswith("SCAN sel"))
    search_mb = next(i for i, line in enumerate(lines) if line.startswith("SEARCH mb"))

    return scan_sel < search_mb and "block_start=?" in lines[search_mb]


def test_candidate_blocks_are_found_inside_the_range_index(pkg: USAPPackage) -> None:
    # The reverse query's prefilter: per touched block, the blocks whose
    # stored [min, max] overlaps the selection, answered inside
    # usap_mb_by_element_range without reading a payload. One face of the
    # Catania mesh went from 6,993 decoded blocks to 2 (37 ms to 0.13 ms).
    lines = _candidate_lookup(pkg)

    assert _selection_is_outside(lines), lines
    assert any(
        "USING COVERING INDEX usap_mb_by_element_range" in line for line in lines
    ), lines


def test_candidate_block_lookup_keeps_the_selection_outside(
    pkg: USAPPackage,
) -> None:
    # On the range index a plain JOIN gets the right plan too, so a plan test
    # on a normal package cannot catch a regression to one. Swap in the 0.5.0
    # three-column index: there a plain JOIN puts the block table outside from
    # eight VALUES rows up and rescans every block of the part per row. The
    # CROSS JOIN must keep the selection outside, looking blocks up by
    # block_start, whatever index is available.
    pkg.conn.executescript(
        """
        DROP INDEX usap_mb_by_element_range;
        CREATE INDEX test_mb_by_element_block
        ON usap_membership_block(asset_part_id, element_kind, block_start);
        """
    )

    lines = _candidate_lookup(pkg)

    assert _selection_is_outside(lines), lines
    assert any("test_mb_by_element_block" in line for line in lines), lines

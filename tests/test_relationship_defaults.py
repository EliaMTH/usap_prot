"""
The CityGML 3.0 link categories a package is created with (SDK 0.6.1).

USAPPackage.create registers the category of the 35 CityGML 3.0 relation
properties, so an imported CityGML 3.0 edge is classified without anyone
asserting it. What each test pins:

  - the table is the one shipped in usap.constants, and REFERENCE.md lists
    exactly it;
  - a category from the defaults gives way, once, to an explicit declaration,
    while two declarations that disagree still raise;
  - register_default_relationship_types brings a package created before the
    defaults existed up to date, filling only what is empty and reporting,
    never raising on, a category it disagrees with;
  - none of it is a schema change: the profile version does not move.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import assert_package_valid, make_pkg, seed_citygml_concepts
from usap import (
    KeptRelationshipCategory,
    USAPError,
    USAPPackage,
    import_citygml_semantics,
)
from usap.constants import (
    CITYGML_3_0_BUILDING_NS,
    CITYGML_3_0_CONSTRUCTION_NS,
    CITYGML_3_0_CORE_NS,
    CITYGML_3_0_RELATIONSHIP_CATEGORIES,
    CURRENT_PROFILE_VERSION,
    RELATIONSHIP_CATEGORIES,
    RELATIONSHIP_CATEGORY_SOURCE_DEFAULT,
    RELATIONSHIP_CATEGORY_SOURCE_KEY,
)

DEFAULT_MARKER = {RELATIONSHIP_CATEGORY_SOURCE_KEY: RELATIONSHIP_CATEGORY_SOURCE_DEFAULT}

DEFAULTS = {
    (local_name, code_space): category
    for local_name, code_space, category in CITYGML_3_0_RELATIONSHIP_CATEGORIES
}

REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"

NS = """xmlns:core="http://www.opengis.net/citygml/3.0"
    xmlns:bldg="http://www.opengis.net/citygml/building/3.0"
    xmlns:con="http://www.opengis.net/citygml/construction/3.0"
    xmlns:brid="http://www.opengis.net/citygml/bridge/3.0"
    xmlns:tran="http://www.opengis.net/citygml/transportation/3.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:xlink="http://www.w3.org/1999/xlink\""""

# One object per module the acceptance criteria name. Storey and BuildingUnit
# point at each other, and so do the two lanes: both pairs are peers, so the
# package must still validate with no containment cycle.
CITY = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <core:boundary>
        <con:RoofSurface gml:id="b1_roof"/>
      </core:boundary>
      <core:boundary>
        <con:WallSurface gml:id="b1_wall">
          <con:fillingSurface>
            <con:WindowSurface gml:id="b1_window"/>
          </con:fillingSurface>
        </con:WallSurface>
      </core:boundary>
      <bldg:buildingRoom>
        <bldg:BuildingRoom gml:id="b1_room"/>
      </bldg:buildingRoom>
      <bldg:buildingSubdivision>
        <bldg:Storey gml:id="b1_storey">
          <bldg:buildingUnit xlink:href="#b1_unit"/>
        </bldg:Storey>
      </bldg:buildingSubdivision>
      <bldg:buildingSubdivision>
        <bldg:BuildingUnit gml:id="b1_unit">
          <bldg:storey xlink:href="#b1_storey"/>
        </bldg:BuildingUnit>
      </bldg:buildingSubdivision>
    </bldg:Building>
  </core:cityObjectMember>
  <core:cityObjectMember>
    <brid:Bridge gml:id="br1">
      <brid:bridgePart>
        <brid:BridgePart gml:id="br1_part"/>
      </brid:bridgePart>
      <brid:bridgeRoom>
        <brid:BridgeRoom gml:id="br1_room"/>
      </brid:bridgeRoom>
    </brid:Bridge>
  </core:cityObjectMember>
  <core:cityObjectMember>
    <tran:Road gml:id="r1">
      <tran:section>
        <tran:Section gml:id="r1_section">
          <tran:trafficSpace>
            <tran:TrafficSpace gml:id="r1_lane_a">
              <tran:successor xlink:href="#r1_lane_b"/>
            </tran:TrafficSpace>
          </tran:trafficSpace>
          <tran:trafficSpace>
            <tran:TrafficSpace gml:id="r1_lane_b">
              <tran:predecessor xlink:href="#r1_lane_a"/>
            </tran:TrafficSpace>
          </tran:trafficSpace>
        </tran:Section>
      </tran:section>
    </tran:Road>
  </core:cityObjectMember>
</core:CityModel>
"""

# What a package built before 0.6.1 holds after an import: types the document
# used, registered without a category.
WALL_AND_ROOM = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <core:boundary>
        <con:WallSurface gml:id="b1_wall">
          <con:fillingSurface>
            <con:WindowSurface gml:id="b1_window"/>
          </con:fillingSurface>
        </con:WallSurface>
      </core:boundary>
      <bldg:buildingRoom>
        <bldg:BuildingRoom gml:id="b1_room"/>
      </bldg:buildingRoom>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""


def _types(pkg: USAPPackage) -> dict[tuple[str, str | None], dict]:
    return {
        (t["local_name"], t["code_space"]): t
        for t in pkg.list_relationship_types()
    }


def _descendants(pkg: USAPPackage, uid: str) -> set[str]:
    return {row["object_uid"] for row in pkg.list_city_objects(descendants_of=uid)}


def _operations(pkg: USAPPackage) -> list[tuple[str, dict | None]]:
    return [
        (row["operation"], json.loads(row["details_json"] or "null"))
        for row in pkg.conn.execute(
            "SELECT operation, details_json FROM usap_edit_log ORDER BY edit_id"
        )
    ]


def _write(tmp_path: Path, name: str, xml: str) -> Path:
    path = tmp_path / name
    path.write_text(xml, encoding="utf-8")

    return path


# ---------------------------------------------------------------------------
# The table and what create() makes of it
# ---------------------------------------------------------------------------


def test_the_table_names_35_distinct_citygml_3_0_properties() -> None:
    keys = [(name, ns) for name, ns, _ in CITYGML_3_0_RELATIONSHIP_CATEGORIES]

    assert len(keys) == 35
    assert len(set(keys)) == 35
    assert all(
        ns.startswith("http://www.opengis.net/citygml/") and ns.endswith("/3.0")
        for _, ns in keys
    )
    assert set(DEFAULTS.values()) <= set(RELATIONSHIP_CATEGORIES)

    # The two pairs that point both ways must not be containment: a file
    # using both directions would make each object its own part.
    for name in ("storey", "buildingUnit", "predecessor", "successor"):
        assert [c for n, _, c in CITYGML_3_0_RELATIONSHIP_CATEGORIES if n == name] == [
            "peer"
        ]


def test_a_new_package_carries_the_citygml_3_0_defaults(pkg: USAPPackage) -> None:
    types = _types(pkg)

    assert {key: t["category"] for key, t in types.items()} == DEFAULTS
    assert all(json.loads(t["metadata_json"]) == DEFAULT_MARKER for t in types.values())
    assert all(t["edge_count"] == 0 for t in types.values())

    # Rows, not schema: the profile a package is stamped with does not move.
    row = pkg.conn.execute("SELECT profile_version FROM usap_profile").fetchone()

    assert row["profile_version"] == CURRENT_PROFILE_VERSION

    assert _operations(pkg) == [
        ("register_default_relationship_types", {"registered": 35, "classified": 0})
    ]
    assert_package_valid(pkg)


def test_create_can_leave_the_defaults_out(bare_pkg: USAPPackage) -> None:
    assert bare_pkg.list_relationship_types() == []
    assert _operations(bare_pkg) == []
    assert_package_valid(bare_pkg)


def test_the_defaults_survive_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "reopen.usap.gpkg"
    USAPPackage.create(path).close()

    with USAPPackage.open(path) as pkg:
        assert {key: t["category"] for key, t in _types(pkg).items()} == DEFAULTS


# ---------------------------------------------------------------------------
# Defaults give way to a declaration; declarations do not give way
# ---------------------------------------------------------------------------


def test_a_declaration_replaces_a_default_once(pkg: USAPPackage) -> None:
    pkg.register_relationship_type(
        "storey", code_space=CITYGML_3_0_BUILDING_NS, category="containment"
    )

    storey = _types(pkg)[("storey", CITYGML_3_0_BUILDING_NS)]

    assert storey["category"] == "containment"
    assert storey["metadata_json"] is None  # a declaration now, not a default

    # From here on it is a declaration, and contradicting one still raises.
    with pytest.raises(USAPError, match="cannot re-register"):
        pkg.register_relationship_type(
            "storey", code_space=CITYGML_3_0_BUILDING_NS, category="peer"
        )

    assert ("override_default_relationship_category",
            {"default": "peer", "category": "containment"}) in _operations(pkg)


def test_a_declaration_that_agrees_turns_the_default_into_a_declaration(
    pkg: USAPPackage,
) -> None:
    # Otherwise "a config confirms peer, then an ontology says containment"
    # would let the ontology win silently -- and loading the two the other way
    # round would raise. Agreeing is declaring.
    pkg.register_relationship_type(
        "boundary", code_space=CITYGML_3_0_CORE_NS, category="containment"
    )

    assert _types(pkg)[("boundary", CITYGML_3_0_CORE_NS)]["metadata_json"] is None
    assert ("confirm_default_relationship_category",
            {"default": "containment", "category": "containment"}) in _operations(pkg)

    with pytest.raises(USAPError, match="cannot re-register"):
        pkg.register_relationship_type(
            "boundary", code_space=CITYGML_3_0_CORE_NS, category="peer"
        )


def test_a_declaration_without_a_category_leaves_the_default(
    pkg: USAPPackage,
) -> None:
    # No category is no information, as it always was -- not "unclassify it".
    pkg.register_relationship_type("storey", code_space=CITYGML_3_0_BUILDING_NS)

    storey = _types(pkg)[("storey", CITYGML_3_0_BUILDING_NS)]

    assert storey["category"] == "peer"
    assert json.loads(storey["metadata_json"]) == DEFAULT_MARKER


# ---------------------------------------------------------------------------
# register_default_relationship_types on a package created before them
# ---------------------------------------------------------------------------


def test_the_defaults_fill_a_package_created_before_them(tmp_path: Path) -> None:
    gml = _write(tmp_path, "wall_and_room.gml", WALL_AND_ROOM)

    # What 0.6.0 built: no defaults, one category declared differently, and
    # the import's own types left unclassified.
    with make_pkg(tmp_path, default_relationship_types=False) as pkg:
        seed_citygml_concepts(pkg)
        pkg.register_relationship_type(
            "fillingSurface", code_space=CITYGML_3_0_CONSTRUCTION_NS, category="peer"
        )
        import_citygml_semantics(pkg, gml)

        assert _descendants(pkg, "b1") == {"b1"}

        result = pkg.register_default_relationship_types()

        assert set(result.classified) == {
            ("boundary", CITYGML_3_0_CORE_NS),
            ("buildingRoom", CITYGML_3_0_BUILDING_NS),
        }
        assert len(result.registered) == 35 - 3
        assert result.kept == (
            KeptRelationshipCategory(
                local_name="fillingSurface",
                code_space=CITYGML_3_0_CONSTRUCTION_NS,
                category="peer",
                default_category="containment",
            ),
        )

        # The declared reading is kept, so the window is still not a part.
        assert _types(pkg)[("fillingSurface", CITYGML_3_0_CONSTRUCTION_NS)][
            "category"
        ] == "peer"
        assert _descendants(pkg, "b1") == {"b1", "b1_wall", "b1_room"}

        report = pkg.validate_report()

        assert report.is_ok, [issue.format() for issue in report.issues]
        assert "UNCLASSIFIED_RELATIONSHIP_TYPE" not in {
            issue.code for issue in report.issues
        }

        # Idempotent: nothing more to fill, and the same disagreement.
        again = pkg.register_default_relationship_types()

        assert again.registered == ()
        assert again.classified == ()
        assert again.kept == result.kept
        assert [op for op, _ in _operations(pkg)].count(
            "register_default_relationship_types"
        ) == 1

        # What it filled is a default like any other: a declaration may
        # still replace it.
        pkg.register_relationship_type(
            "buildingRoom", code_space=CITYGML_3_0_BUILDING_NS, category="peer"
        )

        assert _descendants(pkg, "b1") == {"b1", "b1_wall"}


def test_the_defaults_keep_the_metadata_a_type_already_has(
    bare_pkg: USAPPackage,
) -> None:
    pkg = bare_pkg
    pkg.register_relationship_type(
        "boundary", code_space=CITYGML_3_0_CORE_NS, metadata_json='{"note": "x"}'
    )
    pkg.register_relationship_type(
        "filling", code_space=CITYGML_3_0_CONSTRUCTION_NS, metadata_json="not json"
    )

    pkg.register_default_relationship_types()

    types = _types(pkg)
    boundary = types[("boundary", CITYGML_3_0_CORE_NS)]
    filling = types[("filling", CITYGML_3_0_CONSTRUCTION_NS)]

    assert boundary["category"] == "containment"
    assert json.loads(boundary["metadata_json"]) == {"note": "x", **DEFAULT_MARKER}

    # Metadata that is not a JSON object is the caller's, not ours to replace:
    # the category goes in, unmarked, and so counts as a declaration.
    assert filling["category"] == "containment"
    assert filling["metadata_json"] == "not json"

    with pytest.raises(USAPError, match="cannot re-register"):
        pkg.register_relationship_type(
            "filling", code_space=CITYGML_3_0_CONSTRUCTION_NS, category="peer"
        )

    # Replacing the marked default gives the caller's metadata back unchanged.
    pkg.register_relationship_type(
        "boundary", code_space=CITYGML_3_0_CORE_NS, category="peer"
    )

    assert json.loads(_types(pkg)[("boundary", CITYGML_3_0_CORE_NS)]["metadata_json"]) == {
        "note": "x"
    }


# ---------------------------------------------------------------------------
# What an import into a new package looks like
# ---------------------------------------------------------------------------


def test_a_citygml_3_0_import_is_classified_from_creation(tmp_path: Path) -> None:
    gml = _write(tmp_path, "city.gml", CITY)

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)
        import_citygml_semantics(pkg, gml)

        assert _descendants(pkg, "b1") == {
            "b1", "b1_roof", "b1_wall", "b1_window",
            "b1_room", "b1_storey", "b1_unit",
        }
        assert _descendants(pkg, "br1") == {"br1", "br1_part", "br1_room"}
        assert _descendants(pkg, "r1") == {
            "r1", "r1_section", "r1_lane_a", "r1_lane_b",
        }

        # The peers are recorded, and simply not followed.
        assert _descendants(pkg, "b1_storey") == {"b1_storey"}

        report = pkg.validate_report()
        codes = {issue.code for issue in report.issues}

        assert report.is_ok, [issue.format() for issue in report.issues]
        assert "UNCLASSIFIED_RELATIONSHIP_TYPE" not in codes
        assert "CITY_OBJECT_GRAPH_CYCLE" not in codes


def test_an_external_xlink_of_a_default_property_is_kept(tmp_path: Path) -> None:
    # The import trusts an xlink leaving the document only when its property
    # is a link type the package already knows -- the gate that keeps
    # <app:target> from minting a bogus edge. Every package now knows the 35,
    # so such a link is stored rather than skipped; one created without the
    # defaults still skips it.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <bldg:buildingRoom xlink:href="https://example.org/other.gml#room9"/>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""
    gml = _write(tmp_path, "external.gml", xml)

    with make_pkg(tmp_path, "with.usap.gpkg") as pkg:
        seed_citygml_concepts(pkg)

        with pytest.warns(UserWarning, match="outside"):
            result = import_citygml_semantics(pkg, gml)

        assert result.skipped_references == []
        assert [t.href for t in result.unresolved_targets] == [
            "https://example.org/other.gml#room9"
        ]

    with make_pkg(tmp_path, "without.usap.gpkg", default_relationship_types=False) as pkg:
        seed_citygml_concepts(pkg)
        result = import_citygml_semantics(pkg, gml)

        assert result.relationship_count == 0
        assert result.skipped_references


# ---------------------------------------------------------------------------
# The documentation lists the table it describes
# ---------------------------------------------------------------------------

# | `boundary` | `…/citygml/3.0` | `containment` | ... |
_REFERENCE_ROW = re.compile(r"^\| `(\w+)` \| `…/(citygml/[\w/.]+)` \| `(\w+)` \|")


def test_the_reference_table_lists_exactly_the_shipped_defaults() -> None:
    if not REFERENCE.exists():
        pytest.skip("docs/REFERENCE.md is not beside the tests")

    rows = {
        (match[1], f"http://www.opengis.net/{match[2]}", match[3])
        for line in REFERENCE.read_text(encoding="utf-8").splitlines()
        if (match := _REFERENCE_ROW.match(line))
    }

    assert rows == set(CITYGML_3_0_RELATIONSHIP_CATEGORIES)

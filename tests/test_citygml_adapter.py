from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    make_pkg,
    seed_citygml_concepts,
    seed_citygml_relationship_categories,
)
from usap import USAPError, USAPPackage, import_citygml_semantics


# CityGML 3.0. The property names differ from 2.0 in ways that matter here:
# the boundary property is core:boundary (was bldg:boundedBy), the thematic
# surfaces live in the construction namespace (were building), and an opening
# is con:fillingSurface -> con:WindowSurface (was bldg:opening -> bldg:Window,
# a different class entirely: Window is a filling *element* in 3.0).
TINY_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel
    xmlns:core="http://www.opengis.net/citygml/3.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:con="http://www.opengis.net/citygml/construction/3.0"
    xmlns:bldg="http://www.opengis.net/citygml/building/3.0">
  <core:cityObjectMember>
    <bldg:Building gml:id="building_1">
      <core:boundary>
        <con:RoofSurface gml:id="building_1_roof_1"/>
      </core:boundary>
      <core:boundary>
        <con:WallSurface gml:id="building_1_wall_1">
          <con:fillingSurface>
            <con:WindowSurface gml:id="building_1_window_1"/>
          </con:fillingSurface>
          <con:fillingSurface>
            <con:DoorSurface gml:id="building_1_door_1"/>
          </con:fillingSurface>
        </con:WallSurface>
      </core:boundary>
      <core:boundary>
        <con:GroundSurface gml:id="building_1_ground_1"/>
      </core:boundary>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""


def _edges(pkg: USAPPackage) -> set[tuple[str, str, str]]:
    """Every usap_default edge as (from_uid, to_uid, type)."""
    rows = pkg.conn.execute(
        """
        SELECT
            src.object_uid AS from_uid,
            dst.object_uid AS to_uid,
            rt.local_name AS relationship_type
        FROM usap_city_object_relationship AS r
        JOIN usap_relationship_type AS rt
            ON rt.relationship_type_id = r.relationship_type_id
        JOIN usap_city_object AS src
            ON src.city_object_id = r.from_city_object_id
        JOIN usap_city_object AS dst
            ON dst.city_object_id = r.to_city_object_id
        WHERE r.graph_name = 'usap_default'
        """
    ).fetchall()

    return {
        (row["from_uid"], row["to_uid"], row["relationship_type"])
        for row in rows
    }


def test_import_tiny_citygml_semantics(tmp_path: Path) -> None:
    citygml_path = tmp_path / "tiny_city.gml"
    db_path = tmp_path / "tiny_city.usap.gpkg"

    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")

    with USAPPackage.create(db_path, overwrite=True) as pkg:
        seed_citygml_concepts(pkg)

        result = import_citygml_semantics(pkg, citygml_path)

        assert result.object_count == 6

        object_uids = {
            row["object_uid"]
            for row in pkg.conn.execute(
                "SELECT object_uid FROM usap_city_object"
            ).fetchall()
        }

        assert object_uids == {
            "building_1",
            "building_1_roof_1",
            "building_1_wall_1",
            "building_1_window_1",
            "building_1_door_1",
            "building_1_ground_1",
        }

        # The stored type is the CityGML 3.0 property the edge came through,
        # verbatim — no rename onto a 2.0 token.
        edges = _edges(pkg)

        assert ("building_1", "building_1_roof_1", "boundary") in edges
        assert ("building_1", "building_1_wall_1", "boundary") in edges
        assert ("building_1", "building_1_ground_1", "boundary") in edges
        assert (
            "building_1_wall_1",
            "building_1_window_1",
            "fillingSurface",
        ) in edges
        assert (
            "building_1_wall_1",
            "building_1_door_1",
            "fillingSurface",
        ) in edges

        # …and it keeps the namespace of that property, so the type can be
        # resolved back to its definition.
        boundary = pkg.related_city_objects("building_1")[0]

        assert boundary["relationship_type"] == "boundary"
        assert boundary["code_space"] == "http://www.opengis.net/citygml/3.0"

        report = pkg.validate_report()
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_parts_are_reachable_at_every_depth(tmp_path: Path) -> None:
    # Both CityGML 3.0 containment properties must be followed: core:boundary
    # from the building to its surfaces, and con:fillingSurface from a wall to
    # its openings. The predecessor of this code hardcoded four CityGML 2.0
    # tokens, so 'fillingSurface' was recorded and then never traversed and
    # the window silently was not part of the building.
    citygml_path = tmp_path / "tiny_city.gml"
    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)
        seed_citygml_relationship_categories(pkg)
        import_citygml_semantics(pkg, citygml_path)

        descendants = {
            row["object_uid"]
            for row in pkg.list_city_objects(descendants_of="building_1")
        }

        assert descendants == {
            "building_1",
            "building_1_roof_1",
            "building_1_wall_1",
            "building_1_ground_1",
            "building_1_window_1",
            "building_1_door_1",
        }


def test_uncategorised_links_are_recorded_but_not_traversed(
    tmp_path: Path,
) -> None:
    # USAP ships no link vocabulary, so an import with no categories supplied
    # still records every edge — but nothing is a "part" until something says
    # which properties mean that. The point is that this is visible, not that
    # it is silent: the edges are queryable by name and validation says so.
    citygml_path = tmp_path / "tiny_city.gml"
    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)
        import_citygml_semantics(pkg, citygml_path)

        assert len(_edges(pkg)) == 5

        descendants = {
            row["object_uid"]
            for row in pkg.list_city_objects(descendants_of="building_1")
        }

        assert descendants == {"building_1"}

        # Still reachable when the caller names the type explicitly.
        by_name = {
            row["object_uid"]
            for row in pkg.list_city_objects(
                descendants_of="building_1",
                relationship_types=[
                    ("boundary", "http://www.opengis.net/citygml/3.0"),
                    (
                        "fillingSurface",
                        "http://www.opengis.net/citygml/construction/3.0",
                    ),
                ],
            )
        }

        assert len(by_name) == 6

        report = pkg.validate_report()
        codes = {issue.code for issue in report.issues}

        assert "UNCLASSIFIED_RELATIONSHIP_TYPE" in codes
        assert report.is_ok  # a warning, not an error


def test_import_without_concepts_fails_loud(tmp_path: Path) -> None:
    # The concept registry is a precondition, not something the import creates.
    # USAP ships no CityGML vocabulary, and quietly importing zero objects
    # would look like "this file has no buildings".
    citygml_path = tmp_path / "tiny_city.gml"
    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="No city-object concepts"):
            import_citygml_semantics(pkg, citygml_path)

        assert pkg.list_city_objects() == []


def test_malformed_citygml_fails_loud(tmp_path: Path) -> None:
    # A truncated file used to be parsed with recover=True, silently
    # importing a partial semantic graph; ingestion must refuse it instead.
    citygml_path = tmp_path / "broken.gml"
    citygml_path.write_text(
        TINY_CITYGML[: len(TINY_CITYGML) // 2], encoding="utf-8"
    )

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)

        with pytest.raises(USAPError, match="Malformed CityGML"):
            import_citygml_semantics(pkg, citygml_path)

        count = pkg.conn.execute(
            "SELECT COUNT(*) FROM usap_city_object"
        ).fetchone()[0]

        assert count == 0


NOT_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<CityModel xmlns="urn:not-citygml" xmlns:gml="http://www.opengis.net/gml/3.2">
  <cityObjectMember>
    <Building gml:id="building_1">
      <boundary>
        <RoofSurface gml:id="building_1_roof_1"/>
      </boundary>
    </Building>
  </cityObjectMember>
</CityModel>
"""

FOREIGN_ID_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel
    xmlns:core="http://www.opengis.net/citygml/3.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:other="urn:some-other-vocabulary"
    xmlns:bldg="http://www.opengis.net/citygml/building/3.0">
  <core:cityObjectMember>
    <bldg:Building other:id="not_a_gml_id"/>
  </core:cityObjectMember>
</core:CityModel>
"""


def test_non_citygml_namespace_is_refused(tmp_path: Path) -> None:
    # Element names alone say nothing: any vocabulary may call something
    # "Building". Importing it as a CityGML building would mint city objects
    # with CityGML class URIs for content that never claimed to be CityGML —
    # false semantics that look entirely plausible downstream.
    path = tmp_path / "not_citygml.xml"
    path.write_text(NOT_CITYGML, encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)

        with pytest.raises(USAPError, match="Not a CityGML document"):
            import_citygml_semantics(pkg, path)

        assert pkg.list_city_objects() == []


def test_foreign_id_attribute_is_not_a_gml_id(tmp_path: Path) -> None:
    # gml:id is the stable identity USAP binds annotations to. An id=
    # attribute from an unrelated namespace is not that, and adopting it would
    # produce an object_uid nothing else in the source refers to.
    path = tmp_path / "foreign_id.gml"
    path.write_text(FOREIGN_ID_CITYGML, encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)

        result = import_citygml_semantics(pkg, path)

        assert result.object_count == 1

        imported = result.imported_objects[0]

        assert imported.gml_id is None
        assert imported.object_uid.startswith("generated_Building_")

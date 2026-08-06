from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_pkg
from usap import USAPError, USAPPackage, import_citygml_semantics


TINY_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel
    xmlns:core="http://www.opengis.net/citygml/2.0"
    xmlns:gml="http://www.opengis.net/gml"
    xmlns:bldg="http://www.opengis.net/citygml/building/2.0">
  <core:cityObjectMember>
    <bldg:Building gml:id="building_1">
      <bldg:boundedBy>
        <bldg:RoofSurface gml:id="building_1_roof_1"/>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="building_1_wall_1">
          <bldg:opening>
            <bldg:Window gml:id="building_1_window_1"/>
          </bldg:opening>
          <bldg:opening>
            <bldg:Door gml:id="building_1_door_1"/>
          </bldg:opening>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:GroundSurface gml:id="building_1_ground_1"/>
      </bldg:boundedBy>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""


def test_import_tiny_citygml_semantics(tmp_path: Path) -> None:
    citygml_path = tmp_path / "tiny_city.gml"
    db_path = tmp_path / "tiny_city.usap.gpkg"

    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        result = import_citygml_semantics(pkg, citygml_path)

        assert result.object_count == 6

        object_uids = {
            row["object_uid"]
            for row in pkg.conn.execute(
                """
                SELECT object_uid
                FROM usap_city_object
                """
            ).fetchall()
        }

        assert "building_1" in object_uids
        assert "building_1_roof_1" in object_uids
        assert "building_1_wall_1" in object_uids
        assert "building_1_window_1" in object_uids
        assert "building_1_door_1" in object_uids
        assert "building_1_ground_1" in object_uids
        

        relationships = pkg.conn.execute(
            """
            SELECT
                p.object_uid AS parent_uid,
                c.object_uid AS child_uid,
                r.relationship_type,
                r.role,
                r.graph_name
            FROM usap_city_object_relationship AS r
            JOIN usap_city_object AS p
                ON p.city_object_id = r.parent_city_object_id
            JOIN usap_city_object AS c
                ON c.city_object_id = r.child_city_object_id
            WHERE r.graph_name = 'usap_default'
            ORDER BY parent_uid, child_uid
            """
        ).fetchall()

        rel_pairs = {
            (row["parent_uid"], row["child_uid"], row["relationship_type"], row["role"])
            for row in relationships
        }

        assert ("building_1", "building_1_roof_1", "boundedBy", "roof") in rel_pairs
        assert ("building_1", "building_1_wall_1", "boundedBy", "wall") in rel_pairs
        assert ("building_1_wall_1", "building_1_window_1", "opening", "window") in rel_pairs
        assert ("building_1_wall_1", "building_1_door_1", "opening", "door") in rel_pairs
        assert ("building_1", "building_1_ground_1", "boundedBy", "ground") in rel_pairs

        # The imported nesting must be reachable as "building_1 and its parts"
        # at every depth, including the window/door hanging off the wall via
        # 'opening' edges — that is what object-level annotation retrieval
        # walks.
        descendants = {
            row["object_uid"]
            for row in pkg.list_city_objects(descendants_of="building_1")
        }

        assert "building_1" in descendants
        assert "building_1_roof_1" in descendants
        assert "building_1_wall_1" in descendants
        assert "building_1_window_1" in descendants
        assert "building_1_door_1" in descendants

        report = pkg.validate_report()
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_malformed_citygml_fails_loud(tmp_path: Path) -> None:
    # A truncated file used to be parsed with recover=True, silently
    # importing a partial semantic graph; ingestion must refuse it instead.
    citygml_path = tmp_path / "broken.gml"
    citygml_path.write_text(
        TINY_CITYGML[: len(TINY_CITYGML) // 2], encoding="utf-8"
    )

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="Malformed CityGML"):
            import_citygml_semantics(pkg, citygml_path)

        count = pkg.conn.execute(
            "SELECT COUNT(*) FROM usap_city_object"
        ).fetchone()[0]

        assert count == 0


NOT_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<CityModel xmlns="urn:not-citygml" xmlns:gml="http://www.opengis.net/gml">
  <cityObjectMember>
    <Building gml:id="building_1">
      <boundedBy>
        <RoofSurface gml:id="building_1_roof_1"/>
      </boundedBy>
    </Building>
  </cityObjectMember>
</CityModel>
"""

FOREIGN_ID_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel
    xmlns:core="http://www.opengis.net/citygml/2.0"
    xmlns:gml="http://www.opengis.net/gml"
    xmlns:other="urn:some-other-vocabulary"
    xmlns:bldg="http://www.opengis.net/citygml/building/2.0">
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
        result = import_citygml_semantics(pkg, path)

        assert result.object_count == 1

        imported = result.imported_objects[0]

        assert imported.gml_id is None
        assert imported.object_uid.startswith("generated_Building_")

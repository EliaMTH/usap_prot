from __future__ import annotations

from pathlib import Path

from usap import USAPPackage, import_citygml_semantics


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
        schema_path="sql/schema.sql",
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

        closure_rows = pkg.conn.execute(
            """
            SELECT
                p.object_uid AS ancestor_uid,
                c.object_uid AS descendant_uid,
                cl.depth
            FROM usap_city_object_closure AS cl
            JOIN usap_city_object AS p
                ON p.city_object_id = cl.ancestor_city_object_id
            JOIN usap_city_object AS c
                ON c.city_object_id = cl.descendant_city_object_id
            WHERE cl.graph_name = 'usap_default'
              AND p.object_uid = 'building_1'
            """
        ).fetchall()

        descendants = {
            row["descendant_uid"]
            for row in closure_rows
        }

        assert "building_1" in descendants
        assert "building_1_roof_1" in descendants
        assert "building_1_wall_1" in descendants
        assert "building_1_window_1" in descendants
        assert "building_1_door_1" in descendants

        report = pkg.validate_report()
        assert report.is_ok
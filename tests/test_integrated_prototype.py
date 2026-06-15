from __future__ import annotations

import json
from pathlib import Path

import laspy
import numpy as np
import trimesh

from usap import (
    ELEMENT_KIND_FACE,
    ELEMENT_KIND_POINT,
    USAPPackage,
    import_citygml_semantics,
    register_las_asset,
    register_mesh_asset,
    seed_prototype_ade_classes,
)


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
        </bldg:WallSurface>
      </bldg:boundedBy>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""


def _write_tiny_las(path: Path, point_count: int = 20) -> None:
    header = laspy.LasHeader(point_format=3, version="1.2")
    las = laspy.LasData(header)

    las.x = np.arange(point_count, dtype=float)
    las.y = np.arange(point_count, dtype=float) + 100.0
    las.z = np.arange(point_count, dtype=float) + 200.0

    las.write(path)


def _write_tiny_mesh(path: Path) -> None:
    vertices = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
        ]
    )

    faces = np.array(
        [
            [0, 1, 2],
            [0, 2, 3],
        ]
    )

    mesh = trimesh.Trimesh(
        vertices=vertices,
        faces=faces,
        process=False,
    )

    mesh.export(path)


def test_integrated_citygml_las_mesh_ade_annotation(tmp_path: Path) -> None:
    citygml_path = tmp_path / "tiny_city.gml"
    las_path = tmp_path / "tiny.las"
    mesh_path = tmp_path / "tiny_mesh.ply"
    db_path = tmp_path / "integrated.usap.gpkg"

    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")
    _write_tiny_las(las_path, point_count=20)
    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        citygml = import_citygml_semantics(pkg, citygml_path)
        las = register_las_asset(pkg, las_path)

        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="tiny_city_triangulation",
            representation_kind="triangulated_city_surface",
            lod=None,
        )

        ade_classes = seed_prototype_ade_classes(pkg)

        roof_row = pkg.conn.execute(
            """
            SELECT co.city_object_id, co.object_uid
            FROM usap_city_object AS co
            JOIN usap_semantic_class AS sc
                ON sc.semantic_class_id = co.semantic_class_id
            WHERE sc.local_name = 'RoofSurface'
            LIMIT 1
            """
        ).fetchone()

        assert roof_row is not None
        assert roof_row["object_uid"] == "building_1_roof_1"

        attributes = {
            "domain": "energy_emissions",
            "geometric_attributes": {
                "roof_slope": None,
                "orientation": None,
                "shading": None,
            },
            "non_geometric_attributes": {
                "construction_era": None,
                "use": None,
            },
            "derived_indicators": {
                "specific_energy_kwh_m2": None,
                "co2_emissions": None,
            },
        }

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_integrated_energy_roof",
            semantic_class_id=ade_classes.by_name["EnergyRoof"],
            primary_city_object_id=int(roof_row["city_object_id"]),
            label="Integrated EnergyRoof annotation",
            status="draft",
            confidence=None,
            attributes_json=json.dumps(attributes),
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            element_indices=[1, 2, 3],
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[1],
        )

        las_matches = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            selected_indices=[2],
        )

        mesh_matches = pkg.annotations_for_elements(
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[1],
        )

        assert len(las_matches) == 1
        assert las_matches[0]["annotation_uid"] == "ann_integrated_energy_roof"
        assert las_matches[0]["semantic_class"] == "EnergyRoof"
        assert las_matches[0]["matched_elements"] == [2]

        assert len(mesh_matches) == 1
        assert mesh_matches[0]["annotation_uid"] == "ann_integrated_energy_roof"
        assert mesh_matches[0]["semantic_class"] == "EnergyRoof"
        assert mesh_matches[0]["matched_elements"] == [1]

        membership_parts = {
            row["asset_part_id"]
            for row in pkg.conn.execute(
                """
                SELECT DISTINCT asset_part_id
                FROM usap_membership_block
                WHERE annotation_id = ?
                """,
                (annotation_id,),
            ).fetchall()
        }

        assert las.asset_part_id in membership_parts
        assert mesh.primary_asset_part_id in membership_parts
        assert len(membership_parts) == 2

        assert citygml.object_count == 4

        report = pkg.validate_report()
        assert report.is_ok
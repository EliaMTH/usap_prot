from __future__ import annotations

from pathlib import Path

from conftest import write_tiny_mesh as _write_tiny_mesh
from usap import (
    ELEMENT_KIND_FACE,
    USAPPackage,
    register_mesh_asset,
    seed_default_citygml_vocabulary,
)


def test_register_generic_mesh_and_annotate_faces(tmp_path: Path) -> None:
    mesh_path = tmp_path / "city_triangulation.ply"
    db_path = tmp_path / "mesh.usap.gpkg"

    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        classes = seed_default_citygml_vocabulary(pkg)

        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="city_triangulation",
            representation_kind="triangulated_city_surface",
            lod=None,
        )

        assert mesh.asset_id > 0
        assert mesh.total_face_count == 2
        assert len(mesh.parts) == 1
        assert mesh.primary_asset_part_id == mesh.parts[0].asset_part_id

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_generic_mesh_roof_face",
            semantic_class_id=classes.by_name["RoofSurface"],
            label="Generic triangulation roof face",
            status="accepted",
            confidence=1.0,
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[1],
        )

        matches = pkg.annotations_for_elements(
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[1],
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_generic_mesh_roof_face"
        assert matches[0]["semantic_class"] == "RoofSurface"
        assert matches[0]["matched_elements"] == [1]

        report = pkg.validate_report()
        assert report.is_ok


def test_register_lod2_mesh_is_same_mechanism(tmp_path: Path) -> None:
    mesh_path = tmp_path / "lod2_buildings.ply"
    db_path = tmp_path / "lod2.usap.gpkg"

    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="buildings_lod2",
            representation_kind="building_mesh",
            lod="LoD2",
        )

        assert mesh.lod == "LoD2"
        assert mesh.representation_name == "buildings_lod2"
        assert mesh.representation_kind == "building_mesh"
        assert mesh.total_face_count == 2

        row = pkg.conn.execute(
            """
            SELECT metadata_json
            FROM usap_asset
            WHERE asset_id = ?
            """,
            (mesh.asset_id,),
        ).fetchone()

        assert row is not None
        assert "LoD2" in row["metadata_json"]
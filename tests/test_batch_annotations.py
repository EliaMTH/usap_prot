from __future__ import annotations

import json
from pathlib import Path

import laspy
import numpy as np
import pytest
import trimesh

from usap import (
    USAPError,
    USAPPackage,
    apply_annotation_batch,
    register_las_asset,
    register_mesh_asset,
    seed_default_ade_vocabulary,
    seed_default_citygml_vocabulary,
)


def _write_tiny_las(path: Path, point_count: int = 10) -> None:
    header = laspy.LasHeader(point_format=3, version="1.2")
    las = laspy.LasData(header)

    las.x = np.arange(point_count, dtype=float)
    las.y = np.arange(point_count, dtype=float)
    las.z = np.arange(point_count, dtype=float)

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

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.export(path)


def test_apply_annotation_batch_with_las_and_mesh(tmp_path: Path) -> None:
    db_path = tmp_path / "batch.usap.gpkg"
    las_path = tmp_path / "tiny.las"
    mesh_path = tmp_path / "tiny.ply"

    _write_tiny_las(las_path, point_count=10)
    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        citygml_vocab = seed_default_citygml_vocabulary(pkg)
        seed_default_ade_vocabulary(pkg)

        roof_object_id = pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=citygml_vocab.by_name["RoofSurface"],
            gml_id="building_1_roof_1",
        )

        las = register_las_asset(pkg, las_path)
        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="tiny_mesh",
            representation_kind="triangulated_surface",
            lod=None,
        )

        batch = {
            "annotations": [
                {
                    "annotation_uid": "ann_batch_energy_roof",
                    "concept": "EnergyRoof",
                    "city_object_uid": "building_1_roof_1",
                    "label": "Batch EnergyRoof",
                    "status": "draft",
                    "attributes": {
                        "domain": "energy_emissions"
                    },
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [1, 2, 3]
                        },
                        {
                            "asset_part_id": mesh.primary_asset_part_id,
                            "element_kind": "face",
                            "element_indices": [0, 1]
                        }
                    ]
                }
            ]
        }

        result = apply_annotation_batch(pkg, batch)

        assert result.annotation_count == 1
        assert result.membership_count == 2

        annotation = pkg.get_annotation(
            annotation_uid="ann_batch_energy_roof",
            include_membership_summary=True,
        )

        assert annotation is not None
        assert annotation["semantic_class"] == "EnergyRoof"
        assert annotation["primary_city_object_id"] == roof_object_id
        assert len(annotation["membership_summary"]) == 2

        las_matches = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[2],
        )

        mesh_matches = pkg.annotations_for_elements(
            asset_part_id=mesh.primary_asset_part_id,
            element_kind="face",
            selected_indices=[1],
        )

        assert las_matches[0]["annotation_uid"] == "ann_batch_energy_roof"
        assert mesh_matches[0]["annotation_uid"] == "ann_batch_energy_roof"

        report = pkg.validate_report()
        assert report.is_ok


def test_batch_rejects_unknown_concept(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown_batch.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        register_las_asset(pkg, las_path)

        batch = {
            "annotations": [
                {
                    "annotation_uid": "ann_unknown",
                    "concept": "DefinitelyNotRegistered",
                    "memberships": [
                        {
                            "asset_part_id": 1,
                            "element_kind": "point",
                            "element_indices": [1]
                        }
                    ]
                }
            ]
        }

        with pytest.raises(USAPError):
            apply_annotation_batch(pkg, batch)


def test_batch_rejects_out_of_range_indices(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_indices.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=5)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        seed_default_citygml_vocabulary(pkg)
        las = register_las_asset(pkg, las_path)

        batch = {
            "annotations": [
                {
                    "annotation_uid": "ann_bad_index",
                    "concept": "RoofSurface",
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [99]
                        }
                    ]
                }
            ]
        }

        with pytest.raises(USAPError):
            apply_annotation_batch(pkg, batch)


def test_batch_replace_existing(tmp_path: Path) -> None:
    db_path = tmp_path / "replace.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        seed_default_citygml_vocabulary(pkg)
        las = register_las_asset(pkg, las_path)

        batch_1 = {
            "annotations": [
                {
                    "annotation_uid": "ann_replace",
                    "concept": "RoofSurface",
                    "label": "First",
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [1, 2]
                        }
                    ]
                }
            ]
        }

        batch_2 = {
            "annotations": [
                {
                    "annotation_uid": "ann_replace",
                    "concept": "RoofSurface",
                    "label": "Second",
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [3, 4]
                        }
                    ]
                }
            ]
        }

        apply_annotation_batch(pkg, batch_1)

        with pytest.raises(USAPError):
            apply_annotation_batch(pkg, batch_2)

        apply_annotation_batch(
            pkg,
            batch_2,
            replace_existing=True,
        )

        annotation = pkg.get_annotation(
            annotation_uid="ann_replace",
            include_membership_summary=True,
        )

        assert annotation is not None
        assert annotation["label"] == "Second"

        matches_old = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[1],
        )

        matches_new = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[3],
        )

        assert matches_old == []
        assert matches_new[0]["annotation_uid"] == "ann_replace"
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_mesh_part, make_pkg, seed_citygml_concepts
from conftest import write_tiny_las as _write_tiny_las, write_tiny_mesh as _write_tiny_mesh
from usap import (
    USAPError,
    USAPPackage,
    apply_annotation_batch,
    apply_annotation_batch_file,
    register_las_asset,
    register_mesh_asset,
    seed_default_ade_vocabulary,
)


def test_apply_annotation_batch_with_las_and_mesh(tmp_path: Path) -> None:
    db_path = tmp_path / "batch.usap.gpkg"
    las_path = tmp_path / "tiny.las"
    mesh_path = tmp_path / "tiny.ply"

    _write_tiny_las(las_path, point_count=10)
    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        citygml_vocab = seed_citygml_concepts(pkg)
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
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_batch_rejects_unknown_concept(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown_batch.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
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

        with pytest.raises(USAPError, match="concept not found"):
            apply_annotation_batch(pkg, batch)


def test_batch_rejects_out_of_range_indices(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_indices.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=5)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        seed_citygml_concepts(pkg)
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

        with pytest.raises(USAPError, match="out of range"):
            apply_annotation_batch(pkg, batch)


def test_batch_replace_existing(tmp_path: Path) -> None:
    db_path = tmp_path / "replace.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        seed_citygml_concepts(pkg)
        las = register_las_asset(pkg, las_path)

        batch_1 = {
            "annotations": [
                {
                    "annotation_uid": "ann_replace",
                    "concept": "RoofSurface",
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
                    "status": "accepted",
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

        with pytest.raises(USAPError, match="already exists"):
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
        assert annotation["status"] == "accepted"

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


def test_batch_replace_preserves_omitted_fields(tmp_path: Path) -> None:
    # Re-applying a batch with replace_existing is a *partial* update: fields
    # omitted from the entry must keep their existing values rather than being
    # wiped to NULL. This guards against silent data loss when a follow-up
    # batch only carries new memberships.
    db_path = tmp_path / "replace_preserve.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        citygml_vocab = seed_citygml_concepts(pkg)
        seed_default_ade_vocabulary(pkg)

        roof_object_id = pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=citygml_vocab.by_name["RoofSurface"],
            gml_id="building_1_roof_1",
        )

        las = register_las_asset(pkg, las_path)

        full = {
            "annotations": [
                {
                    "annotation_uid": "ann_preserve",
                    "concept": "EnergyRoof",
                    "city_object_uid": "building_1_roof_1",
                    "confidence": 0.75,
                    "attributes": {"domain": "energy_emissions"},
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [1, 2],
                        }
                    ],
                }
            ]
        }

        # Minimal replacement: only the required fields + new memberships.
        minimal = {
            "annotations": [
                {
                    "annotation_uid": "ann_preserve",
                    "concept": "EnergyRoof",
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [3, 4],
                        }
                    ],
                }
            ]
        }

        apply_annotation_batch(pkg, full)
        apply_annotation_batch(pkg, minimal, replace_existing=True)

        annotation = pkg.get_annotation(annotation_uid="ann_preserve")

        assert annotation is not None
        # Omitted fields are preserved, not cleared to NULL:
        assert annotation["confidence"] == 0.75
        assert annotation["attributes_json"] is not None
        assert json.loads(annotation["attributes_json"]) == {
            "domain": "energy_emissions"
        }
        assert annotation["primary_city_object_id"] == roof_object_id

        # The membership was still replaced by the new indices.
        matches_new = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[3],
        )
        matches_old = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[1],
        )

        assert matches_new[0]["annotation_uid"] == "ann_preserve"
        assert matches_old == []

def test_batch_replace_moves_primary_object_link(tmp_path: Path) -> None:
    # Re-applying a batch entry against a different city object moves the
    # annotation. The batch path used to add the new link without removing the
    # old one, leaving the annotation answering queries for both objects.
    db_path = tmp_path / "replace_move.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        citygml_vocab = seed_citygml_concepts(pkg)

        for uid in ("roof_a", "roof_b"):
            pkg.create_city_object(
                object_uid=uid,
                semantic_class_id=citygml_vocab.by_name["RoofSurface"],
            )

        las = register_las_asset(pkg, las_path)

        def batch_for(object_uid: str) -> dict:
            return {
                "annotations": [
                    {
                        "annotation_uid": "ann_moved",
                        "concept": "RoofSurface",
                        "city_object_uid": object_uid,
                        "memberships": [
                            {
                                "asset_part_id": las.asset_part_id,
                                "element_kind": "point",
                                "element_indices": [1, 2],
                            }
                        ],
                    }
                ]
            }

        apply_annotation_batch(pkg, batch_for("roof_a"))
        apply_annotation_batch(
            pkg,
            batch_for("roof_b"),
            replace_existing=True,
        )

        annotation = pkg.get_annotation(annotation_uid="ann_moved")

        assert annotation is not None
        assert annotation["primary_city_object_uid"] == "roof_b"

        links = pkg.conn.execute(
            """
            SELECT co.object_uid
            FROM usap_annotation_object AS ao
            JOIN usap_city_object AS co
                ON co.city_object_id = ao.city_object_id
            WHERE ao.annotation_id = ?
            """,
            (annotation["annotation_id"],),
        ).fetchall()

        assert [row["object_uid"] for row in links] == ["roof_b"]

        assert pkg.elements_for_city_object(
            "roof_a",
            include_descendants=False,
        ) == []

        moved = pkg.elements_for_city_object("roof_b", include_descendants=False)

        assert {block["annotation_id"] for block in moved} == {
            annotation["annotation_id"]
        }


def test_apply_annotation_batch_file(tmp_path: Path) -> None:
    # INGESTION.md procedure 3 relies on this file entry point for
    # standalone edits; it must behave exactly like the in-memory batch
    # and fail loudly on a missing path.
    with make_pkg(tmp_path) as pkg:
        make_mesh_part(pkg)
        pkg.create_semantic_class(
            scheme="local", class_uri="local:TempRoof", local_name="TempRoof"
        )

        batch_path = tmp_path / "batch.json"
        batch_path.write_text(
            json.dumps(
                {
                    "create_missing_city_objects": True,
                    "annotations": [
                        {
                            "city_object_uid": "tower_A_roof",
                            "concept": "TempRoof",
                            "memberships": [
                                {
                                    "asset_uri": "mesh.ply",
                                    "element_indices": [0, 1],
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = apply_annotation_batch_file(pkg, batch_path)

        assert result.annotation_count == 1

        blocks = pkg.elements_for_city_object("tower_A_roof", expand=True)
        assert [b["elements"] for b in blocks] == [[0, 1]]

        with pytest.raises(FileNotFoundError, match="Batch file not found"):
            apply_annotation_batch_file(pkg, tmp_path / "missing.json")

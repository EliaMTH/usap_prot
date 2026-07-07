from __future__ import annotations

from pathlib import Path

from conftest import write_tiny_mesh as _write_tiny_mesh
from usap import (
    ELEMENT_KIND_FACE,
    USAPPackage,
    register_mesh_asset,
    seed_default_citygml_vocabulary,
    seed_default_ade_vocabulary,
)


# NOTE: bare concept resolution (name + class_uri, CityGML and ADE) is
# covered by test_external_vocabulary.py and
# test_concept_registry.py::test_get_semantic_class_and_concept_exists;
# this file tests resolution through the annotation API.

def test_annotate_elements_with_citygml_concept(tmp_path: Path) -> None:
    mesh_path = tmp_path / "mesh.ply"
    db_path = tmp_path / "annotate_citygml.usap.gpkg"

    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        seed_default_citygml_vocabulary(pkg)

        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="tiny_mesh",
            representation_kind="triangulated_surface",
            lod=None,
        )

        annotation = pkg.annotate_elements(
            concept="RoofSurface",
            annotation_uid="ann_roofsurface_generic",
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[1],
            label="RoofSurface annotation through generic API",
            status="accepted",
            confidence=1.0,
        )

        assert annotation["annotation_uid"] == "ann_roofsurface_generic"
        assert annotation["semantic_class"] == "RoofSurface"
        assert annotation["status"] == "accepted"
        assert len(annotation["membership_summary"]) == 1
        assert annotation["membership_summary"][0]["selected_count"] == 1

        matches = pkg.annotations_for_elements(
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[1],
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_roofsurface_generic"


def test_annotate_elements_with_ade_concept_and_city_object(tmp_path: Path) -> None:
    mesh_path = tmp_path / "mesh.ply"
    db_path = tmp_path / "annotate_ade.usap.gpkg"

    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        citygml = seed_default_citygml_vocabulary(pkg)
        seed_default_ade_vocabulary(pkg)

        roof_object_id = pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=citygml.by_name["RoofSurface"],
            gml_id="building_1_roof_1",
        )

        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="tiny_mesh",
            representation_kind="triangulated_surface",
            lod=None,
        )

        annotation = pkg.annotate_elements(
            concept="EnergyRoof",
            annotation_uid="ann_energyroof_generic",
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[0, 1],
            city_object_uid="building_1_roof_1",
            label="EnergyRoof annotation through generic API",
            status="draft",
            attributes={
                "domain": "energy_emissions",
                "method": "manual_selection",
                "assessed_at": "2026-06-30T14:00:00Z",
            },
        )

        assert annotation["annotation_uid"] == "ann_energyroof_generic"
        assert annotation["semantic_class"] == "EnergyRoof"
        assert annotation["primary_city_object_uid"] == "building_1_roof_1"
        assert annotation["primary_city_object_id"] == roof_object_id
        assert len(annotation["membership_summary"]) == 1
        assert annotation["membership_summary"][0]["selected_count"] == 2

        by_city_object = pkg.list_annotations(
            city_object_uid="building_1_roof_1",
        )

        assert [item["annotation_uid"] for item in by_city_object] == [
            "ann_energyroof_generic"
        ]


def test_attach_annotation_elements_adds_second_representation(tmp_path: Path) -> None:
    mesh_a_path = tmp_path / "mesh_a.ply"
    mesh_b_path = tmp_path / "mesh_b.ply"
    db_path = tmp_path / "multi_rep.usap.gpkg"

    _write_tiny_mesh(mesh_a_path)
    _write_tiny_mesh(mesh_b_path)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        seed_default_citygml_vocabulary(pkg)
        seed_default_ade_vocabulary(pkg)

        mesh_a = register_mesh_asset(
            pkg,
            mesh_a_path,
            representation_name="mesh_a",
            representation_kind="triangulated_surface",
            lod=None,
        )

        mesh_b = register_mesh_asset(
            pkg,
            mesh_b_path,
            representation_name="mesh_b",
            representation_kind="triangulated_surface",
            lod=None,
        )

        assert mesh_a.primary_asset_part_id != mesh_b.primary_asset_part_id

        annotation = pkg.annotate_elements(
            concept="EnergyRoof",
            annotation_uid="ann_multi_rep",
            asset_part_id=mesh_a.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[0],
        )

        annotation = pkg.attach_annotation_elements(
            annotation_id=int(annotation["annotation_id"]),
            asset_part_id=mesh_b.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[1],
        )

        summary = annotation["membership_summary"]

        assert len(summary) == 2

        selected_counts = {
            item["asset_part_id"]: item["selected_count"]
            for item in summary
        }

        assert selected_counts[mesh_a.primary_asset_part_id] == 1
        assert selected_counts[mesh_b.primary_asset_part_id] == 1
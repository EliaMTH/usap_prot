from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_mesh_part, make_pkg
from conftest import write_tiny_mesh as _write_tiny_mesh
from usap import (
    ELEMENT_KIND_FACE,
    USAPError,
    USAPPackage,
    register_mesh_asset,
    seed_default_citygml_vocabulary,
    seed_default_ade_vocabulary,
)


def test_get_and_update_annotation(tmp_path: Path) -> None:
    db_path = tmp_path / "crud.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        classes = seed_default_citygml_vocabulary(pkg)

        building_id = pkg.create_city_object(
            object_uid="building_1",
            semantic_class_id=classes.by_name["Building"],
            gml_id="building_1",
        )

        roof_id = pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=classes.by_name["RoofSurface"],
            gml_id="building_1_roof_1",
        )

        pkg.link_city_objects(
            parent_city_object_id=building_id,
            child_city_object_id=roof_id,
            relationship_type="boundedBy",
            role="roof",
            graph_name="usap_default",
        )

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_crud_roof",
            semantic_class_id=classes.by_name["RoofSurface"],
            primary_city_object_id=roof_id,
            label="Original roof annotation",
            status="draft",
            confidence=0.25,
            attributes_json=json.dumps({"version": 1}),
        )

        annotation = pkg.get_annotation(annotation_id)

        assert annotation is not None
        assert annotation["annotation_uid"] == "ann_crud_roof"
        assert annotation["semantic_class"] == "RoofSurface"
        assert annotation["primary_city_object_uid"] == "building_1_roof_1"
        assert annotation["label"] == "Original roof annotation"
        assert annotation["status"] == "draft"
        assert annotation["confidence"] == 0.25

        updated = pkg.update_annotation(
            annotation_id,
            label="Updated roof annotation",
            status="accepted",
            confidence=None,
            attributes_json=json.dumps({"version": 2}),
        )

        assert updated["label"] == "Updated roof annotation"
        assert updated["status"] == "accepted"
        assert updated["confidence"] is None
        assert json.loads(updated["attributes_json"]) == {"version": 2}

        # updated_at must track the last edit, not stay frozen at created_at.
        # Backdate the row so the assertion is deterministic despite the
        # one-second resolution of CURRENT_TIMESTAMP, then confirm an update
        # advances updated_at while leaving created_at untouched.
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_annotation
                SET created_at = ?, updated_at = ?
                WHERE annotation_id = ?
                """,
                ("2000-01-01 00:00:00", "2000-01-01 00:00:00", annotation_id),
            )

        pkg.update_annotation(annotation_id, label="Touched roof annotation")

        timestamps = pkg.conn.execute(
            """
            SELECT created_at, updated_at
            FROM usap_annotation
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        assert timestamps["created_at"] == "2000-01-01 00:00:00"
        assert timestamps["updated_at"] > "2000-01-01 00:00:00"


def test_list_annotations_with_filters(tmp_path: Path) -> None:
    db_path = tmp_path / "list.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        classes = seed_default_citygml_vocabulary(pkg)
        ade = seed_default_ade_vocabulary(pkg)

        roof_object_id = pkg.create_city_object(
            object_uid="roof_1",
            semantic_class_id=classes.by_name["RoofSurface"],
            gml_id="roof_1",
        )

        pkg.create_annotation(
            annotation_uid="ann_citygml_roof",
            semantic_class_id=classes.by_name["RoofSurface"],
            primary_city_object_id=roof_object_id,
            label="CityGML roof annotation",
            status="accepted",
        )

        pkg.create_annotation(
            annotation_uid="ann_energy_roof",
            semantic_class_id=ade.by_name["EnergyRoof"],
            primary_city_object_id=roof_object_id,
            label="Energy roof annotation",
            status="draft",
        )

        accepted = pkg.list_annotations(status="accepted")
        drafts = pkg.list_annotations(status="draft")
        energy = pkg.list_annotations(semantic_class_local_name="EnergyRoof")
        by_city_object = pkg.list_annotations(city_object_uid="roof_1")

        assert [item["annotation_uid"] for item in accepted] == [
            "ann_citygml_roof"
        ]

        assert [item["annotation_uid"] for item in drafts] == [
            "ann_energy_roof"
        ]

        assert [item["annotation_uid"] for item in energy] == [
            "ann_energy_roof"
        ]

        assert {
            item["annotation_uid"]
            for item in by_city_object
        } == {
            "ann_citygml_roof",
            "ann_energy_roof",
        }


def test_delete_annotation_cascades_membership(tmp_path: Path) -> None:
    mesh_path = tmp_path / "mesh.ply"
    db_path = tmp_path / "delete.usap.gpkg"

    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        classes = seed_default_citygml_vocabulary(pkg)

        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="tiny_mesh",
            representation_kind="triangulated_surface",
            lod=None,
        )

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_delete_me",
            semantic_class_id=classes.by_name["RoofSurface"],
            label="Delete me",
            status="draft",
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[0, 1],
        )

        before = pkg.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM usap_membership_block
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        assert before["n"] > 0

        deleted = pkg.delete_annotation(annotation_id)

        assert deleted is True
        assert pkg.get_annotation(annotation_id) is None

        after = pkg.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM usap_membership_block
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        assert after["n"] == 0

        assert pkg.delete_annotation(annotation_id, missing_ok=True) is False

def test_create_annotation_rejects_conflicting_concept(tmp_path: Path) -> None:
    # Re-using an annotation_uid with a different concept must raise, not
    # silently replace the existing claim.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")
        pkg.create_semantic_class(scheme="s", class_uri="s:Wall", local_name="Wall")

        pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_x",
            asset_part_id=part,
            element_kind="face",
            element_indices=[1, 2],
        )

        with pytest.raises(USAPError, match="different semantic class"):
            pkg.annotate_elements(
                concept="Wall",
                annotation_uid="ann_x",
                asset_part_id=part,
                element_kind="face",
                element_indices=[50, 51],
            )

        # The rejected call must not have touched the annotation or its
        # membership (the old behavior silently replaced the indices).
        annotation = pkg.get_annotation(annotation_uid="ann_x")

        assert annotation is not None
        assert annotation["semantic_class"] == "Roof"

        blocks = pkg.elements_for_annotation(
            int(annotation["annotation_id"]),
            expand=True,
        )

        assert [block["elements"] for block in blocks] == [[1, 2]]


def test_integrity_violations_raise_usap_error(tmp_path: Path) -> None:
    # Constraint violations must surface as USAPError, not raw sqlite3 errors.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")

        annotation = pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_a",
            asset_part_id=part,
            element_kind="face",
            element_indices=[1],
        )

        with pytest.raises(USAPError, match="constraint"):
            pkg.update_annotation(
                int(annotation["annotation_id"]),
                semantic_class_id=None,
            )

        with pytest.raises(USAPError, match="Annotation not found"):
            pkg.attach_annotation_elements(
                annotation_id=99999,
                asset_part_id=part,
                element_kind="face",
                element_indices=[1],
            )

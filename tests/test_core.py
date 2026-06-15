from __future__ import annotations

from pathlib import Path

from usap import ELEMENT_KIND_FACE, USAPPackage


def build_tiny_package(db_path: Path) -> tuple[USAPPackage, int, int, int]:
    pkg = USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    )

    asset_id = pkg.register_asset(
        uri="city_mesh.glb",
        asset_kind="mesh",
        media_type="model/gltf-binary",
        content_hash="fake_hash_for_test",
    )

    asset_part_id = pkg.register_asset_part(
        asset_id=asset_id,
        part_path="node=0/mesh=0/primitive=0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=10000,
    )

    building_class_id = pkg.create_semantic_class(
        scheme="citygml",
        scheme_version="3.0",
        class_uri="citygml-3.0:building:Building",
        local_name="Building",
    )

    roof_class_id = pkg.create_semantic_class(
        scheme="citygml",
        scheme_version="3.0",
        class_uri="citygml-3.0:building:RoofSurface",
        local_name="RoofSurface",
    )

    building_id = pkg.create_city_object(
        object_uid="building_1",
        semantic_class_id=building_class_id,
    )

    roof_id = pkg.create_city_object(
        object_uid="building_1_roof_1",
        semantic_class_id=roof_class_id,
    )

    pkg.link_city_objects(
        parent_city_object_id=building_id,
        child_city_object_id=roof_id,
        relationship_type="boundedBy",
        role="roof",
        graph_name="usap_default",
    )

    annotation_id = pkg.create_annotation(
        annotation_uid="ann_building_1_roof_mesh",
        semantic_class_id=roof_class_id,
        primary_city_object_id=roof_id,
        label="Roof of building_1 in mesh",
        status="accepted",
        confidence=1.0,
    )

    pkg.replace_annotation_membership(
        annotation_id=annotation_id,
        asset_part_id=asset_part_id,
        element_kind=ELEMENT_KIND_FACE,
        element_indices=[100, 101, 102, 6000, 6001],
    )

    return pkg, asset_part_id, roof_class_id, annotation_id


def test_selected_face_returns_roof_annotation(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    pkg, asset_part_id, _roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        matches = pkg.annotations_for_elements(
            asset_part_id=asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[6000],
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_building_1_roof_mesh"
        assert matches[0]["semantic_class"] == "RoofSurface"
        assert matches[0]["primary_city_object_uid"] == "building_1_roof_1"
        assert matches[0]["matched_elements"] == [6000]

    finally:
        pkg.close()


def test_annotation_membership_is_split_into_two_blocks(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    pkg, _asset_part_id, _roof_class_id, annotation_id = build_tiny_package(db_path)

    try:
        blocks = pkg.elements_for_annotation(
            annotation_id=annotation_id,
            expand=True,
        )

        assert len(blocks) == 2

        block_starts = [block["block_start"] for block in blocks]
        assert block_starts == [0, 4096]

        all_faces = []
        for block in blocks:
            all_faces.extend(block["elements"])

        assert all_faces == [100, 101, 102, 6000, 6001]

    finally:
        pkg.close()


def test_city_object_query_uses_usap_default_descendants(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    pkg, _asset_part_id, _roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        blocks = pkg.elements_for_city_object(
            object_uid="building_1",
            include_descendants=True,
            graph_name="usap_default",
            expand=True,
        )

        assert len(blocks) == 2

        all_faces = []
        for block in blocks:
            all_faces.extend(block["elements"])

        assert all_faces == [100, 101, 102, 6000, 6001]

    finally:
        pkg.close()


def test_validation_is_ok_for_tiny_package(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    pkg, _asset_part_id, _roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        problems = pkg.validate_basic()
        assert problems == []

    finally:
        pkg.close()
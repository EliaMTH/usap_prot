from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import make_mesh_part, make_pkg
from usap import ELEMENT_KIND_FACE, USAPPackage, seed_default_citygml_vocabulary
from usap.constants import normalize_element_kind


def build_tiny_package(db_path: Path) -> tuple[USAPPackage, int, int, int]:
    pkg = USAPPackage.create(
        db_path,
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


def test_city_object_query_finds_annotation_without_object_link(tmp_path: Path) -> None:
    # Hardening regression: an annotation that names a primary city object but
    # has no usap_annotation_object link row must still be returned. The query
    # used to match only via the link table, so such an annotation silently
    # vanished from elements_for_city_object.
    db_path = tmp_path / "test.usap.gpkg"

    pkg, asset_part_id, roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        roof_id = pkg.resolve_city_object("building_1_roof_1")

        unlinked_id = pkg.create_annotation(
            annotation_uid="ann_unlinked_roof",
            semantic_class_id=roof_class_id,
            primary_city_object_id=roof_id,
            status="accepted",
            link_primary_object=False,
        )

        pkg.replace_annotation_membership(
            annotation_id=unlinked_id,
            asset_part_id=asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[7000, 7001],
        )

        blocks = pkg.elements_for_city_object(
            object_uid="building_1",
            include_descendants=True,
            graph_name="usap_default",
            expand=True,
        )

        annotation_ids = {block["annotation_id"] for block in blocks}
        assert unlinked_id in annotation_ids

        # Validation of this fixture rides along here instead of having a
        # dedicated (redundant) test.
        report = pkg.validate_report()
        assert report.issues == [], [i.format() for i in report.issues]

    finally:
        pkg.close()


def test_link_city_objects_is_idempotent(pkg: USAPPackage) -> None:
    # Re-linking an identical edge must return the existing relationship_id
    # instead of inserting a duplicate — CityGML re-imports in update mode
    # rely on this to keep the relationship graph stable.
    parent = pkg.create_city_object(object_uid="b1")
    child = pkg.create_city_object(object_uid="b1_roof")

    first = pkg.link_city_objects(
        parent_city_object_id=parent,
        child_city_object_id=child,
        relationship_type="boundedBy",
        role="roof",
    )
    second = pkg.link_city_objects(
        parent_city_object_id=parent,
        child_city_object_id=child,
        relationship_type="boundedBy",
        role="roof",
    )

    assert second == first

    count = pkg.conn.execute(
        "SELECT COUNT(*) AS n FROM usap_city_object_relationship"
    ).fetchone()["n"]

    assert count == 1

    # A variant edge (different role) is a different claim and must insert.
    third = pkg.link_city_objects(
        parent_city_object_id=parent,
        child_city_object_id=child,
        relationship_type="boundedBy",
        role="wall",
    )

    assert third != first


def test_log_edit_writes_row(pkg: USAPPackage) -> None:
    # The edit log is the package's provenance trail; a custom operation
    # recorded through the public API must land in usap_edit_log.
    pkg.log_edit("custom_op", "usap_asset", 7, details_json='{"why": "test"}')

    row = pkg.conn.execute(
        "SELECT operation, target_table, target_id, details_json, created_at "
        "FROM usap_edit_log WHERE operation = 'custom_op'"
    ).fetchone()

    assert row is not None
    assert row["target_table"] == "usap_asset"
    assert row["target_id"] == 7
    assert row["details_json"] == '{"why": "test"}'
    assert row["created_at"] is not None


def test_create_failure_leaves_no_artifacts(tmp_path: Path) -> None:
    # A failed create must not leave a half-initialized package file (or an
    # open connection) behind, or a retry hits "Database already exists".
    bad_schema = tmp_path / "bad_schema.sql"
    bad_schema.write_text("CREATE TABLE broken (;", encoding="utf-8")

    db_path = tmp_path / "broken.usap.gpkg"

    with pytest.raises(sqlite3.OperationalError):
        USAPPackage.create(db_path, schema_path=bad_schema, overwrite=True)

    assert not db_path.exists()


def test_default_paths_work_from_any_cwd(tmp_path: Path, monkeypatch) -> None:
    # Default schema/vocabulary paths are repo-anchored: creating a package
    # and seeding the default vocabulary must not depend on the process CWD.
    monkeypatch.chdir(tmp_path)

    with USAPPackage.create(tmp_path / "cwd.usap.gpkg", overwrite=True) as pkg:
        vocab = seed_default_citygml_vocabulary(pkg)

        assert "Building" in vocab.by_name


def test_annotations_for_elements_survives_huge_selection(tmp_path: Path) -> None:
    block_size = 4096

    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg, element_count=40_000_000)
        pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")

        hit_low = 0
        hit_high = block_size * 2000

        pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_big",
            asset_part_id=part,
            element_kind="face",
            element_indices=[hit_low, hit_high],
        )

        # One selected index in each of 2500 distinct blocks: more than the
        # 999-variable limit of older SQLite builds, so it must be chunked.
        selected = list(range(0, block_size * 2500, block_size))

        matches = pkg.annotations_for_elements(
            asset_part_id=part,
            element_kind="face",
            selected_indices=selected,
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_big"
        # Hits come from different chunks and must be merged.
        assert matches[0]["matched_elements"] == [hit_low, hit_high]


def test_elements_for_city_object_survives_many_descendants(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")

        with pkg.transaction():
            root_id = pkg.create_city_object(object_uid="root")

            child_ids = []

            for i in range(1000):
                child_id = pkg.create_city_object(object_uid=f"child_{i:04d}")

                pkg.link_city_objects(
                    parent_city_object_id=root_id,
                    child_city_object_id=child_id,
                    relationship_type="contains",
                    rebuild_closure=False,
                )

                child_ids.append(child_id)

            pkg.rebuild_city_object_closure()

        annotation = pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_multi",
            asset_part_id=part,
            element_kind="face",
            element_indices=[1, 2, 3],
            city_object_uid="child_0010",
        )

        # Link the same annotation to an object that lands in a different
        # query chunk; its block must still be returned exactly once.
        pkg.link_annotation_to_object(
            annotation_id=int(annotation["annotation_id"]),
            city_object_id=child_ids[990],
        )

        blocks = pkg.elements_for_city_object("root", expand=True)

        assert len(blocks) == 1
        assert blocks[0]["elements"] == [1, 2, 3]


def test_raw_write_then_sdk_write_both_persist(tmp_path: Path) -> None:
    db_path = tmp_path / "raw.usap.gpkg"

    pkg = USAPPackage.create(db_path, overwrite=True)

    # A raw write opens an implicit sqlite3 transaction. The next SDK write
    # must adopt and commit it instead of silently never committing.
    pkg.conn.execute(
        "INSERT INTO usap_asset (uri, asset_kind) VALUES ('raw.las', 'pointcloud')"
    )

    assert pkg.conn.in_transaction

    pkg.register_asset(uri="sdk.las", asset_kind="pointcloud")
    pkg.close()

    conn = sqlite3.connect(db_path)

    try:
        count = conn.execute("SELECT COUNT(*) FROM usap_asset").fetchone()[0]
    finally:
        conn.close()

    assert count == 2


def test_normalize_element_kind_is_strict() -> None:
    assert normalize_element_kind("vertex") == 3
    assert normalize_element_kind("features") == 4

    with pytest.raises(ValueError):
        normalize_element_kind(99)

    with pytest.raises(ValueError):
        normalize_element_kind("polygon")

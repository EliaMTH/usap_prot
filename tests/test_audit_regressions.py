"""
Regression tests for the bugs found in the 2026-07 repo audit.

Each test pins the fixed behavior of one confirmed bug:
  B1/C9  default schema/vocabulary paths must not depend on the process CWD
  B2     an existing annotation_uid with a different concept must raise
  B3     validate_connection must accept connections without sqlite3.Row
  B4     id lists larger than the SQLite variable limit must be chunked
  B5     raw writes on pkg.conn must not disable SDK transaction commits
  B6     constraint violations must surface as USAPError, not sqlite3 errors
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from usap import (
    ELEMENT_KIND_FACE,
    USAPError,
    USAPPackage,
    seed_default_citygml_vocabulary,
    validate_connection,
)
from usap.constants import normalize_element_kind

SCHEMA_PATH = Path("sql/schema.sql").resolve()


def _make_pkg(tmp_path: Path, name: str = "pkg.usap.gpkg") -> USAPPackage:
    return USAPPackage.create(
        tmp_path / name,
        schema_path=SCHEMA_PATH,
        overwrite=True,
    )


def _mesh_part(pkg: USAPPackage, element_count: int = 100) -> int:
    asset_id = pkg.register_asset(uri="mesh.glb", asset_kind="mesh")

    return pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=element_count,
    )


def test_default_paths_work_from_any_cwd(tmp_path: Path, monkeypatch) -> None:
    # B1 + C9: no schema_path given (anchored default) and the default
    # vocabulary loaded with the CWD pointing away from the repo root.
    monkeypatch.chdir(tmp_path)

    with USAPPackage.create(tmp_path / "cwd.usap.gpkg", overwrite=True) as pkg:
        vocab = seed_default_citygml_vocabulary(pkg)

        assert "Building" in vocab.by_name


def test_create_annotation_rejects_conflicting_concept(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg)
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


def test_validate_connection_accepts_plain_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "plain.usap.gpkg"

    with _make_pkg(tmp_path, "plain.usap.gpkg"):
        pass

    conn = sqlite3.connect(db_path)

    try:
        report = validate_connection(conn)

        assert report.is_ok
        # The caller's row factory must be restored, not hijacked.
        assert conn.row_factory is None
    finally:
        conn.close()


def test_annotations_for_elements_survives_huge_selection(tmp_path: Path) -> None:
    block_size = 4096

    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=40_000_000)
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
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg)
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

    pkg = USAPPackage.create(db_path, schema_path=SCHEMA_PATH, overwrite=True)

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


def test_integrity_violations_raise_usap_error(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg)
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


def test_normalize_element_kind_is_strict() -> None:
    assert normalize_element_kind("vertex") == 3
    assert normalize_element_kind("features") == 4

    with pytest.raises(ValueError):
        normalize_element_kind(99)

    with pytest.raises(ValueError):
        normalize_element_kind("polygon")

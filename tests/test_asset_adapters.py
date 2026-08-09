"""
One parametrized round-trip per asset adapter (LAS, mesh).

The shared body pins the adapter contract: registering an external asset
yields an asset part whose elements can be annotated and queried back.
The per-adapter register helpers pin what each registration must capture
from the source file (the index space, never geometry).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    assert_package_valid,
    make_pkg,
    seed_citygml_concepts,
    write_tiny_las as _write_tiny_las,
    write_tiny_mesh as _write_tiny_mesh,
)
from usap import (
    ELEMENT_KIND_FACE,
    ELEMENT_KIND_POINT,
    USAPError,
    USAPPackage,
    register_las_asset,
    register_mesh_asset,
)


def _register_las(pkg: USAPPackage, tmp_path: Path) -> tuple[int, int]:
    las_path = tmp_path / "tiny.las"
    _write_tiny_las(las_path, point_count=10)

    las = register_las_asset(pkg, las_path)

    # LAS registration must read the point count from the header.
    assert las.asset_id > 0
    assert las.asset_part_id > 0
    assert las.point_count == 10

    return las.asset_part_id, ELEMENT_KIND_POINT


def _register_mesh(pkg: USAPPackage, tmp_path: Path) -> tuple[int, int]:
    mesh_path = tmp_path / "city_triangulation.ply"
    _write_tiny_mesh(mesh_path)

    mesh = register_mesh_asset(
        pkg,
        mesh_path,
        representation_name="city_triangulation",
        representation_kind="triangulated_city_surface",
        lod="LoD2",
    )

    # Mesh registration must capture parts, face counts, and representation
    # metadata (several meshes can represent the same area at different LoDs).
    assert mesh.asset_id > 0
    assert mesh.total_face_count == 2
    assert len(mesh.parts) == 1
    assert mesh.primary_asset_part_id == mesh.parts[0].asset_part_id
    assert mesh.lod == "LoD2"

    metadata = pkg.conn.execute(
        "SELECT metadata_json FROM usap_asset WHERE asset_id = ?",
        (mesh.asset_id,),
    ).fetchone()["metadata_json"]

    assert "LoD2" in metadata
    assert "city_triangulation" in metadata

    return mesh.primary_asset_part_id, ELEMENT_KIND_FACE


@pytest.mark.parametrize(
    ("register", "element_indices", "selected"),
    [
        pytest.param(_register_las, [1, 2, 3], [2], id="las"),
        pytest.param(_register_mesh, [1], [1], id="mesh"),
    ],
)
def test_register_asset_and_annotate_elements(
    tmp_path: Path,
    register,
    element_indices: list[int],
    selected: list[int],
) -> None:
    with make_pkg(tmp_path) as pkg:
        classes = seed_citygml_concepts(pkg)

        part_id, element_kind = register(pkg, tmp_path)

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_adapter_roof",
            semantic_class_id=classes.by_name["RoofSurface"],
            label="Adapter roof elements",
            status="accepted",
            confidence=1.0,
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=part_id,
            element_kind=element_kind,
            element_indices=element_indices,
        )

        matches = pkg.annotations_for_elements(
            asset_part_id=part_id,
            element_kind=element_kind,
            selected_indices=selected,
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_adapter_roof"
        assert matches[0]["semantic_class"] == "RoofSurface"
        assert matches[0]["matched_elements"] == selected

        assert_package_valid(pkg)


def test_gltf_mesh_registration_is_refused(tmp_path: Path) -> None:
    # A glTF scene places geometries with node transforms and can instance one
    # geometry many times. Reading its geometries alone produces bounds in the
    # wrong place and one part where the scene has several instances — wrong
    # data rather than missing data, and nothing downstream could tell. Until
    # scene graphs are supported, registration must refuse the format.
    mesh_path = tmp_path / "city.glb"
    _write_tiny_mesh(mesh_path)

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="glTF-family meshes are not supported"):
            register_mesh_asset(
                pkg,
                mesh_path,
                representation_name="city_lod2",
            )

        # Nothing half-registered is left behind.
        assert pkg.list_assets() == []


@pytest.mark.parametrize(
    "filename, expected_profile",
    [
        ("tiny.ply", "usap:ply-face-record-order-v1"),
        ("tiny.obj", "usap:obj-face-record-order-v1"),
        ("tiny.stl", "usap:stl-face-record-order-v1"),
    ],
)
def test_mesh_registration_records_the_indexing_profile(
    tmp_path: Path,
    filename: str,
    expected_profile: str,
) -> None:
    # A content hash proves the bytes are unchanged; it says nothing about how
    # a reader turns those bytes into face 0, 1, 2. The convention has to be
    # recorded, or two readers can disagree on face order and silently repoint
    # every membership without changing a stored index.
    mesh_path = tmp_path / filename
    _write_tiny_mesh(mesh_path)

    with make_pkg(tmp_path) as pkg:
        register_mesh_asset(pkg, mesh_path, representation_name="tiny")

        rows = pkg.conn.execute(
            "SELECT indexing_profile FROM usap_asset_part"
        ).fetchall()

        assert [r["indexing_profile"] for r in rows] == [expected_profile]


def test_las_registration_records_the_indexing_profile(tmp_path: Path) -> None:
    las_path = tmp_path / "tiny.las"
    _write_tiny_las(las_path, point_count=10)

    with make_pkg(tmp_path) as pkg:
        register_las_asset(pkg, las_path)

        row = pkg.conn.execute(
            "SELECT indexing_profile FROM usap_asset_part"
        ).fetchone()

        assert row["indexing_profile"] == "usap:las-point-record-order-v1"


def test_reregistering_a_part_under_a_different_profile_raises(
    tmp_path: Path,
) -> None:
    # Reading one part under two conventions would repoint its memberships
    # without changing a single stored index, so it must be refused like any
    # other conflicting re-registration.
    with make_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="area.ply", asset_kind="mesh")

        common = {
            "asset_id": asset_id,
            "part_path": "geometry/0",
            "element_kind": ELEMENT_KIND_FACE,
            "element_count": 10,
        }

        first = pkg.register_asset_part(
            **common,
            indexing_profile="usap:ply-face-record-order-v1",
        )

        # Re-registering identically stays idempotent.
        assert (
            pkg.register_asset_part(
                **common,
                indexing_profile="usap:ply-face-record-order-v1",
            )
            == first
        )

        with pytest.raises(USAPError, match="already.*registered with different"):
            pkg.register_asset_part(
                **common,
                indexing_profile="usap:obj-face-record-order-v1",
            )

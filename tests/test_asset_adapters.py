"""
One parametrized round-trip per asset adapter (LAS, mesh).

The shared body pins the adapter contract: registering an external asset
yields an asset part whose elements can be annotated and queried back.
The per-adapter register helpers pin what each registration must capture
from the source file (the index space, never geometry).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh

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
    canonical_hash,
    register_las_asset,
    register_mesh_asset,
    register_mesh_asset_with_geometries,
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


# ---------------------------------------------------------------------------
# register_mesh_asset_with_geometries: the caller supplies what the file holds
# ---------------------------------------------------------------------------


def _facts_from_the_file(path: Path) -> list[tuple[str, int, tuple[float, ...]]]:
    """What the caller's loader would supply: one entry per geometry."""
    mesh = trimesh.load(str(path), process=False)

    # register_mesh_asset names the single geometry of a plain mesh "default".
    return [("default", len(mesh.faces), tuple(mesh.bounds.ravel()))]


def _rows(pkg: USAPPackage) -> dict[str, list[dict]]:
    """Asset and part rows, metadata parsed, minus what only one path writes."""

    def parsed(table: str) -> list[dict]:
        rows = []

        for row in pkg.conn.execute(f"SELECT * FROM {table} ORDER BY 1"):
            item = dict(row)
            metadata = json.loads(item.pop("metadata_json"))
            metadata.pop("facts_source", None)
            metadata.pop("streamed", None)
            rows.append({**item, "metadata": metadata})

        return rows

    return {
        "asset": parsed("usap_asset"),
        "parts": parsed("usap_asset_part"),
        "extent": [
            dict(row) for row in pkg.conn.execute("SELECT * FROM usap_asset_extent")
        ],
    }


@pytest.mark.parametrize("filename", ["tiny.ply", "tiny.obj"])
def test_known_geometries_write_the_rows_register_mesh_asset_writes(
    tmp_path: Path, filename: str
) -> None:
    # Given the facts register_mesh_asset would read, the new path writes the
    # same asset and parts -- uri, hash, media type, part_path, element count,
    # bounds, indexing profile, metadata -- except for the two keys that say
    # where the facts came from.
    mesh_path = tmp_path / filename
    _write_tiny_mesh(mesh_path)
    options = {"representation_name": "city", "lod": "LoD2", "uri": filename}

    with make_pkg(tmp_path, "read.usap.gpkg") as read_pkg:
        read = register_mesh_asset(read_pkg, mesh_path, **options)
        read_rows = _rows(read_pkg)

    with make_pkg(tmp_path, "given.usap.gpkg") as given_pkg:
        given = register_mesh_asset_with_geometries(
            given_pkg, mesh_path, _facts_from_the_file(mesh_path), **options
        )
        given_rows = _rows(given_pkg)
        metadata = given_pkg.conn.execute(
            "SELECT metadata_json FROM usap_asset"
        ).fetchone()[0]

        # Idempotent with the same facts; the other path's metadata differs,
        # so registering the file through it too is a conflicting
        # re-registration, as with any other changed metadata.
        assert register_mesh_asset_with_geometries(
            given_pkg, mesh_path, _facts_from_the_file(mesh_path), **options
        ) == given

        with pytest.raises(USAPError, match="already registered"):
            register_mesh_asset(given_pkg, mesh_path, **options)

    assert given == read
    assert given_rows == read_rows
    assert json.loads(metadata)["facts_source"] == "caller"
    assert json.loads(metadata)["streamed"] is None


def test_known_geometries_are_not_read_from_the_file(
    tmp_path: Path, monkeypatch
) -> None:
    # The point of the function: the file is read for its hash and for
    # nothing else -- and not at all with compute_hash=False. A file no mesh
    # reader could parse registers fine, which no reading path could do.
    import usap.adapters.mesh_adapter as mesh_adapter

    not_a_mesh = tmp_path / "huge.obj"
    not_a_mesh.write_bytes(b"\x00 this is not an OBJ file \xff")
    facts = [("default", 18_775_443, (0.0, 0.0, 0.0, 10.0, 10.0, 5.0))]

    hashed: list[Path] = []
    real_hash = mesh_adapter.canonical_hash

    def spy_hash(path: Path) -> str:
        hashed.append(Path(path))
        return real_hash(path)

    monkeypatch.setattr(mesh_adapter, "canonical_hash", spy_hash)

    with make_pkg(tmp_path) as pkg:
        unhashed = register_mesh_asset_with_geometries(
            pkg, not_a_mesh, facts,
            representation_name="city", uri="unhashed.obj", compute_hash=False,
        )

        assert hashed == []

        hashed_result = register_mesh_asset_with_geometries(
            pkg, not_a_mesh, facts, representation_name="city", uri="hashed.obj",
        )

        assert hashed == [not_a_mesh]
        assert unhashed.total_face_count == hashed_result.total_face_count == 18_775_443

        stored = dict(
            pkg.conn.execute("SELECT uri, content_hash FROM usap_asset").fetchall()
        )

    assert stored == {
        "unhashed.obj": None,
        "hashed.obj": canonical_hash(not_a_mesh),
    }


def test_known_geometries_keep_the_part_index_of_skipped_ones(tmp_path: Path) -> None:
    # Several geometries, one without faces: it is skipped as register_mesh_asset
    # skips one, and the others keep their position in part_path. numpy scalars
    # are what a loader hands over, and are accepted.
    mesh_path = tmp_path / "district.ply"
    _write_tiny_mesh(mesh_path)
    facts = [
        ("roofs", np.int64(120), np.array([0, 0, 10, 50, 40, 25], dtype=np.float64)),
        ("empty", 0, None),
        ("walls", 300, None),
    ]

    with make_pkg(tmp_path) as pkg:
        result = register_mesh_asset_with_geometries(
            pkg, mesh_path, facts, representation_name="district",
        )
        parts = pkg.list_asset_parts(asset_id=result.asset_id)
        part_metadata = [
            json.loads(row["metadata_json"])
            for row in pkg.conn.execute(
                "SELECT metadata_json FROM usap_asset_part ORDER BY asset_part_id"
            )
        ]
        asset_metadata = json.loads(
            pkg.conn.execute("SELECT metadata_json FROM usap_asset").fetchone()[0]
        )

        assert_package_valid(pkg)

    assert [p["part_path"] for p in parts] == ["geometry/0:roofs", "geometry/2:walls"]
    assert [p["element_count"] for p in parts] == [120, 300]
    assert [p["indexing_profile"] for p in parts] == [
        "usap:ply-face-record-order-v1"
    ] * 2
    assert (result.parts[0].minx, result.parts[0].maxz) == (0.0, 25.0)
    assert result.parts[1].minx is None
    assert result.total_face_count == 420
    assert asset_metadata["geometry_count"] == 3
    assert {m["facts_source"] for m in part_metadata} == {"caller"}


@pytest.mark.parametrize(
    ("geometries", "message"),
    [
        pytest.param([("a", -1, None)], "negative", id="negative-count"),
        pytest.param([("a", 2.0, None)], "integer", id="float-count"),
        pytest.param([("a", True, None)], "integer", id="bool-count"),
        pytest.param([("a", 2, None), ("a", 3, None)], "twice", id="duplicate-name"),
        pytest.param([(7, 2, None)], "string", id="name-not-a-string"),
        pytest.param([("a", 2)], "must be", id="not-a-triple"),
        pytest.param([("a", 2, (0, 0, 0, 1, 1))], "six finite", id="five-bounds"),
        pytest.param(
            [("a", 2, (0, 0, 0, 1, float("nan"), 1))], "six finite", id="nan-bound"
        ),
        pytest.param(
            [("a", 2, ((0, 0, 0), (1, 1, 1)))], "ravel", id="trimesh-shaped-bounds"
        ),
        pytest.param([("a", 2, (0, 0, 5, 1, 1, 1))], "min greater", id="min-above-max"),
    ],
)
def test_inconsistent_known_geometries_are_refused(
    tmp_path: Path, geometries, message: str
) -> None:
    mesh_path = tmp_path / "city.ply"
    _write_tiny_mesh(mesh_path)

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match=message):
            register_mesh_asset_with_geometries(
                pkg, mesh_path, geometries, representation_name="city"
            )

        assert pkg.list_assets() == []


@pytest.mark.parametrize(
    ("geometries", "message"),
    [
        pytest.param([], "No triangular mesh geometries", id="none"),
        pytest.param(
            [("a", 0, None), ("b", 0, None)], "No mesh parts with faces", id="no-faces"
        ),
    ],
)
def test_known_geometries_without_faces_raise_like_register_mesh_asset(
    tmp_path: Path, geometries, message: str
) -> None:
    mesh_path = tmp_path / "city.ply"
    _write_tiny_mesh(mesh_path)

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(ValueError, match=message):
            register_mesh_asset_with_geometries(
                pkg, mesh_path, geometries, representation_name="city"
            )

        assert pkg.list_assets() == []


def test_known_geometries_keep_the_file_checks(tmp_path: Path) -> None:
    # The file is not read, but it must exist, and glTF stays refused: what is
    # registered is still a file, and its scene graph would still be lost.
    facts = [("default", 2, None)]

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(FileNotFoundError):
            register_mesh_asset_with_geometries(
                pkg, tmp_path / "missing.obj", facts, representation_name="city"
            )

        gltf = tmp_path / "city.glb"
        _write_tiny_mesh(gltf)

        with pytest.raises(USAPError, match="glTF-family meshes are not supported"):
            register_mesh_asset_with_geometries(
                pkg, gltf, facts, representation_name="city"
            )

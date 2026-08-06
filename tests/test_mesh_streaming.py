"""
Streaming mesh registration.

Registration is the only step that opens a mesh file, and it needs two facts
from it: the face count (the index space annotations are written against) and
the bounding box. trimesh.load produces both by materializing the mesh, which
does not survive a 10 GB asset. These tests pin that the streaming readers
produce *identical* facts — a registration that disagreed with the loaded one
would silently change what an element index means.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from conftest import make_pkg
from usap import USAPError, register_mesh_asset


def _write_grid_mesh(path: Path, rows: int = 12, encoding: str | None = None) -> None:
    """A deterministic, non-degenerate mesh with distinct bounds per axis."""
    xs, ys = np.meshgrid(np.arange(rows), np.arange(rows))

    vertices = np.column_stack(
        [
            xs.ravel().astype(float),
            ys.ravel().astype(float) * 2.0,
            (xs.ravel() + ys.ravel()).astype(float) * 0.5,
        ]
    )

    faces = []

    for row in range(rows - 1):
        for col in range(rows - 1):
            a = row * rows + col
            faces.append([a, a + 1, a + rows])
            faces.append([a + 1, a + rows + 1, a + rows])

    mesh = trimesh.Trimesh(
        vertices=vertices, faces=np.array(faces), process=False
    )

    if encoding is None:
        mesh.export(path)
    else:
        mesh.export(path, encoding=encoding)


@pytest.mark.parametrize(
    ("filename", "encoding"),
    [
        pytest.param("grid.ply", None, id="ply-binary"),
        pytest.param("grid_ascii.ply", "ascii", id="ply-ascii"),
        pytest.param("grid.obj", None, id="obj"),
    ],
)
def test_streamed_registration_matches_loaded_registration(
    tmp_path: Path,
    filename: str,
    encoding: str | None,
) -> None:
    # The property that makes streaming safe to switch on by file size: the
    # two paths must record the same index space and the same bounds, or the
    # meaning of a face index would depend on how big the file happened to be.
    mesh_path = tmp_path / filename
    _write_grid_mesh(mesh_path, encoding=encoding)

    def register(stream: bool) -> dict:
        with make_pkg(tmp_path, name=f"{stream}.usap.gpkg") as pkg:
            result = register_mesh_asset(
                pkg,
                mesh_path,
                representation_name="grid",
                stream=stream,
            )

            part = result.parts[0]

            return {
                "total_face_count": result.total_face_count,
                "part_count": len(result.parts),
                "face_count": part.face_count,
                "bounds": [
                    part.minx, part.miny, part.minz,
                    part.maxx, part.maxy, part.maxz,
                ],
            }

    streamed = register(stream=True)
    loaded = register(stream=False)

    assert streamed["total_face_count"] == loaded["total_face_count"] == 242
    assert streamed["part_count"] == loaded["part_count"] == 1
    assert streamed["face_count"] == loaded["face_count"]
    assert streamed["bounds"] == pytest.approx(loaded["bounds"])


def test_large_files_stream_by_default(tmp_path: Path) -> None:
    # The threshold is what makes this reachable without every caller opting
    # in, so the decision itself is recorded on the asset.
    from usap.adapters.mesh_adapter import MESH_STREAM_THRESHOLD_BYTES

    mesh_path = tmp_path / "small.ply"
    _write_grid_mesh(mesh_path)

    assert mesh_path.stat().st_size < MESH_STREAM_THRESHOLD_BYTES

    with make_pkg(tmp_path) as pkg:
        result = register_mesh_asset(
            pkg, mesh_path, representation_name="grid"
        )

        metadata = pkg.conn.execute(
            "SELECT metadata_json FROM usap_asset WHERE asset_id = ?",
            (result.asset_id,),
        ).fetchone()["metadata_json"]

        assert '"streamed": false' in metadata


def test_grouped_obj_is_refused_when_streaming(tmp_path: Path) -> None:
    # A normal load splits a grouped OBJ into one geometry per group, i.e.
    # several asset parts with different part_paths — and annotations are
    # bound to those names. A streaming pass sees one file. Rather than
    # register the same file under a different part identity depending on its
    # size, refuse and say why.
    mesh_path = tmp_path / "grouped.obj"
    mesh_path.write_text(
        "o first\n"
        "v 0 0 0\nv 1 0 0\nv 1 1 0\n"
        "f 1 2 3\n"
        "o second\n"
        "v 2 0 0\nv 3 0 0\nv 3 1 0\n"
        "f 4 5 6\n",
        encoding="utf-8",
    )

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="object/group names"):
            register_mesh_asset(
                pkg,
                mesh_path,
                representation_name="grouped",
                stream=True,
            )


def test_streaming_an_unsupported_format_is_refused(tmp_path: Path) -> None:
    # STL is a supported mesh format but has no streaming reader; the caller
    # must be told to load it rather than get a wrong answer.
    mesh_path = tmp_path / "grid.stl"
    _write_grid_mesh(mesh_path)

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="No streaming reader"):
            register_mesh_asset(
                pkg,
                mesh_path,
                representation_name="grid",
                stream=True,
            )

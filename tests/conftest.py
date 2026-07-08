from __future__ import annotations

from pathlib import Path
from typing import Iterator

import laspy
import numpy as np
import pytest
import trimesh

from usap import ELEMENT_KIND_FACE, USAPPackage

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"


def make_pkg(tmp_path: Path, name: str = "pkg.usap.gpkg") -> USAPPackage:
    """Create a fresh empty package under tmp_path."""
    return USAPPackage.create(
        tmp_path / name,
        schema_path=SCHEMA_PATH,
        overwrite=True,
    )


def make_mesh_part(pkg: USAPPackage, element_count: int = 100) -> int:
    """Register a bare mesh asset with one face part; returns asset_part_id."""
    asset_id = pkg.register_asset(uri="mesh.glb", asset_kind="mesh")

    return pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=element_count,
    )


@pytest.fixture
def pkg(tmp_path: Path) -> Iterator[USAPPackage]:
    with make_pkg(tmp_path) as p:
        yield p


@pytest.fixture
def mesh_part(pkg: USAPPackage) -> int:
    return make_mesh_part(pkg)


def assert_package_valid(pkg) -> None:
    """
    Assert validate_report() is clean, printing the issues on failure
    (a bare `assert report.is_ok` fails without saying why).
    """
    report = pkg.validate_report()
    assert report.is_ok, [issue.format() for issue in report.issues]


def write_tiny_las(path: Path, point_count: int = 10) -> None:
    """
    Write a minimal LAS file with `point_count` points for tests.

    Coordinates are distinct per axis (x, x+100, x+200) so bounds are non-degenerate.
    """
    header = laspy.LasHeader(point_format=3, version="1.2")
    las = laspy.LasData(header)

    las.x = np.arange(point_count, dtype=float)
    las.y = np.arange(point_count, dtype=float) + 100.0
    las.z = np.arange(point_count, dtype=float) + 200.0

    las.write(path)


def write_tiny_mesh(path: Path) -> None:
    """
    Write a minimal 2-triangle quad mesh for tests.

    The export format is inferred from the file suffix (e.g. .ply, .obj).
    """
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

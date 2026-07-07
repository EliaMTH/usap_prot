from __future__ import annotations

from pathlib import Path

import laspy
import numpy as np
import trimesh


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

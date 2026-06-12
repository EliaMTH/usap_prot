from __future__ import annotations

from pathlib import Path

import laspy
import numpy as np

from usap import (
    ELEMENT_KIND_POINT,
    USAPPackage,
    register_las_asset,
    seed_citygml_basic_classes,
)


def _write_tiny_las(path: Path, point_count: int = 10) -> None:
    header = laspy.LasHeader(point_format=3, version="1.2")
    las = laspy.LasData(header)

    las.x = np.arange(point_count, dtype=float)
    las.y = np.arange(point_count, dtype=float) + 100.0
    las.z = np.arange(point_count, dtype=float) + 200.0

    las.write(path)


def test_register_las_asset_and_annotate_points(tmp_path: Path) -> None:
    las_path = tmp_path / "tiny.las"
    db_path = tmp_path / "tiny_las.usap.gpkg"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        classes = seed_citygml_basic_classes(pkg)

        las = register_las_asset(pkg, las_path)

        assert las.point_count == 10
        assert las.asset_id > 0
        assert las.asset_part_id > 0

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_tiny_roof_points",
            semantic_class_id=classes.by_name["RoofSurface"],
            label="Tiny roof points",
            status="accepted",
            confidence=1.0,
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            element_indices=[1, 2, 3],
        )

        matches = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            selected_indices=[2],
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_tiny_roof_points"
        assert matches[0]["semantic_class"] == "RoofSurface"
        assert matches[0]["matched_elements"] == [2]

        report = pkg.validate_report()
        assert report.is_ok
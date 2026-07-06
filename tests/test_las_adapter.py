from __future__ import annotations

from pathlib import Path

from conftest import write_tiny_las as _write_tiny_las
from usap import (
    ELEMENT_KIND_POINT,
    USAPPackage,
    register_las_asset,
    seed_default_citygml_vocabulary,
)


def test_register_las_asset_and_annotate_points(tmp_path: Path) -> None:
    las_path = tmp_path / "tiny.las"
    db_path = tmp_path / "tiny_las.usap.gpkg"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        classes = seed_default_citygml_vocabulary(pkg)

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
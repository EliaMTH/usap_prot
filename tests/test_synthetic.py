from __future__ import annotations

from pathlib import Path

from usap import (
    ELEMENT_KIND_FACE,
    SyntheticConfig,
    USAPPackage,
    create_synthetic_package,
)


def test_synthetic_package_can_be_created(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.usap.gpkg"

    result = create_synthetic_package(
        db_path,
        schema_path="sql/schema.sql",
        config=SyntheticConfig(
            building_count=10,
            roof_faces_per_building=20,
            wall_faces_per_building=30,
            ground_faces_per_building=10,
        ),
        overwrite=True,
    )

    assert result.building_count == 10
    assert result.annotation_count == 30
    assert result.total_face_count == 600
    assert db_path.exists()


def test_synthetic_selected_roof_face_returns_roof_annotation(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.usap.gpkg"

    result = create_synthetic_package(
        db_path,
        schema_path="sql/schema.sql",
        config=SyntheticConfig(
            building_count=10,
            roof_faces_per_building=20,
            wall_faces_per_building=30,
            ground_faces_per_building=10,
        ),
        overwrite=True,
    )

    with USAPPackage.open(db_path) as pkg:
        matches = pkg.annotations_for_elements(
            asset_part_id=result.asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[0],
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_building_000000_roof_mesh"
        assert matches[0]["semantic_class"] == "RoofSurface"
        assert matches[0]["primary_city_object_uid"] == "building_000000_roof"
        assert matches[0]["matched_elements"] == [0]


def test_synthetic_city_object_query_returns_building_parts(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.usap.gpkg"

    create_synthetic_package(
        db_path,
        schema_path="sql/schema.sql",
        config=SyntheticConfig(
            building_count=10,
            roof_faces_per_building=20,
            wall_faces_per_building=30,
            ground_faces_per_building=10,
        ),
        overwrite=True,
    )

    with USAPPackage.open(db_path) as pkg:
        blocks = pkg.elements_for_city_object(
            object_uid="building_000000",
            include_descendants=True,
            graph_name="usap_default",
            expand=True,
        )

        all_faces = []
        for block in blocks:
            all_faces.extend(block["elements"])

        assert sorted(all_faces) == list(range(0, 60))


def test_synthetic_semantic_class_query_returns_roof_blocks(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.usap.gpkg"

    result = create_synthetic_package(
        db_path,
        schema_path="sql/schema.sql",
        config=SyntheticConfig(
            building_count=10,
            roof_faces_per_building=20,
            wall_faces_per_building=30,
            ground_faces_per_building=10,
        ),
        overwrite=True,
    )

    with USAPPackage.open(db_path) as pkg:
        blocks = pkg.elements_for_semantic_class(
            semantic_class_id=result.roof_class_id,
            include_subclasses=True,
            expand=True,
        )

        all_roof_faces = []
        for block in blocks:
            all_roof_faces.extend(block["elements"])

        expected_roof_faces = []

        faces_per_building = 20 + 30 + 10

        for i in range(10):
            base = i * faces_per_building
            expected_roof_faces.extend(range(base, base + 20))

        assert sorted(all_roof_faces) == expected_roof_faces

def test_selected_faces_across_multiple_blocks_return_annotations(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic_multiblock.usap.gpkg"

    result = create_synthetic_package(
        db_path,
        schema_path="sql/schema.sql",
        config=SyntheticConfig(
            building_count=20,
            roof_faces_per_building=120,
            wall_faces_per_building=300,
            ground_faces_per_building=80,
        ),
        overwrite=True,
    )

    with USAPPackage.open(db_path) as pkg:
        selected_faces = [0, 4096, 8192]

        matches = pkg.annotations_for_elements(
            asset_part_id=result.asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=selected_faces,
        )

        matched_faces = []

        for match in matches:
            matched_faces.extend(match["matched_elements"])

        assert sorted(matched_faces) == selected_faces
        assert len(matches) >= 1
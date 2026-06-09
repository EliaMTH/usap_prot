from __future__ import annotations

from usap import (
    ELEMENT_KIND_FACE,
    SyntheticConfig,
    USAPPackage,
    create_synthetic_package,
)


def main() -> None:
    result = create_synthetic_package(
        "synthetic_100.usap.gpkg",
        schema_path="sql/schema.sql",
        config=SyntheticConfig(
            building_count=100,
            roof_faces_per_building=120,
            wall_faces_per_building=300,
            ground_faces_per_building=80,
        ),
        overwrite=True,
    )

    print("Created:", result.db_path)
    print("Buildings:", result.building_count)
    print("Annotations:", result.annotation_count)
    print("Total synthetic faces:", result.total_face_count)
    print("Asset part ID:", result.asset_part_id)
    print()

    with USAPPackage.open(result.db_path) as pkg:
        selected_faces = [0, 10, 119]

        matches = pkg.annotations_for_elements(
            asset_part_id=result.asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=selected_faces,
        )

        print("Selected faces:", selected_faces)

        for match in matches:
            print("Annotation:", match["annotation_uid"])
            print("Semantic class:", match["semantic_class"])
            print("City object:", match["primary_city_object_uid"])
            print("Matched faces:", match["matched_elements"])
            print()

        blocks = pkg.elements_for_city_object(
            object_uid="building_000000",
            include_descendants=True,
            graph_name="usap_default",
            expand=False,
        )

        print("Compact blocks for building_000000:", len(blocks))

        roof_blocks = pkg.elements_for_semantic_class(
            semantic_class_id=result.roof_class_id,
            include_subclasses=True,
            expand=False,
        )

        print("Compact RoofSurface blocks:", len(roof_blocks))


if __name__ == "__main__":
    main()
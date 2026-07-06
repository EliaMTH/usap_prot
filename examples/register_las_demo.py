from __future__ import annotations

import argparse
import json

from usap import (
    ELEMENT_KIND_POINT,
    USAPPackage,
    register_las_asset,
    seed_default_citygml_vocabulary,
    seed_default_ade_vocabulary,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register a LAS file and create a prototype point annotation."
    )

    parser.add_argument("las_path")
    parser.add_argument(
        "--db",
        default="prototype_las.usap.gpkg",
    )

    args = parser.parse_args()

    with USAPPackage.create(
        args.db,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        citygml_classes = seed_default_citygml_vocabulary(pkg)
        ade_classes = seed_default_ade_vocabulary(pkg)

        las = register_las_asset(pkg, args.las_path)

        print("Registered LAS:")
        print("  asset_id:", las.asset_id)
        print("  asset_part_id:", las.asset_part_id)
        print("  point_count:", las.point_count)
        print("  bounds:", (las.minx, las.miny, las.minz, las.maxx, las.maxy, las.maxz))

        # Example 1: CityGML-style roof annotation over LAS points.
        roof_annotation_id = pkg.create_annotation(
            annotation_uid="ann_demo_roof_las_points",
            semantic_class_id=citygml_classes.by_name["RoofSurface"],
            label="Demo roof LAS point annotation",
            status="accepted",
            confidence=1.0,
        )

        demo_points = list(range(min(10, las.point_count)))

        pkg.replace_annotation_membership(
            annotation_id=roof_annotation_id,
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            element_indices=demo_points,
        )

        # Example 2: ADE/domain annotation over the same point indices.
        energy_attributes = {
            "domain": "energy_emissions",
            "geometric_attributes": {
                "roof_slope": None,
                "footprint_area": None,
                "height": None,
                "orientation": None,
                "shading": None,
            },
            "non_geometric_attributes": {
                "construction_era": None,
                "use": None,
                "archetype": None,
                "technology": None,
                "conservation_state": None,
            },
            "derived_indicators": {
                "specific_energy_kwh_m2": None,
                "co2_emissions": None,
            },
        }

        energy_annotation_id = pkg.create_annotation(
            annotation_uid="ann_demo_energy_roof_las_points",
            semantic_class_id=ade_classes.by_name["EnergyRoof"],
            label="Demo EnergyRoof ADE annotation",
            status="draft",
            confidence=None,
            attributes_json=json.dumps(energy_attributes),
        )

        pkg.replace_annotation_membership(
            annotation_id=energy_annotation_id,
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            element_indices=demo_points,
        )

        matches = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            selected_indices=[demo_points[0]],
        )

        print()
        print("Annotations for selected LAS point:", demo_points[0])

        for match in matches:
            print("  -", match["annotation_uid"], match["semantic_class"])

        report = pkg.validate_report()

        print()
        report.print()


if __name__ == "__main__":
    main()
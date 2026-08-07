from __future__ import annotations

import argparse

from usap import USAPPackage, import_citygml_semantics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import CityGML semantic objects into a USAP package."
    )

    parser.add_argument("citygml_path")

    parser.add_argument(
        "--db",
        default="prototype_citygml.usap.gpkg",
    )

    args = parser.parse_args()

    with USAPPackage.create(
        args.db,
        overwrite=True,
    ) as pkg:
        result = import_citygml_semantics(pkg, args.citygml_path)

        print("Imported CityGML semantic source:")
        print("  source asset_id:", result.asset_id)
        print("  objects:", result.object_count)
        print("  relationships:", result.relationship_count)
        print()

        print("First imported objects:")

        for item in result.imported_objects[:20]:
            print(
                " ",
                item.object_uid,
                item.local_name,
                f"city_object_id={item.city_object_id}",
            )

        print()

        print("First imported relationships:")

        for rel in result.imported_relationships[:20]:
            print(
                " ",
                rel.graph_name,
                rel.parent_uid,
                "->",
                rel.child_uid,
                rel.relationship_type,
                rel.role,
            )

        print()

        report = pkg.validate_report()
        report.print()


if __name__ == "__main__":
    main()
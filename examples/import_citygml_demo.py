from __future__ import annotations

import argparse

from usap import USAPPackage, import_citygml_semantics, load_citygml_schema


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import CityGML semantic objects into a USAP package."
    )

    parser.add_argument("citygml_path")

    parser.add_argument(
        "--citygml-schema",
        required=True,
        help=(
            "Path to the OGC CityGML 3.0 XSDs (a directory or a single .xsd). "
            "Concepts are a precondition of the import, and USAP ships no "
            "CityGML vocabulary of its own. Get the schemas from "
            "schemas.opengis.net/citygml/citygml-3_0_0.zip."
        ),
    )

    parser.add_argument(
        "--db",
        default="prototype_citygml.usap.gpkg",
    )

    args = parser.parse_args()

    with USAPPackage.create(
        args.db,
        overwrite=True,
    ) as pkg:
        load_citygml_schema(pkg, args.citygml_schema, scheme_version="3.0")

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
                rel.from_uid,
                "->",
                # An xlink that leaves the document has no local target; the
                # link is still recorded, pointing at the URI it named.
                rel.to_uid or f"<{rel.to_external_uri}>",
                f"{rel.relationship_type} ({rel.code_space})",
                rel.role or "",
            )

        print()

        if result.unresolved_targets:
            print(
                f"{len(result.unresolved_targets)} target(s) outside this "
                "document:"
            )

            for target in result.unresolved_targets[:10]:
                print(
                    " ", target.from_uid, "-", target.relationship_type,
                    "->", target.href,
                )

            print()

        if result.skipped_references:
            print("References that are not city-object links:")

            for reference in result.skipped_references[:10]:
                print("  ", reference)

            print()

        report = pkg.validate_report()
        report.print()


if __name__ == "__main__":
    main()
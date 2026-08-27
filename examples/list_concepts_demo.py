from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401  -- puts src/ on sys.path from a checkout

from usap import (
    USAPPackage,
    import_citygml_semantics,
    load_citygml_schema,
    seed_default_ade_vocabulary,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List accepted USAP concepts."
    )

    parser.add_argument(
        "--db",
        default=None,
        help="Optional existing USAP package to inspect.",
    )

    parser.add_argument(
        "--citygml",
        default=None,
        help="Optional CityGML file to import before listing concepts.",
    )

    parser.add_argument(
        "--search",
        default=None,
    )

    parser.add_argument(
        "--used",
        action="store_true",
        help="Only show concepts referenced by at least one annotation.",
    )

    parser.add_argument(
        "--citygml-schema",
        required=True,
        help=(
            "Path to the OGC CityGML 3.0 XSDs (a directory or a single .xsd). "
            "USAP ships no CityGML vocabulary: concepts are read from the "
            "schema you supply. Get them from "
            "schemas.opengis.net/citygml/citygml-3_0_0.zip."
        ),
    )

    args = parser.parse_args()

    in_use = True if args.used else None

    if args.db is None:
        db_path = "concept_registry_demo.usap.gpkg"

        with USAPPackage.create(
            db_path,
            overwrite=True,
        ) as pkg:
            load_citygml_schema(
                pkg, args.citygml_schema, scheme_version="3.0"
            )
            seed_default_ade_vocabulary(pkg)

            if args.citygml is not None:
                import_citygml_semantics(pkg, args.citygml)

            concepts = pkg.list_accepted_concepts(
                search=args.search,
                in_use=in_use,
            )

            print("Created demo package:", db_path)
            print()
            _print_concepts(concepts)

    else:
        with USAPPackage.open(args.db) as pkg:
            concepts = pkg.list_accepted_concepts(
                search=args.search,
                in_use=in_use,
            )
            _print_concepts(concepts)


def _print_concepts(concepts: list[dict]) -> None:
    print("Accepted concepts:")
    print()

    for item in concepts:
        kind = "ADE" if item["is_ade"] else "standard"

        if item["in_use"]:
            usage = f"used:{item['annotation_count']}"
        else:
            usage = "unused"

        print(
            f"- {item['local_name']} "
            f"[{kind}] "
            f"[{usage}] "
            f"scheme={item['scheme']} "
            f"uri={item['class_uri']}"
        )


if __name__ == "__main__":
    main()

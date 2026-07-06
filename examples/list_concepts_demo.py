from __future__ import annotations

import argparse

from usap import (
    USAPPackage,
    import_citygml_semantics,
    seed_default_citygml_vocabulary,
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

    args = parser.parse_args()

    if args.db is None:
        db_path = "concept_registry_demo.usap.gpkg"

        with USAPPackage.create(
            db_path,
            schema_path="sql/schema.sql",
            overwrite=True,
        ) as pkg:
            seed_default_citygml_vocabulary(pkg)
            seed_default_ade_vocabulary(pkg)

            if args.citygml is not None:
                import_citygml_semantics(pkg, args.citygml)

            concepts = pkg.list_accepted_concepts(search=args.search)

            print("Created demo package:", db_path)
            print()
            _print_concepts(concepts)

    else:
        with USAPPackage.open(args.db) as pkg:
            concepts = pkg.list_accepted_concepts(search=args.search)
            _print_concepts(concepts)


def _print_concepts(concepts: list[dict]) -> None:
    print("Accepted concepts:")
    print()

    for item in concepts:
        kind = "ADE" if item["is_ade"] else "standard"

        print(
            f"- {item['local_name']} "
            f"[{kind}] "
            f"scheme={item['scheme']} "
            f"uri={item['class_uri']}"
        )


if __name__ == "__main__":
    main()
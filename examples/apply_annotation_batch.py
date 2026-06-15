from __future__ import annotations

import argparse

from usap import USAPPackage, apply_annotation_batch_file


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply a JSON annotation batch to an existing USAP package."
    )

    parser.add_argument("db")
    parser.add_argument("batch_json")

    parser.add_argument(
        "--replace-existing",
        action="store_true",
    )

    args = parser.parse_args()

    with USAPPackage.open(args.db) as pkg:
        result = apply_annotation_batch_file(
            pkg,
            args.batch_json,
            replace_existing=args.replace_existing,
        )

        print("Applied annotation batch")
        print("  annotations:", result.annotation_count)
        print("  memberships:", result.membership_count)

        for item in result.annotations:
            print(
                "  -",
                item.annotation_uid,
                "concept=",
                item.concept,
                "memberships=",
                item.membership_count,
            )

        print()

        report = pkg.validate_report()
        report.print()


if __name__ == "__main__":
    main()
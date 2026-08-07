from __future__ import annotations

import argparse

from usap import VALIDATION_LEVELS, USAPPackage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a USAP package."
    )

    parser.add_argument(
        "db",
        help="Path to .usap.gpkg file.",
    )

    parser.add_argument(
        "--level",
        choices=VALIDATION_LEVELS,
        default="deep",
        help=(
            "basic: SQL structure only, no payload decoding. "
            "deep (default): also decode every payload, check the object "
            "graph and annotation domain values. "
            "external: also re-hash every registered asset file."
        ),
    )

    args = parser.parse_args()

    with USAPPackage.open(args.db) as pkg:
        report = pkg.validate_report(level=args.level)

        report.print()

        if report.is_ok:
            raise SystemExit(0)

        raise SystemExit(1)


if __name__ == "__main__":
    main()
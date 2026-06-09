from __future__ import annotations

import argparse

from usap import USAPPackage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a USAP package."
    )

    parser.add_argument(
        "db",
        help="Path to .usap.gpkg file.",
    )

    args = parser.parse_args()

    with USAPPackage.open(args.db) as pkg:
        report = pkg.validate_report()

        report.print()

        if report.is_ok:
            raise SystemExit(0)

        raise SystemExit(1)


if __name__ == "__main__":
    main()
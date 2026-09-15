"""
The `usap` console script.

A package is a file, and a file should be checkable without writing Python.
This is the same validation `USAPPackage.validate_report` runs — no separate
implementation, no second set of rules — wrapped so a CI pipeline, a Makefile
or a C++ shop with no Python of its own can gate on it.

A subcommand rather than a bare `usap-validate`: `usap info` and
`usap verify-assets` are the obvious next asks, and they should not each need
their own entry point.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .core import USAPPackage
from .errors import USAPError
from .validation import VALIDATION_LEVELS


def _add_validate_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "validate",
        help="Validate a USAP package.",
        description="Validate a USAP package.",
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

    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit the report as JSON on stdout instead of the human format. "
            "A gate that has to parse the human format is a gate that breaks "
            "when the wording improves."
        ),
    )

    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help=(
            "Exit non-zero on warnings too. The default follows report.is_ok, "
            "which counts errors only; a pipeline should say which it means "
            "rather than inherit it."
        ),
    )

    parser.set_defaults(handler=_run_validate)


def _run_validate(args: argparse.Namespace) -> int:
    with USAPPackage.open(args.db) as pkg:
        report = pkg.validate_report(level=args.level)

    # bool(), because report.warnings is a list of issues: without it `failed`
    # is that list, which is fine for an exit code and wrong the moment it is
    # reported as a value.
    failed = not report.is_ok or bool(args.fail_on_warning and report.warnings)

    if args.as_json:
        json.dump(
            {
                # is_ok counts errors only, as validate_report defines it.
                # failed is what the exit code actually reflects, which is a
                # different question under --fail-on-warning: without it a
                # warnings-only package emits is_ok true and exits 1, and a
                # gate reading the document concludes the opposite of what
                # happened.
                "is_ok": report.is_ok,
                "failed": failed,
                "issues": [
                    dataclasses.asdict(issue) for issue in report.issues
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        report.print()

    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usap",
        description="Command-line tools for USAP packages.",
    )

    subparsers = parser.add_subparsers(dest="command")
    _add_validate_parser(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "handler", None) is None:
        parser.print_help()
        return 2

    try:
        return args.handler(args)
    except USAPError as error:
        # A missing file, or one that is not a USAP package at all, is an
        # ordinary thing to type. The audience here has no Python of its own,
        # so a traceback tells them nothing they can act on -- and the exit
        # code is the same either way.
        #
        # Under --json that message has to be a document too. These are the
        # command's most ordinary failures -- wrong path, typo, a package from
        # an older profile -- and a gate that pipes stdout to a parser would
        # otherwise meet a JSONDecodeError instead of a reason. Same keys as a
        # report, plus `error`, so one parser handles both and the presence of
        # that key means "the run could not start".
        if getattr(args, "as_json", False):
            json.dump(
                {
                    "is_ok": False,
                    "failed": True,
                    "error": str(error),
                    "issues": [],
                },
                sys.stdout,
                indent=2,
            )
            sys.stdout.write("\n")
        else:
            print(f"usap: {error}", file=sys.stderr)

        return 1


if __name__ == "__main__":
    raise SystemExit(main())

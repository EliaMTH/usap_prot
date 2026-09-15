"""
The `usap` console script.

The point of the command is that a package can be checked without writing
Python — so what these tests pin is the machine-facing contract a pipeline
gates on: the exit code, and the shape of --json. The validation itself is
`validate_report`'s, tested in test_validation.py; there is no second
implementation here to check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_pkg
from usap.cli import main


def _clean_package(tmp_path: Path) -> Path:
    path = tmp_path / "clean.usap.gpkg"
    with make_pkg(tmp_path, "clean.usap.gpkg"):
        pass
    return path


def _package_with_a_warning(tmp_path: Path) -> Path:
    # An annotated part with no indexing_profile: a warning, never an error.
    path = tmp_path / "warn.usap.gpkg"
    with make_pkg(tmp_path, "warn.usap.gpkg") as pkg:
        concept = pkg.create_semantic_class(
            scheme="local", class_uri="local:R", local_name="R"
        )
        asset_id = pkg.register_asset(uri="mesh.ply", asset_kind="mesh")
        part_id = pkg.register_asset_part(asset_id, "g/0", "face", 10)
        annotation_id = pkg.create_annotation(
            annotation_uid="a1", semantic_class_id=concept
        )
        pkg.replace_annotation_membership(annotation_id, part_id, "face", [1, 2])
    return path


def test_validate_exits_zero_on_a_clean_package(tmp_path: Path) -> None:
    assert main(["validate", str(_clean_package(tmp_path))]) == 0


def test_validate_json_is_machine_readable(tmp_path: Path, capsys) -> None:
    # A gate that has to parse the human format is a gate that breaks when the
    # wording improves.
    assert main(["validate", str(_clean_package(tmp_path)), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)

    assert payload["is_ok"] is True
    assert payload["issues"] == []


def test_validate_json_carries_every_issue_field(tmp_path: Path, capsys) -> None:
    main(["validate", str(_package_with_a_warning(tmp_path)), "--json"])

    payload = json.loads(capsys.readouterr().out)

    assert payload["is_ok"] is True, "a warning must not make is_ok false"
    assert len(payload["issues"]) == 1
    assert set(payload["issues"][0]) == {
        "severity", "code", "message", "table", "row_id", "details"
    }
    assert payload["issues"][0]["code"] == "ASSET_PART_NO_INDEXING_PROFILE"


def test_warnings_do_not_fail_by_default(tmp_path: Path) -> None:
    # is_ok counts errors only, and the default follows it.
    assert main(["validate", str(_package_with_a_warning(tmp_path))]) == 0


def test_fail_on_warning_makes_a_pipeline_say_which_it_means(tmp_path: Path) -> None:
    assert main(
        ["validate", str(_package_with_a_warning(tmp_path)), "--fail-on-warning"]
    ) == 1

    # ... and does not invent failures on a package with no warnings at all.
    assert main(
        ["validate", str(_clean_package(tmp_path)), "--fail-on-warning"]
    ) == 0


def test_an_unknown_level_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["validate", str(_clean_package(tmp_path)), "--level", "profound"])


def test_no_subcommand_prints_help(tmp_path: Path, capsys) -> None:
    assert main([]) == 2
    assert "validate" in capsys.readouterr().out


def test_a_missing_package_is_a_message_not_a_traceback(
    tmp_path: Path, capsys
) -> None:
    # Typing a path that is not there is an ordinary thing to do, and this
    # command exists for people who do not write Python: a traceback tells
    # them nothing they can act on. The exit code is 1 either way, so what is
    # pinned here is the output.
    assert main(["validate", str(tmp_path / "nope.usap.gpkg")]) == 1

    captured = capsys.readouterr()
    assert "Database does not exist" in captured.err
    assert "Traceback" not in captured.err


def test_a_failure_under_json_is_still_json(tmp_path: Path, capsys) -> None:
    # --json exists so a gate never parses prose, and that has to hold on the
    # failure path too: a wrong path and an older profile are this command's
    # most ordinary failures, so emitting the message as text there means the
    # gate's first real failure is a JSONDecodeError instead of a reason.
    assert main(["validate", str(tmp_path / "nope.usap.gpkg"), "--json"]) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["is_ok"] is False
    assert payload["failed"] is True
    assert "Database does not exist" in payload["error"]
    assert payload["issues"] == []

    # The prose form must not also fire, or a gate reading stderr sees a
    # failure it has already been told about in the document.
    assert captured.err == ""


def test_json_failed_matches_the_exit_code(tmp_path: Path, capsys) -> None:
    # is_ok counts errors only, so under --fail-on-warning a warnings-only
    # package is is_ok true *and* exit 1. Without `failed` the document says
    # the opposite of what the command did.
    package = str(_package_with_a_warning(tmp_path))

    assert main(["validate", package, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["is_ok"] is True
    assert payload["failed"] is False

    assert main(["validate", package, "--json", "--fail-on-warning"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["is_ok"] is True
    assert payload["failed"] is True

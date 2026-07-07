"""
Regression tests for the bugs found in the second (2026-07) repo audit.

Each test pins the fixed behavior of one confirmed finding:
  B1  narrow-dtype casts must not silently wrap/truncate/overflow values
  B2  a mixed-dtype value field must fail validation, not only the readers
  B3  every value reader must reject partial fields (v1 full coverage)
  R1  no explicit index may duplicate a UNIQUE constraint's auto-index
  M1  an ambiguous vocabulary parent must surface as ambiguity, not
      "not registered"
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

import numpy as np
import pytest

from usap import (
    ELEMENT_KIND_FACE,
    USAPAmbiguityError,
    USAPError,
    USAPPackage,
    seed_vocabulary_file,
)

SCHEMA_PATH = Path("sql/schema.sql").resolve()


def _make_pkg(tmp_path: Path, name: str = "pkg.usap.gpkg") -> USAPPackage:
    return USAPPackage.create(
        tmp_path / name,
        schema_path=SCHEMA_PATH,
        overwrite=True,
    )


def _field_setup(pkg: USAPPackage, element_count: int = 10) -> int:
    asset_id = pkg.register_asset(uri="mesh.glb", asset_kind="mesh")
    pkg.create_semantic_class(scheme="s", class_uri="s:Frac", local_name="Frac")

    return pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=element_count,
    )


def _replace_field_blocks(
    pkg: USAPPackage,
    annotation_id: int,
    asset_part_id: int,
    blocks: list[tuple[int, np.ndarray, str]],
) -> None:
    """
    Overwrite an annotation's value blocks via raw SQL with
    (block_start, values, dtype_tag) triples, keeping min/max consistent.
    """
    pkg.conn.execute(
        "DELETE FROM usap_value_block WHERE annotation_id = ?",
        (annotation_id,),
    )

    for block_start, values, dtype_tag in blocks:
        typed = np.ascontiguousarray(values, dtype=np.dtype("<" + dtype_tag))

        pkg.conn.execute(
            """
            INSERT INTO usap_value_block (
                annotation_id, asset_part_id, element_kind,
                block_start, element_count, value_dtype,
                value_min, value_max, payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                annotation_id,
                asset_part_id,
                ELEMENT_KIND_FACE,
                block_start,
                len(typed),
                dtype_tag,
                float(typed.min()),
                float(typed.max()),
                zlib.compress(typed.tobytes()),
            ),
        )

    pkg.conn.commit()


# ---------------------------------------------------------------------------
# B1 — strict dtype casting
# ---------------------------------------------------------------------------

def test_integer_dtype_rejects_out_of_range_and_truncation(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _field_setup(pkg)
        base = [0.0] * 10

        for bad, match in [
            (300.0, "range"),   # would wrap to 44
            (-1.0, "range"),    # would wrap to 255
            (0.7, "truncate"),  # would truncate to 0
        ]:
            values = list(base)
            values[3] = bad

            with pytest.raises(USAPError, match=match):
                pkg.annotate_value_field(
                    concept="Frac",
                    asset_part_id=part,
                    element_kind="face",
                    values=values,
                    value_dtype="u1",
                )

        # Same rule for pure-int inputs (int64 -> u1 wraparound).
        with pytest.raises(USAPError, match="range"):
            pkg.annotate_value_field(
                concept="Frac",
                asset_part_id=part,
                element_kind="face",
                values=[300] + [0] * 9,
                value_dtype="u1",
            )


def test_exact_integer_values_still_round_trip(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _field_setup(pkg)
        values = [0.0, 1.0, 255.0, 7.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        annotation = pkg.annotate_value_field(
            concept="Frac",
            asset_part_id=part,
            element_kind="face",
            values=values,
            value_dtype="u1",
        )

        back = pkg.values_for_annotation(int(annotation["annotation_id"]))

        assert back.dtype == np.dtype("<u1")
        assert back.tolist() == [int(v) for v in values]


def test_float_dtype_allows_rounding_but_not_overflow(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _field_setup(pkg)

        # f8 -> f4 precision rounding is inherent to the requested dtype.
        annotation = pkg.annotate_value_field(
            concept="Frac",
            asset_part_id=part,
            element_kind="face",
            values=[0.1] * 10,
            value_dtype="f4",
        )

        back = pkg.values_for_annotation(int(annotation["annotation_id"]))

        assert back.dtype == np.dtype("<f4")
        assert np.allclose(back, 0.1)

        # But a finite value that would become inf must raise.
        with pytest.raises(USAPError, match="inf"):
            pkg.annotate_value_field(
                concept="Frac",
                asset_part_id=part,
                element_kind="face",
                values=[1e300] + [0.0] * 9,
                value_dtype="f4",
            )


# ---------------------------------------------------------------------------
# B2 — mixed-dtype field fails validation
# ---------------------------------------------------------------------------

def test_mixed_dtype_field_fails_validation(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _field_setup(pkg)

        annotation = pkg.annotate_value_field(
            concept="Frac",
            asset_part_id=part,
            element_kind="face",
            values=np.linspace(0.0, 1.0, 10, dtype=np.float32),
        )
        annotation_id = int(annotation["annotation_id"])

        _replace_field_blocks(
            pkg,
            annotation_id,
            part,
            [
                (0, np.zeros(5), "f4"),
                (5, np.ones(5), "f8"),
            ],
        )

        # Readers already refused this; validation must agree.
        with pytest.raises(USAPError, match="mixes dtypes"):
            pkg.values_for_annotation(annotation_id)

        report = pkg.validate_report()

        assert not report.is_ok
        assert "MIXED_VALUE_DTYPE_FIELD" in {i.code for i in report.errors}


# ---------------------------------------------------------------------------
# B3 — all readers reject partial fields
# ---------------------------------------------------------------------------

def test_all_value_readers_reject_partial_fields(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _field_setup(pkg)

        annotation = pkg.annotate_value_field(
            concept="Frac",
            asset_part_id=part,
            element_kind="face",
            values=np.linspace(0.0, 1.0, 10, dtype=np.float32),
        )
        annotation_id = int(annotation["annotation_id"])

        # Keep only the first half of the field: coverage gap at element 5.
        _replace_field_blocks(
            pkg,
            annotation_id,
            part,
            [(0, np.linspace(0.0, 0.4, 5), "f4")],
        )

        with pytest.raises(USAPError, match="partial fields"):
            pkg.values_for_annotation(annotation_id)

        with pytest.raises(USAPError, match="partial fields"):
            pkg.elements_where(annotation_id, (">", -1.0))

        with pytest.raises(USAPError, match="partial fields"):
            pkg.value_field_stats(annotation_id)


# ---------------------------------------------------------------------------
# R1 — no duplicate indexes
# ---------------------------------------------------------------------------

def test_no_explicit_index_duplicates_a_unique_autoindex(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        tables = [
            row["name"]
            for row in pkg.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name LIKE 'usap_%'"
            ).fetchall()
        ]

        for table in tables:
            indexes = pkg.conn.execute(
                f"PRAGMA index_list({table})"
            ).fetchall()

            columns_by_origin: dict[str, list[tuple[str, ...]]] = {
                "c": [],
                "u": [],
            }

            for index in indexes:
                if index["origin"] not in columns_by_origin:
                    continue

                columns = tuple(
                    info["name"]
                    for info in pkg.conn.execute(
                        f"PRAGMA index_info({index['name']})"
                    ).fetchall()
                )
                columns_by_origin[index["origin"]].append(columns)

            for explicit in columns_by_origin["c"]:
                assert explicit not in columns_by_origin["u"], (
                    f"{table}: explicit index on {explicit} duplicates the "
                    "UNIQUE constraint's auto-index"
                )

        # The annotation-first fetches must still be served by an index
        # (the auto-index) after dropping the explicit duplicates.
        for table in ("usap_membership_block", "usap_value_block"):
            plan = " ".join(
                row[3]
                for row in pkg.conn.execute(
                    f"EXPLAIN QUERY PLAN SELECT payload FROM {table} "
                    "WHERE annotation_id = 1 "
                    "ORDER BY asset_part_id, element_kind, block_start"
                ).fetchall()
            )

            assert "USING INDEX" in plan, plan


# ---------------------------------------------------------------------------
# M1 — ambiguous vocabulary parent
# ---------------------------------------------------------------------------

def test_ambiguous_vocabulary_parent_reports_ambiguity(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        pkg.create_semantic_class(scheme="s1", class_uri="s1:Dup", local_name="Dup")
        pkg.create_semantic_class(scheme="s2", class_uri="s2:Dup", local_name="Dup")

        vocab_path = tmp_path / "vocab.json"
        vocab_path.write_text(
            json.dumps(
                {
                    "scheme": "s3",
                    "concepts": [{"local_name": "Child", "parent_uri": "Dup"}],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(USAPAmbiguityError, match="ambiguous"):
            seed_vocabulary_file(pkg, vocab_path)

"""
Tests for per-element value fields (usap_value_block).

Covers the v1 contract from VALUE_FIELDS_DESIGN.md: dense full-coverage
scalar fields bound to an asset part (never a city object), typed by a
registered concept, queryable by value, edited by whole-field rewrite.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import numpy as np
import pytest

from conftest import (
    assert_package_valid,
    make_mesh_part as _mesh_part,
    make_pkg as _make_pkg,
)

from usap import (
    ELEMENT_KIND_FACE,
    USAPError,
    apply_annotation_batch,
    seed_default_ade_vocabulary,
    seed_vocabulary_file,
)
from usap.constants import VALUE_CHUNK_SIZE


def test_round_trip_with_nan_holes(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=50)
        seed_default_ade_vocabulary(pkg)

        field = np.linspace(0.0, 1.0, 50, dtype=np.float32)
        field[[3, 17, 42]] = np.nan

        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part,
            element_kind="face",
            values=field,
            attributes={"validAt": "2026-06-21T14:00:00Z", "unit": "fraction"},
        )

        result = pkg.values_for_annotation(int(annotation["annotation_id"]))

        assert result.dtype == np.dtype("<f4")
        np.testing.assert_array_equal(np.isnan(result), np.isnan(field))
        np.testing.assert_array_equal(
            result[~np.isnan(field)], field[~np.isnan(field)]
        )

        summary = annotation["value_field_summary"]

        assert len(summary) == 1
        assert summary[0]["asset_part_id"] == part
        assert summary[0]["value_count"] == 50

        assert_package_valid(pkg)


def test_elements_where_query_intent(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=20)
        seed_default_ade_vocabulary(pkg)

        field = np.zeros(20, dtype=np.float32)
        field[[2, 5, 11]] = 0.9
        field[7] = np.nan  # "no value" must never match, not even "!="

        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part,
            element_kind="face",
            values=field,
        )
        annotation_id = int(annotation["annotation_id"])

        # The asset-level case is the point: no city object anywhere.
        assert annotation["primary_city_object_id"] is None

        assert pkg.elements_where(annotation_id, (">", 0.5)) == [2, 5, 11]
        assert pkg.elements_where(annotation_id, ("==", 0.0)) == [
            i for i in range(20) if i not in (2, 5, 7, 11)
        ]
        assert 7 not in pkg.elements_where(annotation_id, ("!=", 123.0))

        callable_hits = pkg.elements_where(
            annotation_id, lambda v: (v > 0.5) & (v < 1.0)
        )

        assert callable_hits == [2, 5, 11]


def test_concept_must_be_registered_and_field_is_asset_bound(
    tmp_path: Path,
) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=4)

        with pytest.raises(USAPError, match="concept not found"):
            pkg.annotate_value_field(
                concept="ShadowFraction",  # nothing seeded yet
                asset_part_id=part,
                element_kind="face",
                values=[0.1, 0.2, 0.3, 0.4],
            )

        seed_default_ade_vocabulary(pkg)

        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part,
            element_kind="face",
            values=[0.1, 0.2, 0.3, 0.4],
        )

        assert annotation["primary_city_object_id"] is None

        rows = pkg.conn.execute(
            """
            SELECT asset_part_id
            FROM usap_value_block
            WHERE annotation_id = ?
            """,
            (int(annotation["annotation_id"]),),
        ).fetchall()

        assert {int(row["asset_part_id"]) for row in rows} == {part}


def test_minimal_local_vocabulary_concept_works(tmp_path: Path) -> None:
    vocab_path = tmp_path / "local.json"
    vocab_path.write_text(
        json.dumps(
            {
                "scheme": "local",
                "concepts": [{"local_name": "TempShadow"}],
            }
        ),
        encoding="utf-8",
    )

    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=6)
        seed_vocabulary_file(pkg, vocab_path)

        annotation = pkg.annotate_value_field(
            concept="TempShadow",
            asset_part_id=part,
            element_kind="face",
            values=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        )

        assert annotation["semantic_class_uri"] == "local:TempShadow"
        assert pkg.elements_where(
            int(annotation["annotation_id"]), (">=", 0.6)
        ) == [3, 4, 5]


def test_chunking_and_block_pruning(tmp_path: Path) -> None:
    element_count = VALUE_CHUNK_SIZE * 2 + 1000  # 3 blocks

    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=element_count)
        seed_default_ade_vocabulary(pkg)

        field = np.zeros(element_count, dtype=np.float32)
        # hits only inside the last (short) block
        hit_indices = [VALUE_CHUNK_SIZE * 2 + 10, VALUE_CHUNK_SIZE * 2 + 999]
        field[hit_indices] = 0.9

        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part,
            element_kind="face",
            values=field,
        )
        annotation_id = int(annotation["annotation_id"])

        block_count = pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_value_block WHERE annotation_id = ?",
            (annotation_id,),
        ).fetchone()

        assert int(block_count["n"]) == 3

        # multi-block field reassembles in element order
        result = pkg.values_for_annotation(annotation_id)
        np.testing.assert_array_equal(result, field)

        # min/max pruning skips the first two blocks yet the global indices
        # returned must still be absolute
        assert pkg.elements_where(annotation_id, (">", 0.5)) == hit_indices

        assert_package_valid(pkg)


def test_replace_overwrites_old_field(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=8)
        seed_default_ade_vocabulary(pkg)

        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part,
            element_kind="face",
            values=np.full(8, 0.9, dtype=np.float32),
        )
        annotation_id = int(annotation["annotation_id"])

        corrected = np.full(8, 0.1, dtype=np.float32)
        pkg.replace_value_field(annotation_id, part, "face", corrected)

        np.testing.assert_array_equal(
            pkg.values_for_annotation(annotation_id), corrected
        )
        assert pkg.elements_where(annotation_id, (">", 0.5)) == []


def test_dtype_fidelity(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=5)
        seed_default_ade_vocabulary(pkg)

        cases = {
            "u1": np.array([0, 1, 127, 254, 255], dtype=np.uint8),
            "f2": np.array([0.0, 0.5, 0.25, 1.0, -2.0], dtype=np.float16),
            "f4": np.array([0.0, 0.1, 0.2, 0.3, 0.4], dtype=np.float32),
        }

        for tag, values in cases.items():
            annotation = pkg.annotate_value_field(
                concept="ShadowFraction",
                annotation_uid=f"ann_dtype_{tag}",
                asset_part_id=part,
                element_kind="face",
                values=values,
                value_dtype=tag,
            )

            result = pkg.values_for_annotation(int(annotation["annotation_id"]))

            assert result.dtype == np.dtype("<" + tag)
            np.testing.assert_array_equal(result, values)

            stats = pkg.value_field_stats(int(annotation["annotation_id"]))
            assert stats["value_dtype"] == tag


def test_narrowing_cast_allows_rounding_but_not_overflow(tmp_path: Path) -> None:
    # Strict casting must not block legitimate narrowing: f8 input -> f4
    # loses only precision, which is inherent to the requested dtype.
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=10)
        seed_default_ade_vocabulary(pkg)

        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part,
            element_kind="face",
            values=[0.1] * 10,
            value_dtype="f4",
        )

        back = pkg.values_for_annotation(int(annotation["annotation_id"]))

        assert back.dtype == np.dtype("<f4")
        assert np.allclose(back, 0.1)

        # But a finite value that would become inf must raise, not be stored.
        with pytest.raises(USAPError, match="inf"):
            pkg.annotate_value_field(
                concept="ShadowFraction",
                annotation_uid="ann_overflow",
                asset_part_id=part,
                element_kind="face",
                values=[1e300] + [0.0] * 9,
                value_dtype="f4",
            )


def test_error_paths(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=10)
        seed_default_ade_vocabulary(pkg)

        with pytest.raises(USAPError, match="cover the whole asset part"):
            pkg.annotate_value_field(
                concept="ShadowFraction",
                asset_part_id=part,
                element_kind="face",
                values=[0.1, 0.2, 0.3],  # 3 of 10
            )

        with pytest.raises(USAPError, match="NaN is not representable"):
            pkg.annotate_value_field(
                concept="ShadowFraction",
                asset_part_id=part,
                element_kind="face",
                values=[float("nan")] + [1.0] * 9,
                value_dtype="u1",
            )

        with pytest.raises(ValueError, match="Unsupported value_dtype"):
            pkg.annotate_value_field(
                concept="ShadowFraction",
                asset_part_id=part,
                element_kind="face",
                values=[1.0] * 10,
                value_dtype="f16",
            )

        with pytest.raises(USAPError, match="Annotation not found"):
            pkg.replace_value_field(99999, part, "face", [1.0] * 10)

        # The setup call stays outside the raises block: only the read may
        # raise, or the test could pass for the wrong reason.
        annotation = pkg.create_concept_annotation(concept="ShadowFraction")

        with pytest.raises(USAPError, match="No value field"):
            pkg.values_for_annotation(int(annotation["annotation_id"]))


def test_validation_flags_corrupt_value_blocks(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=10)
        seed_default_ade_vocabulary(pkg)

        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part,
            element_kind="face",
            values=np.arange(10, dtype=np.float32) / 10.0,
        )
        annotation_id = int(annotation["annotation_id"])

        assert_package_valid(pkg)

        # inject corruption directly
        with pkg.transaction():
            pkg.conn.execute(
                "UPDATE usap_value_block SET payload = ? WHERE annotation_id = ?",
                (zlib.compress(b"garbage!"), annotation_id),
            )

        codes = {issue.code for issue in pkg.validate_report().issues}
        assert "CORRUPT_VALUE_PAYLOAD" in codes

        with pkg.transaction():
            pkg.conn.execute(
                "UPDATE usap_value_block SET value_dtype = 'zz' WHERE annotation_id = ?",
                (annotation_id,),
            )

        codes = {issue.code for issue in pkg.validate_report().issues}
        assert "UNSUPPORTED_VALUE_DTYPE" in codes

        # restore a healthy field, then push it out of the part's range
        pkg.replace_value_field(
            annotation_id, part, "face", np.zeros(10, dtype=np.float32)
        )

        with pkg.transaction():
            pkg.conn.execute(
                "UPDATE usap_value_block SET block_start = 5 WHERE annotation_id = ?",
                (annotation_id,),
            )

        codes = {issue.code for issue in pkg.validate_report().issues}
        assert "VALUE_BLOCK_OUT_OF_RANGE" in codes

        # cascade: deleting the annotation removes its value blocks
        pkg.delete_annotation(annotation_id)

        remaining = pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_value_block"
        ).fetchone()

        assert int(remaining["n"]) == 0
        assert_package_valid(pkg)


def test_batch_value_fields(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=4)
        seed_default_ade_vocabulary(pkg)

        batch = {
            "annotations": [
                {
                    "annotation_uid": "ann_shadow_batch",
                    "concept": "ShadowFraction",
                    "attributes": {"validAt": "2026-06-21T14:00:00Z"},
                    "value_fields": [
                        {
                            "asset_part_id": part,
                            "element_kind": "face",
                            "values": [0.0, 0.7, None, 0.5],
                        }
                    ],
                }
            ]
        }

        result = apply_annotation_batch(pkg, batch)

        assert result.annotation_count == 1
        assert result.membership_count == 0
        assert result.value_field_count == 1

        annotation = pkg.get_annotation(annotation_uid="ann_shadow_batch")
        assert annotation is not None
        annotation_id = int(annotation["annotation_id"])

        values = pkg.values_for_annotation(annotation_id)

        assert bool(np.isnan(values[2]))  # JSON null -> NaN
        assert pkg.elements_where(annotation_id, (">", 0.4)) == [1, 3]

        # neither memberships nor value_fields -> rejected
        with pytest.raises(ValueError, match="at least one of"):
            apply_annotation_batch(
                pkg,
                {
                    "annotations": [
                        {
                            "annotation_uid": "ann_empty",
                            "concept": "ShadowFraction",
                        }
                    ]
                },
            )


def test_value_field_stats(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        part = _mesh_part(pkg, element_count=6)
        seed_default_ade_vocabulary(pkg)

        field = np.array([0.2, np.nan, 0.9, 0.1, np.nan, 0.5], dtype=np.float32)

        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part,
            element_kind="face",
            values=field,
        )

        stats = pkg.value_field_stats(int(annotation["annotation_id"]))

        assert stats["min"] == pytest.approx(float(np.nanmin(field)))
        assert stats["max"] == pytest.approx(float(np.nanmax(field)))
        assert stats["count"] == 6  # total stored values, NaN included
        assert stats["block_count"] == 1
        assert stats["asset_part_id"] == part


def _field_setup(pkg, element_count: int = 10) -> int:
    asset_id = pkg.register_asset(uri="field_mesh.glb", asset_kind="mesh")
    pkg.create_semantic_class(scheme="s", class_uri="s:Frac", local_name="Frac")

    return pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=element_count,
    )


def _replace_field_blocks(
    pkg,
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


def test_integer_dtype_rejects_out_of_range_and_truncation(tmp_path: Path) -> None:
    # Narrow integer dtypes must never wrap or truncate values silently.
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


def test_mixed_dtype_field_fails_validation(tmp_path: Path) -> None:
    # A field mixing dtypes across blocks must fail validate_report(), not
    # only the readers.
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


def test_all_value_readers_reject_partial_fields(tmp_path: Path) -> None:
    # All three readers must enforce the v1 full-coverage contract.
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

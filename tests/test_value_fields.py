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

from conftest import assert_package_valid

from usap import (
    ELEMENT_KIND_FACE,
    USAPError,
    USAPPackage,
    apply_annotation_batch,
    seed_default_ade_vocabulary,
    seed_vocabulary_file,
)
from usap.constants import VALUE_CHUNK_SIZE

SCHEMA_PATH = Path("sql/schema.sql").resolve()


def _make_pkg(tmp_path: Path) -> USAPPackage:
    return USAPPackage.create(
        tmp_path / "values.usap.gpkg",
        schema_path=SCHEMA_PATH,
        overwrite=True,
    )


def _mesh_part(pkg: USAPPackage, element_count: int = 100) -> int:
    asset_id = pkg.register_asset(uri="mesh.glb", asset_kind="mesh")

    return pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=element_count,
    )


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

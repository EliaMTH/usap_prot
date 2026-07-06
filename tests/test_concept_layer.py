"""
Tests for the concept-layer features:

  - minimal local vocabularies (derived class_uri, parent by name,
    re-ingestion for updates)
  - hierarchy integrity (parent conflicts raise)
  - concept registry scouting (in_use flag / annotation_count)
  - hierarchy-accelerated query plans (closure + annotation-by-class index)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from usap import ELEMENT_KIND_FACE, USAPError, USAPPackage, seed_vocabulary_file

SCHEMA_PATH = Path("sql/schema.sql").resolve()

MINIMAL_VOCAB = {
    "scheme": "local",
    "concepts": [
        {"local_name": "TempSurface"},
        {"local_name": "TempRoof", "parent_uri": "TempSurface"},
        {"local_name": "TempChimney", "parent_uri": "TempRoof"},
    ],
}


def _make_pkg(tmp_path: Path) -> USAPPackage:
    return USAPPackage.create(
        tmp_path / "concepts.usap.gpkg",
        schema_path=SCHEMA_PATH,
        overwrite=True,
    )


def _write_vocab(tmp_path: Path, data: dict, name: str = "local.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _mesh_part(pkg: USAPPackage, element_count: int = 100) -> int:
    asset_id = pkg.register_asset(uri="mesh.glb", asset_kind="mesh")

    return pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=element_count,
    )


def test_minimal_vocabulary_seeds_and_annotates(tmp_path: Path) -> None:
    vocab_path = _write_vocab(tmp_path, MINIMAL_VOCAB)

    with _make_pkg(tmp_path) as pkg:
        result = seed_vocabulary_file(pkg, vocab_path)

        # class_uri derived from scheme + name
        assert result.by_uri["local:TempRoof"] == result.by_name["TempRoof"]

        # parent-by-name produced real closure rows (depth 1 and 2)
        depth = pkg.conn.execute(
            """
            SELECT c.depth
            FROM usap_semantic_class_closure AS c
            JOIN usap_semantic_class AS anc
                ON anc.semantic_class_id = c.ancestor_class_id
            JOIN usap_semantic_class AS des
                ON des.semantic_class_id = c.descendant_class_id
            WHERE anc.local_name = 'TempSurface'
              AND des.local_name = 'TempChimney'
            """
        ).fetchone()

        assert depth is not None
        assert int(depth["depth"]) == 2

        # local concepts are usable for annotation + query right away
        part = _mesh_part(pkg)

        pkg.annotate_elements(
            concept="TempRoof",
            annotation_uid="ann_temp_roof",
            asset_part_id=part,
            element_kind="face",
            element_indices=[3, 4],
        )

        matches = pkg.annotations_for_elements(
            asset_part_id=part,
            element_kind="face",
            selected_indices=[4],
        )

        assert [m["semantic_class"] for m in matches] == ["TempRoof"]

        # subclass query: TempRoof blocks found under the TempSurface ancestor
        blocks = pkg.elements_for_semantic_class(
            result.by_name["TempSurface"],
            include_subclasses=True,
            expand=True,
        )

        assert [block["elements"] for block in blocks] == [[3, 4]]

        assert pkg.validate_report().is_ok


def test_minimal_vocabulary_reingest_is_additive(tmp_path: Path) -> None:
    vocab_path = _write_vocab(tmp_path, MINIMAL_VOCAB)

    with _make_pkg(tmp_path) as pkg:
        seed_vocabulary_file(pkg, vocab_path)
        seed_vocabulary_file(pkg, vocab_path)  # unchanged re-ingest: no-op

        count = pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_semantic_class"
        ).fetchone()

        assert int(count["n"]) == 3

        # updated copy: one concept added, existing ones untouched
        updated = json.loads(json.dumps(MINIMAL_VOCAB))
        updated["concepts"].append(
            {"local_name": "TempGutter", "parent_uri": "TempRoof"}
        )
        updated_path = _write_vocab(tmp_path, updated, name="local_v2.json")

        result = seed_vocabulary_file(pkg, updated_path)

        assert "TempGutter" in result.by_name

        count = pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_semantic_class"
        ).fetchone()

        assert int(count["n"]) == 4
        assert pkg.validate_report().is_ok


def test_changing_parent_on_reingest_raises(tmp_path: Path) -> None:
    vocab_path = _write_vocab(tmp_path, MINIMAL_VOCAB)

    conflicting = json.loads(json.dumps(MINIMAL_VOCAB))
    # TempChimney rewired from TempRoof to TempSurface
    conflicting["concepts"][2]["parent_uri"] = "TempSurface"
    conflicting_path = _write_vocab(tmp_path, conflicting, name="conflict.json")

    with _make_pkg(tmp_path) as pkg:
        seed_vocabulary_file(pkg, vocab_path)

        with pytest.raises(USAPError, match="different parent"):
            seed_vocabulary_file(pkg, conflicting_path)


def test_list_accepted_concepts_in_use_flag(tmp_path: Path) -> None:
    vocab_path = _write_vocab(tmp_path, MINIMAL_VOCAB)

    with _make_pkg(tmp_path) as pkg:
        seed_vocabulary_file(pkg, vocab_path)

        concepts = pkg.list_accepted_concepts(scheme="local")

        assert {c["local_name"] for c in concepts} == {
            "TempSurface",
            "TempRoof",
            "TempChimney",
        }
        assert all(c["in_use"] is False for c in concepts)
        assert all(c["annotation_count"] == 0 for c in concepts)

        part = _mesh_part(pkg)

        for uid in ("ann_r1", "ann_r2"):
            pkg.annotate_elements(
                concept="TempRoof",
                annotation_uid=uid,
                asset_part_id=part,
                element_kind="face",
                element_indices=[1],
            )

        by_name = {
            c["local_name"]: c for c in pkg.list_accepted_concepts(scheme="local")
        }

        assert by_name["TempRoof"]["in_use"] is True
        assert by_name["TempRoof"]["annotation_count"] == 2
        assert by_name["TempSurface"]["in_use"] is False

        used = pkg.list_accepted_concepts(in_use=True)
        unused = pkg.list_accepted_concepts(in_use=False)

        assert [c["local_name"] for c in used] == ["TempRoof"]
        assert {c["local_name"] for c in unused} == {"TempSurface", "TempChimney"}


def test_subclass_block_query_uses_indexes(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        # The subclass -> membership-blocks query must start from the closure
        # and reach annotations via usap_annotation_by_class, never by
        # scanning all membership blocks.
        plan_rows = pkg.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT mb.membership_block_id
            FROM usap_membership_block AS mb
            JOIN usap_annotation AS a
                ON a.annotation_id = mb.annotation_id
            JOIN usap_semantic_class_closure AS c
                ON c.descendant_class_id = a.semantic_class_id
            WHERE c.ancestor_class_id = ?
            """,
            (1,),
        ).fetchall()

        plan = "\n".join(row[3] for row in plan_rows)

        assert "SCAN mb" not in plan, plan

        # Closure maintenance (ancestors of one class) must be indexed too.
        plan_rows = pkg.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT ancestor_class_id, depth
            FROM usap_semantic_class_closure
            WHERE descendant_class_id = ?
            """,
            (1,),
        ).fetchall()

        plan = "\n".join(row[3] for row in plan_rows)

        assert "usap_scc_by_descendant" in plan, plan

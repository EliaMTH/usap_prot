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

from conftest import (
    assert_package_valid,
    make_mesh_part as _mesh_part,
    make_pkg as _make_pkg,
)

from usap import USAPAmbiguityError, USAPError, seed_vocabulary_file

MINIMAL_VOCAB = {
    "scheme": "local",
    "concepts": [
        {"local_name": "TempSurface"},
        {"local_name": "TempRoof", "parent_uri": "TempSurface"},
        {"local_name": "TempChimney", "parent_uri": "TempRoof"},
    ],
}


def _write_vocab(tmp_path: Path, data: dict, name: str = "local.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


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

        assert_package_valid(pkg)


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
        assert_package_valid(pkg)


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


def test_reingest_backfills_missing_provenance(tmp_path: Path) -> None:
    # A registry that gains provenance later must be able to reach packages
    # that already exist. Without backfill, seeding returns success and
    # silently keeps the old NULLs, so the only way to enrich a package would
    # be to rebuild it — which is exactly what a stable identifier scheme
    # added after the fact would require.
    vocab_path = _write_vocab(tmp_path, MINIMAL_VOCAB)

    enriched = json.loads(json.dumps(MINIMAL_VOCAB))
    enriched["source_namespace"] = "https://example.org/ns/local#"
    enriched["concepts"][0]["concept_iri"] = "https://example.org/c/TempSurface"
    enriched_path = _write_vocab(tmp_path, enriched, name="enriched.json")

    with _make_pkg(tmp_path) as pkg:
        seed_vocabulary_file(pkg, vocab_path)

        before = pkg.conn.execute(
            """
            SELECT source_namespace, concept_iri
            FROM usap_semantic_class
            WHERE local_name = 'TempSurface'
            """
        ).fetchone()

        assert before["source_namespace"] is None
        assert before["concept_iri"] is None

        seed_vocabulary_file(pkg, enriched_path)

        after = pkg.conn.execute(
            """
            SELECT source_namespace, concept_iri
            FROM usap_semantic_class
            WHERE local_name = 'TempSurface'
            """
        ).fetchone()

        assert after["source_namespace"] == "https://example.org/ns/local#"
        assert after["concept_iri"] == "https://example.org/c/TempSurface"

        # Enriching is not duplicating.
        count = pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_semantic_class"
        ).fetchone()

        assert int(count["n"]) == 3
        assert_package_valid(pkg)


def test_reingest_does_not_overwrite_existing_provenance(
    tmp_path: Path,
) -> None:
    # Backfill fills gaps; it must not rewrite an assertion the package
    # already carries, or re-seeding would be a silent way to change what a
    # concept claims to be.
    first = json.loads(json.dumps(MINIMAL_VOCAB))
    first["source_namespace"] = "https://example.org/ns/one#"

    second = json.loads(json.dumps(MINIMAL_VOCAB))
    second["source_namespace"] = "https://example.org/ns/two#"

    with _make_pkg(tmp_path) as pkg:
        seed_vocabulary_file(pkg, _write_vocab(tmp_path, first, name="a.json"))

        with pytest.raises(USAPError, match="different source_namespace"):
            seed_vocabulary_file(
                pkg,
                _write_vocab(tmp_path, second, name="b.json"),
            )


def test_backfilled_parent_reaches_the_closure(tmp_path: Path) -> None:
    # The closure is written eagerly at insert, so a parent arriving through a
    # backfill has to be propagated too — otherwise the hierarchy looks right
    # in usap_semantic_class while subclass queries keep missing the edge.
    flat = {
        "scheme": "local",
        "concepts": [{"local_name": "Base"}, {"local_name": "Derived"}],
    }

    linked = json.loads(json.dumps(flat))
    linked["concepts"][1]["parent_uri"] = "Base"

    with _make_pkg(tmp_path) as pkg:
        seed_vocabulary_file(pkg, _write_vocab(tmp_path, flat, name="flat.json"))
        seed_vocabulary_file(
            pkg,
            _write_vocab(tmp_path, linked, name="linked.json"),
        )

        base_id = pkg.resolve_semantic_class("Base")
        derived_id = pkg.resolve_semantic_class("Derived")

        depth = pkg.conn.execute(
            """
            SELECT depth
            FROM usap_semantic_class_closure
            WHERE ancestor_class_id = ?
              AND descendant_class_id = ?
            """,
            (base_id, derived_id),
        ).fetchone()

        assert depth is not None, "backfilled parent never reached the closure"
        assert int(depth["depth"]) == 1
        assert_package_valid(pkg)


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


def test_ambiguous_vocabulary_parent_reports_ambiguity(tmp_path: Path) -> None:
    # A vocabulary parent name that matches concepts in several schemes must
    # surface as ambiguity, not as "not registered".
    with _make_pkg(tmp_path) as pkg:
        pkg.create_semantic_class(scheme="s1", class_uri="s1:Dup", local_name="Dup")
        pkg.create_semantic_class(scheme="s2", class_uri="s2:Dup", local_name="Dup")

        vocab_path = _write_vocab(
            tmp_path,
            {
                "scheme": "s3",
                "concepts": [{"local_name": "Child", "parent_uri": "Dup"}],
            },
            name="ambiguous_parent.json",
        )

        with pytest.raises(USAPAmbiguityError, match="ambiguous"):
            seed_vocabulary_file(pkg, vocab_path)

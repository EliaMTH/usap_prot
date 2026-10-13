from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from conftest import CITYGML_SCHEMA_FIXTURE, make_pkg, seed_citygml_concepts
from usap import (
    USAPAmbiguityError,
    USAPError,
    USAPPackage,
    load_citygml_schema,
    seed_default_ade_vocabulary,
    seed_vocabulary_file,
)
from usap.constants import CITYGML_3_0_BUILDING_NS


def test_vocabulary_seeding_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        first_citygml = seed_citygml_concepts(pkg)
        first_ade = seed_default_ade_vocabulary(pkg)

        count_after_first = pkg.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM usap_semantic_class
            """
        ).fetchone()["n"]

        second_citygml = seed_citygml_concepts(pkg)
        second_ade = seed_default_ade_vocabulary(pkg)

        count_after_second = pkg.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM usap_semantic_class
            """
        ).fetchone()["n"]

        assert count_after_second == count_after_first

        assert (
            second_citygml.by_name["RoofSurface"]
            == first_citygml.by_name["RoofSurface"]
        )

        assert (
            second_ade.by_name["EnergyRoof"]
            == first_ade.by_name["EnergyRoof"]
        )

        report = pkg.validate_report()
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_list_accepted_concepts(tmp_path: Path) -> None:
    db_path = tmp_path / "accepted.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        seed_citygml_concepts(pkg)
        seed_default_ade_vocabulary(pkg)

        all_concepts = pkg.list_accepted_concepts()
        ade_concepts = pkg.list_accepted_concepts(is_ade=True)
        citygml_concepts = pkg.list_accepted_concepts(scheme="citygml")
        roof_search = pkg.list_accepted_concepts(search="Roof")

        all_names = {item["local_name"] for item in all_concepts}
        ade_names = {item["local_name"] for item in ade_concepts}
        citygml_names = {item["local_name"] for item in citygml_concepts}
        roof_names = {item["local_name"] for item in roof_search}

        assert "RoofSurface" in all_names
        assert "EnergyRoof" in all_names

        assert "EnergyRoof" in ade_names
        assert "RoofSurface" not in ade_names

        assert "RoofSurface" in citygml_names
        assert "EnergyRoof" not in citygml_names

        assert "RoofSurface" in roof_names
        assert "EnergyRoof" in roof_names


def test_get_semantic_class_and_concept_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "get_concept.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        seed_citygml_concepts(pkg)
        seed_default_ade_vocabulary(pkg)

        roof = pkg.get_semantic_class("RoofSurface")
        energy = pkg.get_semantic_class("EnergyRoof")

        assert roof["scheme"] == "citygml"
        assert roof["local_name"] == "RoofSurface"
        assert roof["is_ade"] is False

        assert energy["scheme"] == "usap-ade-prototype"
        assert energy["local_name"] == "EnergyRoof"
        assert energy["is_ade"] is True

        assert pkg.concept_exists("RoofSurface") is True
        assert pkg.concept_exists("EnergyRoof") is True
        assert pkg.concept_exists("DefinitelyNotAConcept") is False


def test_unknown_concept_fails_loudly(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        seed_citygml_concepts(pkg)

        with pytest.raises(USAPError, match="not found"):
            pkg.resolve_semantic_class("NotRegistered")

        # match= guards against passing because of the nonexistent
        # asset_part_id instead of the unknown concept.
        with pytest.raises(USAPError, match="concept not found"):
            pkg.annotate_elements(
                concept="NotRegistered",
                asset_part_id=1,
                element_kind="face",
                element_indices=[1],
            )


def test_ambiguous_local_name_requires_scheme_or_uri(tmp_path: Path) -> None:
    db_path = tmp_path / "ambiguous.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        citygml_roof = pkg.create_semantic_class(
            scheme="citygml",
            scheme_version="3.0",
            class_uri="http://www.opengis.net/citygml/construction/3.0#Roof",
            local_name="Roof",
            is_ade=False,
        )

        ade_roof = pkg.create_semantic_class(
            scheme="my-ade",
            scheme_version="0.1",
            class_uri="my-ade:energy:Roof",
            local_name="Roof",
            is_ade=True,
        )

        # Ambiguity has its own exception type since the second audit.
        with pytest.raises(USAPAmbiguityError, match="ambiguous"):
            pkg.resolve_semantic_class("Roof")

        assert (
            pkg.resolve_semantic_class("Roof", scheme="citygml")
            == citygml_roof
        )

        assert (
            pkg.resolve_semantic_class("Roof", scheme="my-ade")
            == ade_roof
        )

        assert (
            pkg.resolve_semantic_class("my-ade:energy:Roof")
            == ade_roof
        )

# ---------------------------------------------------------------------------
# One transaction per vocabulary file
# ---------------------------------------------------------------------------


def _commits(pkg: USAPPackage, call: Callable[[], object]) -> int:
    """How many COMMIT statements call() issues on the package's connection."""
    statements: list[str] = []
    pkg.conn.set_trace_callback(statements.append)

    try:
        call()
    finally:
        pkg.conn.set_trace_callback(None)

    return sum(1 for sql in statements if sql.lstrip().upper().startswith("COMMIT"))


def _registry(pkg: USAPPackage) -> tuple[list[tuple], list[tuple]]:
    """Every concept and closure row, for comparing two seeded packages."""
    concepts = pkg.conn.execute(
        """
        SELECT semantic_class_id, scheme, scheme_version, class_uri, local_name,
               parent_class_id, source_namespace, concept_iri, is_ade,
               metadata_json
        FROM usap_semantic_class
        ORDER BY semantic_class_id
        """
    ).fetchall()
    closure = pkg.conn.execute(
        """
        SELECT ancestor_class_id, descendant_class_id, depth
        FROM usap_semantic_class_closure
        ORDER BY ancestor_class_id, descendant_class_id
        """
    ).fetchall()

    return [tuple(row) for row in concepts], [tuple(row) for row in closure]


def test_seeding_a_vocabulary_file_commits_once(tmp_path: Path) -> None:
    # create_semantic_class opens its own transaction, so outside an outer one
    # every concept used to commit -- and sync -- on its own: City3D's first
    # annotation timed out seeding 889 concepts on a slow disk. One file is now
    # one commit, and it writes exactly what it writes inside a caller's
    # transaction.
    vocabulary = tmp_path / "chains.json"
    vocabulary.write_text(
        json.dumps(
            {
                "scheme": "chains",
                "concepts": [
                    {
                        "local_name": f"C{i}",
                        **({"parent_uri": f"C{i - 1}"} if i % 4 else {}),
                    }
                    for i in range(40)
                ],
            }
        ),
        encoding="utf-8",
    )

    with make_pkg(tmp_path, "alone.usap.gpkg") as alone, make_pkg(
        tmp_path, "wrapped.usap.gpkg"
    ) as wrapped:

        def inside_a_transaction() -> None:
            with wrapped.transaction():
                seed_vocabulary_file(wrapped, vocabulary)

        assert _commits(alone, lambda: seed_vocabulary_file(alone, vocabulary)) == 1
        assert _commits(wrapped, inside_a_transaction) == 1

        assert _registry(alone) == _registry(wrapped)
        assert len(_registry(alone)[0]) == 40
        assert len(_registry(alone)[1]) == 100  # 10 chains of depth 4


def test_loading_a_citygml_schema_commits_once(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        assert _commits(pkg, lambda: seed_citygml_concepts(pkg)) == 1
        assert pkg.resolve_semantic_class("RoofSurface")


def test_a_vocabulary_file_that_fails_halfway_leaves_nothing(tmp_path: Path) -> None:
    # Two concepts load, the third names a parent that does not exist. With
    # one transaction per file none of the three stays written, so the file
    # can be fixed and re-run from clean.
    vocabulary = tmp_path / "broken.json"
    vocabulary.write_text(
        json.dumps(
            {
                "scheme": "s",
                "concepts": [
                    {"local_name": "A"},
                    {"local_name": "B", "parent_uri": "A"},
                    {"local_name": "C", "parent_uri": "Missing"},
                ],
            }
        ),
        encoding="utf-8",
    )

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(ValueError, match="Missing"):
            seed_vocabulary_file(pkg, vocabulary)

        assert _registry(pkg) == ([], [])


def test_a_citygml_schema_that_fails_halfway_leaves_nothing(tmp_path: Path) -> None:
    # The schema's own Building contradicts one already registered under the
    # same class_uri, so loading stops there -- after the abstract classes
    # created parents-first before it. None of those may stay written.
    building_uri = f"{CITYGML_3_0_BUILDING_NS}#Building"

    with make_pkg(tmp_path) as pkg:
        existing = pkg.create_semantic_class(
            scheme="citygml",
            class_uri=building_uri,
            local_name="Building",
            scheme_version="2.0",
        )

        with pytest.raises(USAPError, match="scheme_version"):
            load_citygml_schema(pkg, CITYGML_SCHEMA_FIXTURE, scheme_version="3.0")

        concepts, _ = _registry(pkg)

        assert [row[0] for row in concepts] == [existing]

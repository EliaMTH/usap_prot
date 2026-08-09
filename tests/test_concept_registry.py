from __future__ import annotations

from pathlib import Path

import pytest

from conftest import seed_citygml_concepts
from usap import (
    USAPAmbiguityError,
    USAPError,
    USAPPackage,
    seed_default_ade_vocabulary,
)


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
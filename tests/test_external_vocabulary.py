from __future__ import annotations

from pathlib import Path

from usap import (
    USAPPackage,
    seed_default_ade_vocabulary,
    seed_default_citygml_vocabulary,
)


def test_seed_default_citygml_vocabulary(tmp_path: Path) -> None:
    db_path = tmp_path / "citygml_vocab.usap.gpkg"

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        vocab = seed_default_citygml_vocabulary(pkg)

        assert "Building" in vocab.by_name
        assert "RoofSurface" in vocab.by_name
        assert "WallSurface" in vocab.by_name
        assert "Window" in vocab.by_name
        assert "Road" in vocab.by_name
        assert "TrafficArea" in vocab.by_name
        assert "CityFurniture" in vocab.by_name
        assert "TINRelief" in vocab.by_name

        assert pkg.resolve_semantic_class("RoofSurface") == vocab.by_name["RoofSurface"]
        assert (
            pkg.resolve_semantic_class("citygml-3.0:building:RoofSurface")
            == vocab.by_name["RoofSurface"]
        )

        concepts = pkg.list_accepted_concepts(scheme="citygml")

        names = {item["local_name"] for item in concepts}

        assert "Building" in names
        assert "RoofSurface" in names
        assert "Road" in names
        assert "TrafficSpace" in names

        report = pkg.validate_report()
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_seed_default_ade_vocabulary(tmp_path: Path) -> None:
    db_path = tmp_path / "ade_vocab.usap.gpkg"

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        vocab = seed_default_ade_vocabulary(pkg)

        assert "EnergyRoof" in vocab.by_name
        assert "VisualFacade" in vocab.by_name
        assert "PermeabilityExternalSurface" in vocab.by_name

        concepts = pkg.list_accepted_concepts(is_ade=True)
        names = {item["local_name"] for item in concepts}

        assert "EnergyRoof" in names
        assert "VisualFacade" in names

# NOTE: seeding idempotency is covered (as a superset: both vocabularies,
# stable ids, validation) by
# test_concept_registry.py::test_vocabulary_seeding_is_idempotent.
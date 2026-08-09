from __future__ import annotations

from pathlib import Path

from conftest import seed_citygml_concepts
from usap import (
    USAPPackage,
    city_object_classes,
    is_city_object_class,
    seed_default_ade_vocabulary,
)


def _ancestors(pkg: USAPPackage, local_name: str) -> list[str]:
    """Local names from the concept up to its root, nearest first."""
    class_id = pkg.resolve_semantic_class(local_name)

    rows = pkg.conn.execute(
        """
        SELECT ancestor.local_name
        FROM usap_semantic_class_closure AS c
        JOIN usap_semantic_class AS ancestor
            ON ancestor.semantic_class_id = c.ancestor_class_id
        WHERE c.descendant_class_id = ?
        ORDER BY c.depth
        """,
        (class_id,),
    ).fetchall()

    return [row["local_name"] for row in rows]


def test_citygml_concepts_are_derived_from_the_schema(tmp_path: Path) -> None:
    # The registry is read from the OGC XSD, not asserted by USAP. This test
    # replaces the deleted hand-written citygml_3_0_mvp.json, and every
    # assertion here is one that file got wrong.
    db_path = tmp_path / "citygml_vocab.usap.gpkg"

    with USAPPackage.create(db_path, overwrite=True) as pkg:
        vocab = seed_citygml_concepts(pkg)

        assert {
            "Building",
            "RoofSurface",
            "WallSurface",
            "Window",
            "Road",
            "TrafficArea",
            "CityFurniture",
            "TINRelief",
        } <= set(vocab.by_name)

        # Identity is the real QName: the thematic surfaces are construction
        # concepts in 3.0, not building ones.
        roof = pkg.conn.execute(
            """
            SELECT class_uri, source_namespace
            FROM usap_semantic_class
            WHERE local_name = 'RoofSurface'
            """
        ).fetchone()

        assert roof["source_namespace"] == (
            "http://www.opengis.net/citygml/construction/3.0"
        )
        assert roof["class_uri"] == (
            "http://www.opengis.net/citygml/construction/3.0#RoofSurface"
        )

        # The space / space-boundary layer is present rather than collapsed.
        assert _ancestors(pkg, "RoofSurface") == [
            "RoofSurface",
            "AbstractConstructionSurface",
            "AbstractThematicSurface",
            "AbstractSpaceBoundary",
            "AbstractCityObject",
            "AbstractFeatureWithLifespan",
            "AbstractFeature",
        ]

        # Window is a filling *element*, not a filling surface.
        assert "AbstractFillingElement" in _ancestors(pkg, "Window")
        assert "AbstractFillingSurface" in _ancestors(pkg, "WindowSurface")

        # A relief component's parent is AbstractReliefComponent; ReliefFeature
        # aggregates components, it does not generalize them.
        assert "AbstractReliefComponent" in _ancestors(pkg, "TINRelief")
        assert "ReliefFeature" not in _ancestors(pkg, "TINRelief")

        # WaterClosureSurface exists only in CityGML 2.0.
        assert "WaterClosureSurface" not in vocab.by_name

        report = pkg.validate_report()
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_relation_objects_are_concepts_but_not_city_objects(tmp_path: Path) -> None:
    # CityObjectRelation and Role are real CityGML classes, so they register —
    # but neither substitutes AbstractCityObject, and instantiating one would
    # put a relation object into usap_city_object.
    with USAPPackage.create(tmp_path / "roles.usap.gpkg", overwrite=True) as pkg:
        vocab = seed_citygml_concepts(pkg)

        assert "CityObjectRelation" in vocab.by_name
        assert "CityModel" in vocab.by_name

        assert not is_city_object_class(pkg, vocab.by_name["CityObjectRelation"])
        assert not is_city_object_class(pkg, vocab.by_name["CityModel"])
        assert not is_city_object_class(pkg, vocab.by_name["Address"])

        assert is_city_object_class(pkg, vocab.by_name["Building"])
        assert is_city_object_class(pkg, vocab.by_name["RoofSurface"])

        # city_object_classes is what the importer filters on, keyed by QName.
        instantiable = city_object_classes(pkg)

        assert (
            "http://www.opengis.net/citygml/building/3.0",
            "Building",
        ) in instantiable
        assert (
            "http://www.opengis.net/citygml/3.0",
            "CityObjectRelation",
        ) not in instantiable


def test_seed_default_ade_vocabulary(tmp_path: Path) -> None:
    db_path = tmp_path / "ade_vocab.usap.gpkg"

    with USAPPackage.create(
        db_path,
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
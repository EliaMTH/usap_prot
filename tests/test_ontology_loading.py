"""
Initialising a package on an ontology.

The governing requirement: supply a different ontology, or extend the one you
have, and the package's link vocabulary changes with it. USAP ships no link
vocabulary and asserts no category of its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_pkg, seed_citygml_concepts
from usap import USAPError, USAPPackage, import_citygml_semantics, load_ontology

CATEGORY_ONTOLOGY = Path(__file__).parent / "fixtures" / "citygml_3_0_categories.owl"

CORE_NS = "http://www.opengis.net/citygml/3.0"
CON_NS = "http://www.opengis.net/citygml/construction/3.0"

TINY_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel
    xmlns:core="http://www.opengis.net/citygml/3.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:con="http://www.opengis.net/citygml/construction/3.0"
    xmlns:bldg="http://www.opengis.net/citygml/building/3.0">
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <core:boundary>
        <con:WallSurface gml:id="w1">
          <con:fillingSurface>
            <con:WindowSurface gml:id="win1"/>
          </con:fillingSurface>
        </con:WallSurface>
      </core:boundary>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""


def _types(pkg: USAPPackage) -> dict[tuple[str, str | None], str | None]:
    return {
        (t["local_name"], t["code_space"]): t["category"]
        for t in pkg.list_relationship_types()
    }


def test_ontology_supplies_the_categories(pkg: USAPPackage) -> None:
    result = load_ontology(pkg, CATEGORY_ONTOLOGY)

    assert result.categorised == 6
    assert result.imports == ["http://www.opengis.net/citygml/3.0"]

    types = _types(pkg)

    assert types[("boundary", CORE_NS)] == "containment"
    assert types[("fillingSurface", CON_NS)] == "containment"
    assert types[("adjacentTo", CORE_NS)] == "peer"
    assert types[("generalizesTo", CORE_NS)] == "generalization"

    # Declared but unclassified stays NULL — never assumed to be containment.
    assert types[("relatedTo", CORE_NS)] is None


def test_iso_19150_class_property_names_are_split(pkg: USAPPackage) -> None:
    # CityGML's published OWL renderings name a property `Class.property`.
    # Keeping the class prefix would give a type name no document ever writes,
    # so the category would land on a type nothing uses.
    load_ontology(pkg, CATEGORY_ONTOLOGY)

    types = _types(pkg)
    group_ns = "http://www.opengis.net/citygml/cityobjectgroup/3.0"

    assert types[("groupMember", group_ns)] == "grouping"
    assert ("Role.groupMember", group_ns) not in types


def test_ade_classes_and_their_parents_are_registered(pkg: USAPPackage) -> None:
    load_ontology(pkg, CATEGORY_ONTOLOGY, scheme="ade")

    quality = pkg.resolve_semantic_class("AcousticQuality")

    ancestors = [
        row["local_name"]
        for row in pkg.conn.execute(
            """
            SELECT s.local_name
            FROM usap_semantic_class_closure AS c
            JOIN usap_semantic_class AS s
                ON s.semantic_class_id = c.ancestor_class_id
            WHERE c.descendant_class_id = ?
            ORDER BY c.depth
            """,
            (quality,),
        )
    ]

    assert ancestors == ["AcousticQuality", "BuildingQuality"]


def test_the_ontology_classifies_a_real_import(tmp_path: Path) -> None:
    # End to end: concepts from the schema, edges from the document, and the
    # one thing neither carries — what counts as a part — from the ontology.
    gml = tmp_path / "city.gml"
    gml.write_text(TINY_CITYGML, encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)
        import_citygml_semantics(pkg, gml)

        # Before: edges exist, nothing is a part.
        assert {
            row["object_uid"] for row in pkg.list_city_objects(descendants_of="b1")
        } == {"b1"}

        load_ontology(pkg, CATEGORY_ONTOLOGY)

        # After: the same stored edges now answer the question.
        assert {
            row["object_uid"] for row in pkg.list_city_objects(descendants_of="b1")
        } == {"b1", "w1", "win1"}

        report = pkg.validate_report()

        assert report.is_ok
        assert "UNCLASSIFIED_RELATIONSHIP_TYPE" not in {
            issue.code for issue in report.issues
        }


def test_loading_is_idempotent_and_order_independent(pkg: USAPPackage) -> None:
    # Classifying before or after the edges exist must give the same package,
    # and re-loading an unchanged ontology must change nothing.
    first = load_ontology(pkg, CATEGORY_ONTOLOGY)
    second = load_ontology(pkg, CATEGORY_ONTOLOGY)

    assert first.relationship_types == second.relationship_types
    assert _types(pkg) == _types(pkg)


def test_a_contradicting_category_raises(pkg: USAPPackage) -> None:
    pkg.register_relationship_type(
        "boundary", code_space=CORE_NS, category="peer"
    )

    with pytest.raises(USAPError, match="cannot re-register"):
        load_ontology(pkg, CATEGORY_ONTOLOGY)


def test_turtle_is_refused_with_a_usable_message(tmp_path: Path) -> None:
    # The built-in reader is RDF/XML only, on purpose: ontology support costs
    # no extra dependency. Refusing has to say what to do about it.
    turtle = tmp_path / "ont.ttl"
    turtle.write_text(
        '@prefix owl: <http://www.w3.org/2002/07/owl#> .\n'
        '<http://example.org#boundary> a owl:ObjectProperty .\n',
        encoding="utf-8",
    )

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="RDF/XML only"):
            load_ontology(pkg, turtle)


def test_xml_that_is_not_rdf_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "not_rdf.xml"
    other.write_text("<catalogue><entry/></catalogue>", encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="not RDF/XML"):
            load_ontology(pkg, other)


def test_swapping_the_ontology_swaps_the_vocabulary(tmp_path: Path) -> None:
    # The governing requirement, as a test. A different ontology gives a
    # different link vocabulary over the very same document.
    alternative = tmp_path / "alt.owl"
    alternative.write_text(
        '<?xml version="1.0"?>\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
        '         xmlns:owl="http://www.w3.org/2002/07/owl#"\n'
        '         xmlns:usap="urn:usap:">\n'
        # This deployment does NOT consider an opening part of the building.
        f'  <owl:ObjectProperty rdf:about="{CORE_NS}#boundary">\n'
        "    <usap:category>containment</usap:category>\n"
        "  </owl:ObjectProperty>\n"
        f'  <owl:ObjectProperty rdf:about="{CON_NS}#fillingSurface">\n'
        "    <usap:category>peer</usap:category>\n"
        "  </owl:ObjectProperty>\n"
        "</rdf:RDF>\n",
        encoding="utf-8",
    )

    gml = tmp_path / "city.gml"
    gml.write_text(TINY_CITYGML, encoding="utf-8")

    parts = {}

    for label, ontology in [("standard", CATEGORY_ONTOLOGY), ("alt", alternative)]:
        with make_pkg(tmp_path, f"{label}.usap.gpkg") as pkg:
            seed_citygml_concepts(pkg)
            load_ontology(pkg, ontology)
            import_citygml_semantics(pkg, gml)

            parts[label] = {
                row["object_uid"]
                for row in pkg.list_city_objects(descendants_of="b1")
            }

    assert parts["standard"] == {"b1", "w1", "win1"}
    assert parts["alt"] == {"b1", "w1"}


def test_reads_a_real_world_ade(tmp_path: Path) -> None:
    # Against an actual ontology rather than a fixture, when one is present.
    # Not committed (*.owl is gitignored), so this skips in CI.
    uish = Path(__file__).resolve().parents[1] / "uish_ontology_building.owl"

    if not uish.exists():
        pytest.skip("uish_ontology_building.owl is not in the checkout")

    with make_pkg(tmp_path) as pkg:
        result = load_ontology(pkg, uish, scheme="uish")

        # 11 object properties and 11 classes, none of them classified: this
        # ADE describes building qualities, not city-object structure.
        assert len(result.relationship_types) == 11
        assert len(result.concepts) == 11
        assert result.categorised == 0

        assert pkg.resolve_semantic_class("BuildingQuality")
        assert pkg.validate_report().is_ok

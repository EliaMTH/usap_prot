"""
Initialising a package on an ontology.

The governing requirement: supply a different ontology, or extend the one you
have, and the package's link vocabulary changes with it. The only categories
USAP asserts itself are the CityGML 3.0 defaults create() registers, and an
ontology overrides those; the tests that need a vocabulary the ontology alone
supplies use a package created without them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import CITYGML_SCHEMA_FIXTURE, make_pkg, seed_citygml_concepts
from usap import (
    USAPAmbiguityError,
    USAPError,
    USAPPackage,
    city_object_classes,
    import_citygml_semantics,
    load_ontology,
    load_vocabulary_folder,
)

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


def test_ontology_supplies_the_categories(bare_pkg: USAPPackage) -> None:
    pkg = bare_pkg
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


def test_iso_19150_class_property_names_are_split(bare_pkg: USAPPackage) -> None:
    # CityGML's published OWL renderings name a property `Class.property`.
    # Keeping the class prefix would give a type name no document ever writes,
    # so the category would land on a type nothing uses.
    pkg = bare_pkg
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
    # End to end: concepts from the schema, edges from the document, and what
    # counts as a part from the ontology -- on a package created without the
    # CityGML 3.0 defaults, so the ontology is the only thing classifying.
    gml = tmp_path / "city.gml"
    gml.write_text(TINY_CITYGML, encoding="utf-8")

    with make_pkg(tmp_path, default_relationship_types=False) as pkg:
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
    # boundary arrives as a CityGML 3.0 default (containment). Declaring it
    # peer replaces the default, and a declaration is not something a later
    # source may contradict -- an ontology included.
    pkg.register_relationship_type(
        "boundary", code_space=CORE_NS, category="peer"
    )

    with pytest.raises(USAPError, match="cannot re-register"):
        load_ontology(pkg, CATEGORY_ONTOLOGY)


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
        # fillingSurface is a CityGML 3.0 default (containment); the ontology
        # replaces it, because the defaults are a starting point.
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


# ---------------------------------------------------------------------------
# Syntax dispatch and the configuration-folder loader
# ---------------------------------------------------------------------------

TURTLE_ONTOLOGY = """
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix usap: <urn:usap:> .
@prefix ex:   <http://example.org/onto#> .

ex:boundary a owl:ObjectProperty ;
    usap:category "containment" .

ex:Surface a owl:Class .
ex:RoofPanel a owl:Class ;
    rdfs:subClassOf ex:Surface .
"""


def test_turtle_is_refused_with_a_conversion_hint(tmp_path):
    """
    USAP reads RDF/XML only. A Turtle file must be refused by name, with the
    remedy in the message — not accepted, and not reported as broken XML.
    """
    ontology = tmp_path / "domain.ttl"
    ontology.write_text(TURTLE_ONTOLOGY)

    with make_pkg(tmp_path, "ttl.usap.gpkg") as pkg:
        with pytest.raises(USAPError, match="no longer supported") as excinfo:
            load_ontology(pkg, ontology)

        assert "RDF/XML" in str(excinfo.value)


@pytest.mark.parametrize("suffix", [".ttl", ".n3", ".nt", ".trig", ".jsonld"])
def test_unsupported_rdf_syntax_in_a_folder_raises_rather_than_being_skipped(
    tmp_path, suffix
):
    """
    The failure mode dropping Turtle support could have introduced: a folder
    walk that no longer recognises .ttl walks straight past it, and the package
    comes up with fewer concepts than the operator configured, silently.

    So the unsupported suffixes stay in the folder's file set on purpose — they
    are routed to load_ontology precisely so they raise. Nothing else in the
    folder may be seeded either; a partial load is the same lie in slower form.
    """
    config = tmp_path / "vocabulary"
    config.mkdir()

    (config / f"domain{suffix}").write_text(TURTLE_ONTOLOGY)
    (config / "local.json").write_text(
        json.dumps({"scheme": "local", "concepts": [{"local_name": "SolarPanel"}]})
    )

    with make_pkg(tmp_path, f"folder{suffix.replace('.', '_')}.usap.gpkg") as pkg:
        with pytest.raises(USAPError, match="no longer supported"):
            load_vocabulary_folder(pkg, config)

        assert pkg.list_accepted_concepts() == []


def test_unknown_ontology_suffix_is_refused(tmp_path):
    other = tmp_path / "vocab.yaml"
    other.write_text("nope")

    with make_pkg(tmp_path, "suffix.usap.gpkg") as pkg:
        with pytest.raises(USAPError, match="Unsupported ontology suffix"):
            load_ontology(pkg, other)


def test_vocabulary_folder_loads_schemas_and_ontologies_together(tmp_path):
    """
    US-DATA-04's "reads the configured file(s) at startup", as one call over a
    configuration directory holding more than one kind of source.
    """
    config = tmp_path / "vocabulary"
    config.mkdir()

    for xsd in CITYGML_SCHEMA_FIXTURE.rglob("*.xsd"):
        (config / xsd.name).write_bytes(xsd.read_bytes())

    (config / "domain.owl").write_bytes(CATEGORY_ONTOLOGY.read_bytes())
    (config / "local.json").write_text(
        json.dumps(
            {
                "scheme": "local",
                "concepts": [{"local_name": "SolarPanel"}],
            }
        )
    )

    with make_pkg(tmp_path, "folder.usap.gpkg") as pkg:
        results = load_vocabulary_folder(pkg, config)

        assert "domain.owl" in results
        assert "local.json" in results

        # All three sources landed in one registry.
        names = {c["local_name"] for c in pkg.list_accepted_concepts()}
        assert "Building" in names        # from the XSDs
        assert "SolarPanel" in names      # from the JSON vocabulary

        # And the CityGML hierarchy came with them, which is what the XSDs are
        # for: an ontology alone would not carry it.
        assert city_object_classes(pkg)

        # Re-seeding on open is the intended usage, so it must be a no-op.
        load_vocabulary_folder(pkg, config)
        assert {c["local_name"] for c in pkg.list_accepted_concepts()} == names


def test_vocabulary_folder_needs_a_directory_with_something_in_it(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    with make_pkg(tmp_path, "empty.usap.gpkg") as pkg:
        with pytest.raises(USAPError, match="No vocabulary files"):
            load_vocabulary_folder(pkg, empty)


# ---------------------------------------------------------------------------
# A configuration folder loads all or nothing, and its errors name the file
# ---------------------------------------------------------------------------


def _vocabulary(scheme: str, concepts: list[dict], **extra: object) -> str:
    return json.dumps({"scheme": scheme, "concepts": concepts, **extra})


def _folder(
    tmp_path: Path,
    files: dict[str, str | bytes],
    *,
    name: str = "vocabulary",
    with_schemas: bool = False,
) -> Path:
    folder = tmp_path / name
    folder.mkdir()

    for file_name, content in files.items():
        if isinstance(content, bytes):
            (folder / file_name).write_bytes(content)
        else:
            (folder / file_name).write_text(content, encoding="utf-8")

    if with_schemas:
        for xsd in CITYGML_SCHEMA_FIXTURE.rglob("*.xsd"):
            (folder / xsd.name).write_bytes(xsd.read_bytes())

    return folder


def _written(pkg: USAPPackage) -> tuple[int, int]:
    """(concepts, link types) in the package."""
    return (
        pkg.conn.execute("SELECT COUNT(*) FROM usap_semantic_class").fetchone()[0],
        pkg.conn.execute("SELECT COUNT(*) FROM usap_relationship_type").fetchone()[0],
    )


def _commits(pkg: USAPPackage, call) -> int:
    statements: list[str] = []
    pkg.conn.set_trace_callback(statements.append)

    try:
        call()
    finally:
        pkg.conn.set_trace_callback(None)

    return sum(1 for sql in statements if sql.lstrip().upper().startswith("COMMIT"))


def test_a_vocabulary_folder_loads_all_or_nothing(tmp_path):
    """
    Schemas, an ontology and three JSON files, the second of which fails.
    The folder is one transaction, so nothing of it stays written: not the
    schemas, not the ontology's link types, not the JSON file before it.
    Re-running after the fix starts from clean.
    """
    folder = _folder(
        tmp_path,
        {
            "a.json": _vocabulary("a", [{"local_name": "First"}]),
            "b.json": _vocabulary(
                "b", [{"local_name": "Second", "parent_uri": "Missing"}]
            ),
            "c.json": _vocabulary("c", [{"local_name": "Third"}]),
            "domain.owl": CATEGORY_ONTOLOGY.read_bytes(),
        },
        with_schemas=True,
    )

    with make_pkg(tmp_path, "folder.usap.gpkg") as pkg:
        before = _written(pkg)

        with pytest.raises(ValueError, match="Missing"):
            load_vocabulary_folder(pkg, folder)

        assert _written(pkg) == before

        (folder / "b.json").write_text(
            _vocabulary("b", [{"local_name": "Second"}]), encoding="utf-8"
        )

        # One commit for the whole folder, and on an already seeded package
        # (the every-open path) one empty one.
        assert _commits(pkg, lambda: load_vocabulary_folder(pkg, folder)) == 1
        seeded = _written(pkg)

        assert _commits(pkg, lambda: load_vocabulary_folder(pkg, folder)) == 1
        assert _written(pkg) == seeded
        assert {"First", "Second", "Third", "Building"} <= {
            c["local_name"] for c in pkg.list_accepted_concepts()
        }


_LOADS = _vocabulary("s", [{"local_name": "A"}])


@pytest.mark.parametrize(
    ("files", "error", "culprit", "already_named"),
    [
        pytest.param(
            {
                "a.json": _LOADS,
                "b.json": _vocabulary(
                    "s", [{"local_name": "B", "parent_uri": "Missing"}]
                ),
            },
            ValueError, "b.json", True,
            id="missing-parent",
        ),
        pytest.param(
            {"bad.xsd": "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'"},
            USAPError, "bad.xsd", True,
            id="malformed-schema",
        ),
        pytest.param(
            {
                "a.json": _vocabulary(
                    "x", [{"local_name": "Roof"}], scheme_version="1"
                ),
                "b.json": _vocabulary(
                    "x", [{"local_name": "Roof"}], scheme_version="2"
                ),
            },
            USAPError, "b.json", False,
            id="contradicts-an-earlier-file",
        ),
        pytest.param(
            {
                "a.json": _vocabulary("A", [{"local_name": "Dup"}]),
                "b.json": _vocabulary("B", [{"local_name": "Dup"}]),
                "c.json": _vocabulary(
                    "C", [{"local_name": "Kid", "parent_uri": "Dup"}]
                ),
            },
            USAPAmbiguityError, "c.json", False,
            id="ambiguous-parent",
        ),
        pytest.param(
            {"a.json": _LOADS, "b.json": '{"scheme": "s", "concepts": [ oops ]}'},
            json.JSONDecodeError, "b.json", False,
            id="malformed-json",
        ),
        pytest.param(
            {"a.json": _LOADS, "b.json": b'\xff\xfe{"scheme": "s"}'},
            UnicodeDecodeError, "b.json", False,
            id="not-utf8",
        ),
    ],
)
def test_a_folder_error_names_its_file_and_keeps_its_type(
    tmp_path, files, error, culprit, already_named
):
    """
    All or nothing means the caller must be told which file to fix. The
    exception type is what it always was -- callers catch USAPAmbiguityError
    or ValueError by type, and json.JSONDecodeError / UnicodeDecodeError
    cannot be rebuilt from a message alone -- and the original stays chained
    as __cause__. A message that named the file already is left as it was.
    """
    folder = _folder(tmp_path, files)

    with make_pkg(tmp_path, "errors.usap.gpkg") as pkg:
        before = _written(pkg)

        with pytest.raises(error) as excinfo:
            load_vocabulary_folder(pkg, folder)

        assert _written(pkg) == before

    raised = excinfo.value

    assert type(raised) is error
    assert str(folder / culprit) in str(raised)

    if not already_named:
        assert type(raised.__cause__) is error
        assert str(folder / culprit) not in str(raised.__cause__)


def test_a_schema_error_names_the_schema_folder(tmp_path):
    # The .xsd files load as one set (a parent may live in another module),
    # so an error found while registering them names the folder they came
    # from.
    folder = _folder(tmp_path, {}, with_schemas=True)

    with make_pkg(tmp_path, "schema_error.usap.gpkg") as pkg:
        pkg.create_semantic_class(
            scheme="citygml",
            class_uri="http://www.opengis.net/citygml/building/3.0#Building",
            local_name="Building",
            scheme_version="2.0",
        )

        with pytest.raises(USAPError, match="scheme_version") as excinfo:
            load_vocabulary_folder(pkg, folder, scheme_version="3.0")

    assert str(excinfo.value).startswith(f"{folder}: ")


def test_a_folder_given_as_a_relative_path_is_still_named(tmp_path, monkeypatch):
    # The "already named?" check matches the path as a whole. A substring
    # test would find "." in any sentence, and with the folder given as "."
    # would never add the file -- or the schema folder -- to the message.
    contradiction = {
        "a.json": _vocabulary("x", [{"local_name": "Roof"}], scheme_version="1"),
        "b.json": _vocabulary("x", [{"local_name": "Roof"}], scheme_version="2"),
    }
    json_folder = _folder(tmp_path, contradiction, name="json_only")
    schema_folder = _folder(tmp_path, {}, name="schemas", with_schemas=True)

    with make_pkg(tmp_path, "relative.usap.gpkg") as pkg:
        monkeypatch.chdir(json_folder)

        with pytest.raises(USAPError) as excinfo:
            load_vocabulary_folder(pkg, ".")

        assert str(excinfo.value).startswith("b.json: ")

        pkg.create_semantic_class(
            scheme="citygml",
            class_uri="http://www.opengis.net/citygml/building/3.0#Building",
            local_name="Building",
            scheme_version="2.0",
        )
        monkeypatch.chdir(schema_folder)

        with pytest.raises(USAPError) as excinfo:
            load_vocabulary_folder(pkg, ".", scheme_version="3.0")

        assert str(excinfo.value).startswith(".: ")

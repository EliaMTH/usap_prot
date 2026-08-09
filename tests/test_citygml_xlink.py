"""
Relationship serialization: nesting is not the relationship.

A CityGML relationship is the named property element. Whether the target sits
inside that element or behind an xlink:href only says where the target is
*defined* — 29 of the 33 city-object properties accept either form, and six
accept nothing but a reference. The adapter this replaces read nesting alone,
so a document written by reference imported as a pile of unrelated roots with
no warning, and the xlink-only properties could never appear at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    make_pkg,
    seed_citygml_concepts,
    seed_citygml_relationship_categories,
)
from usap import USAPPackage, import_citygml_semantics

NS = """xmlns:core="http://www.opengis.net/citygml/3.0"
    xmlns:bldg="http://www.opengis.net/citygml/building/3.0"
    xmlns:con="http://www.opengis.net/citygml/construction/3.0"
    xmlns:grp="http://www.opengis.net/citygml/cityobjectgroup/3.0"
    xmlns:app="http://www.opengis.net/citygml/appearance/3.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:xlink="http://www.w3.org/1999/xlink\""""

# (A) inline: every target written inside the property that points at it.
NESTED = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <core:boundary>
        <con:RoofSurface gml:id="r1"/>
      </core:boundary>
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

# (B) flat + xlink: identical semantics, every feature a top-level member.
# This is the form CityGML forces whenever a surface is shared between two
# features, so it is not an exotic case.
FLAT = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <core:boundary xlink:href="#r1"/>
      <core:boundary xlink:href="#w1"/>
    </bldg:Building>
  </core:cityObjectMember>
  <core:cityObjectMember>
    <con:RoofSurface gml:id="r1"/>
  </core:cityObjectMember>
  <core:cityObjectMember>
    <con:WallSurface gml:id="w1">
      <con:fillingSurface xlink:href="#win1"/>
    </con:WallSurface>
  </core:cityObjectMember>
  <core:cityObjectMember>
    <con:WindowSurface gml:id="win1"/>
  </core:cityObjectMember>
</core:CityModel>
"""


def _import(tmp_path: Path, name: str, xml: str) -> USAPPackage:
    gml = tmp_path / f"{name}.gml"
    gml.write_text(xml, encoding="utf-8")

    pkg = make_pkg(tmp_path, f"{name}.usap.gpkg")
    seed_citygml_concepts(pkg)
    seed_citygml_relationship_categories(pkg)
    import_citygml_semantics(pkg, gml)

    return pkg


def _edges(pkg: USAPPackage) -> set[tuple[str, str | None, str, str | None]]:
    rows = pkg.conn.execute(
        """
        SELECT
            src.object_uid AS from_uid,
            dst.object_uid AS to_uid,
            rt.local_name,
            rt.code_space
        FROM usap_city_object_relationship AS r
        JOIN usap_relationship_type AS rt
            ON rt.relationship_type_id = r.relationship_type_id
        JOIN usap_city_object AS src
            ON src.city_object_id = r.from_city_object_id
        LEFT JOIN usap_city_object AS dst
            ON dst.city_object_id = r.to_city_object_id
        """
    ).fetchall()

    return {tuple(row) for row in rows}


def test_inline_and_xlink_produce_the_same_graph(tmp_path: Path) -> None:
    # The headline test. Before the two-pass rewrite this was 3 edges vs 0.
    with _import(tmp_path, "nested", NESTED) as a:
        with _import(tmp_path, "flat", FLAT) as b:
            nested_edges = _edges(a)
            flat_edges = _edges(b)

            # Assert non-emptiness explicitly: two empty sets are also equal,
            # and that is exactly the bug this guards against.
            assert nested_edges
            assert nested_edges == flat_edges

            for pkg in (a, b):
                assert {
                    row["object_uid"]
                    for row in pkg.list_city_objects(descendants_of="b1")
                } == {"b1", "r1", "w1", "win1"}


def test_xlink_only_properties_are_visible(tmp_path: Path) -> None:
    # generalizesTo and relatedTo accept no inline form at all, so a
    # nesting-only reader could never see them however the file was written.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <core:generalizesTo xlink:href="#b1_lod1"/>
      <core:relatedTo>
        <core:CityObjectRelation>
          <core:relationType codeSpace="https://example.org/rel">adjacentTo</core:relationType>
          <core:relatedTo xlink:href="#b2"/>
        </core:CityObjectRelation>
      </core:relatedTo>
    </bldg:Building>
  </core:cityObjectMember>
  <core:cityObjectMember><bldg:Building gml:id="b1_lod1"/></core:cityObjectMember>
  <core:cityObjectMember><bldg:Building gml:id="b2"/></core:cityObjectMember>
</core:CityModel>
"""

    with _import(tmp_path, "xlinkonly", xml) as pkg:
        edges = {
            (e["to_object_uid"], e["relationship_type"], e["code_space"])
            for e in pkg.related_city_objects("b1")
        }

        assert (
            "b1_lod1",
            "generalizesTo",
            "http://www.opengis.net/citygml/3.0",
        ) in edges

        # The objectified relation stores the relationType code VALUE as the
        # type, with its codeSpace — not the generic 'relatedTo' carrier, which
        # would make every open relation indistinguishable in SQL.
        assert ("b2", "adjacentTo", "https://example.org/rel") in edges

        adjacent = next(
            e
            for e in pkg.related_city_objects("b1")
            if e["relationship_type"] == "adjacentTo"
        )

        assert "relatedTo" in adjacent["metadata_json"]


def test_group_role_populates_the_role_column(tmp_path: Path) -> None:
    # grp:Role.role is the only role qualifier in CityGML 3.0. It is read from
    # the document, never derived from the target's class — which is all the
    # old adapter ever put there.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <grp:CityObjectGroup gml:id="g1">
      <grp:groupMember>
        <grp:Role>
          <grp:role>load-bearing</grp:role>
          <grp:groupMember xlink:href="#w1"/>
        </grp:Role>
      </grp:groupMember>
    </grp:CityObjectGroup>
  </core:cityObjectMember>
  <core:cityObjectMember><con:WallSurface gml:id="w1"/></core:cityObjectMember>
</core:CityModel>
"""

    with _import(tmp_path, "group", xml) as pkg:
        edges = pkg.related_city_objects("g1")

        assert len(edges) == 1
        assert edges[0]["to_object_uid"] == "w1"
        assert edges[0]["role"] == "load-bearing"


def test_target_outside_the_document_is_kept_and_warned(tmp_path: Path) -> None:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <core:boundary xlink:href="https://example.org/other.gml#r9"/>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""

    gml = tmp_path / "external.gml"
    gml.write_text(xml, encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)
        seed_citygml_relationship_categories(pkg)

        with pytest.warns(UserWarning, match="outside"):
            result = import_citygml_semantics(pkg, gml)

        assert len(result.unresolved_targets) == 1
        assert result.unresolved_targets[0].href == (
            "https://example.org/other.gml#r9"
        )

        edge = pkg.related_city_objects("b1")[0]

        assert edge["to_city_object_id"] is None
        assert edge["to_external_uri"] == "https://example.org/other.gml#r9"

        report = pkg.validate_report()
        codes = {issue.code for issue in report.issues}

        assert "UNRESOLVED_RELATIONSHIP_TARGET" in codes
        assert report.is_ok


def test_appearance_hrefs_do_not_become_city_object_edges(tmp_path: Path) -> None:
    # The appearance module is also under opengis.net/citygml, so an
    # unguarded xlink path would mint a bogus edge and a bogus link type from
    # <app:target>. An unknown property is only trusted when the href
    # resolves to something already known to be a city object.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel {NS}>
  <core:cityObjectMember>
    <bldg:Building gml:id="b1">
      <core:boundary>
        <con:RoofSurface gml:id="r1"/>
      </core:boundary>
      <app:someUnknownReference xlink:href="urn:not-a-city-object"/>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""

    gml = tmp_path / "appearance.gml"
    gml.write_text(xml, encoding="utf-8")

    with make_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)
        seed_citygml_relationship_categories(pkg)

        result = import_citygml_semantics(pkg, gml)

        assert result.relationship_count == 1
        assert result.skipped_references

        types = {t["local_name"] for t in pkg.list_relationship_types()}

        assert "someUnknownReference" not in types


def test_import_writes_one_graph(tmp_path: Path) -> None:
    # The import used to write every edge twice, into 'citygml_import' and a
    # mirrored 'usap_default'. Half the relationship table was a duplicate,
    # and every traversal paid for it.
    with _import(tmp_path, "single", NESTED) as pkg:
        graphs = [
            row[0]
            for row in pkg.conn.execute(
                "SELECT DISTINCT graph_name FROM usap_city_object_relationship"
            )
        ]

        assert graphs == ["usap_default"]

        # …and the default query needs no graph_name argument to find them.
        assert len(pkg.list_city_objects(descendants_of="b1")) == 4

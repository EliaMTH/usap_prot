"""
End-to-end tests for the three ingestion procedures of INGESTION.md
(designed in DATA_INGESTION_REVAMP.md):

  1. init from 3D assets + CityGML + a linking JSON keyed by gml ids
  2. init from 3D assets + a minimal vocabulary + a minimal linking JSON
     (id + concept + elements) that creates temporary carrier city objects
  3. editing an existing usap with the same formats (update mode)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (
    CITYGML_CONTAINMENT_CONFIG,
    CITYGML_SCHEMA_FIXTURE,
    assert_package_valid,
    seed_citygml_concepts,
)
from conftest import write_tiny_mesh as _write_tiny_mesh

from usap import (
    USAPAmbiguityError,
    USAPError,
    USAPPackage,
    apply_annotation_batch,
    build_project_package_from_file,
)

TINY_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel
    xmlns:core="http://www.opengis.net/citygml/3.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:con="http://www.opengis.net/citygml/construction/3.0"
    xmlns:bldg="http://www.opengis.net/citygml/building/3.0">
  <core:cityObjectMember>
    <bldg:Building gml:id="building_1">
      <core:boundary>
        <con:RoofSurface gml:id="building_1_roof_1"/>
      </core:boundary>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""

MINIMAL_VOCAB = {
    "scheme": "local",
    "concepts": [
        {"local_name": "TempSurface"},
        {"local_name": "TempRoof", "parent_uri": "TempSurface"},
    ],
}


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _procedure_1_files(tmp_path: Path) -> Path:
    """Lay out config + assets + linking JSON for a CityGML-based init."""
    (tmp_path / "city.gml").write_text(TINY_CITYGML, encoding="utf-8")
    _write_tiny_mesh(tmp_path / "city_mesh.ply")

    _write_json(
        tmp_path / "links.json",
        {
            "annotations": [
                {
                    # No concept (inherited from the object's CityGML class)
                    # and no annotation_uid (derived): the linking JSON only
                    # says which object owns which elements.
                    "city_object_uid": "building_1_roof_1",
                    "memberships": [
                        {
                            "asset_uri": "city_mesh",
                            "element_indices": [0, 1],
                        }
                    ],
                }
            ]
        },
    )

    return _write_json(
        tmp_path / "project.json",
        {
            "db_path": "proc1.usap.gpkg",
            "manifest_path": "proc1_manifest.json",
            "citygml_schema": str(CITYGML_SCHEMA_FIXTURE),
            "relationship_types": CITYGML_CONTAINMENT_CONFIG,
            "citygml": {"path": "city.gml"},
            "meshes": [
                {
                    "path": "city_mesh.ply",
                    "uri": "city_mesh",
                    "representation_name": "city_mesh",
                }
            ],
            "annotation_batches": ["links.json"],
        },
    )


def test_procedure_1_citygml_init_is_one_call(tmp_path: Path) -> None:
    config_path = _procedure_1_files(tmp_path)

    result = build_project_package_from_file(config_path)

    assert result.batches[0].annotation_count == 1
    assert result.batches[0].created_city_object_uids == []
    assert result.manifest_path is not None and result.manifest_path.exists()

    with USAPPackage.open(result.db_path) as pkg:
        # Concept inherited from the CityGML object's class, uid derived.
        annotation = pkg.get_annotation(
            annotation_uid="ann_building_1_roof_1_RoofSurface"
        )

        assert annotation is not None
        assert annotation["semantic_class"] == "RoofSurface"
        assert annotation["primary_city_object_uid"] == "building_1_roof_1"

        # Object queries work, including through the decomposition.
        for object_uid in ("building_1_roof_1", "building_1"):
            blocks = pkg.elements_for_city_object(object_uid, expand=True)
            assert [b["elements"] for b in blocks] == [[0, 1]]

        assert_package_valid(pkg)


def _procedure_2_files(tmp_path: Path) -> Path:
    """Config + assets + minimal linking JSON for a no-CityGML init."""
    _write_tiny_mesh(tmp_path / "city_mesh.ply")
    _write_json(tmp_path / "vocab.json", MINIMAL_VOCAB)

    _write_json(
        tmp_path / "links.json",
        {
            "create_missing_city_objects": True,
            "annotations": [
                {
                    # The documented minimum: id + what it is + elements.
                    "city_object_uid": "tower_A_roof",
                    "concept": "TempRoof",
                    "memberships": [
                        {
                            "asset_uri": "city_mesh",
                            "element_indices": [0],
                        }
                    ],
                }
            ],
        },
    )

    return _write_json(
        tmp_path / "project.json",
        {
            "db_path": "proc2.usap.gpkg",
            "manifest_path": "proc2_manifest.json",
            "vocabularies": ["vocab.json"],
            "meshes": [
                {
                    "path": "city_mesh.ply",
                    "uri": "city_mesh",
                    "representation_name": "city_mesh",
                }
            ],
            "annotation_batches": ["links.json"],
        },
    )


def test_procedure_2_minimal_init_is_fully_queryable(tmp_path: Path) -> None:
    config_path = _procedure_2_files(tmp_path)

    result = build_project_package_from_file(config_path)

    assert result.batches[0].created_city_object_uids == ["tower_A_roof"]
    assert result.batches[0].created_city_object_count == 1

    with USAPPackage.open(result.db_path) as pkg:
        # The carrier: classed by "what it is", marked for later alignment.
        carrier = pkg.conn.execute(
            """
            SELECT co.object_status, sc.local_name
            FROM usap_city_object AS co
            JOIN usap_semantic_class AS sc
                ON sc.semantic_class_id = co.semantic_class_id
            WHERE co.object_uid = 'tower_A_roof'
            """
        ).fetchone()

        assert carrier is not None
        assert carrier["object_status"] == "temporary"
        assert carrier["local_name"] == "TempRoof"

        # All query families work on the minimal package:
        blocks = pkg.elements_for_city_object("tower_A_roof", expand=True)
        assert [b["elements"] for b in blocks] == [[0]]

        parent_id = pkg.resolve_semantic_class("TempSurface")
        blocks = pkg.elements_for_semantic_class(
            parent_id, include_subclasses=True, expand=True
        )
        assert [b["elements"] for b in blocks] == [[0]]

        part_id = pkg.resolve_asset_part("city_mesh")
        matches = pkg.annotations_for_elements(part_id, "face", [0])
        assert [m["annotation_uid"] for m in matches] == [
            "ann_tower_A_roof_TempRoof"
        ]

        assert_package_valid(pkg)

    # Manifest lists the carrier like any other object.
    manifest = json.loads(
        (tmp_path / "proc2_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["summary"]["city_objects"] == 1

    # Procedure 3 on the same files: re-running in update mode edits in
    # place (stable derived uid), instead of duplicating or re-creating.
    again = build_project_package_from_file(config_path, update=True)

    assert again.batches[0].created_city_object_uids == []

    with USAPPackage.open(again.db_path) as pkg:
        count = pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_annotation"
        ).fetchone()["n"]

        assert count == 1


def _minimal_pkg(tmp_path: Path) -> USAPPackage:
    pkg = USAPPackage.create(tmp_path / "strict.usap.gpkg", overwrite=True)
    pkg.create_semantic_class(
        scheme="local", class_uri="local:TempRoof", local_name="TempRoof"
    )
    asset_id = pkg.register_asset(uri="city_mesh", asset_kind="mesh")
    pkg.register_asset_part(asset_id, "geometry/0", "face", 10)
    return pkg


def test_unknown_city_object_fails_without_the_flag(tmp_path: Path) -> None:
    with _minimal_pkg(tmp_path) as pkg:
        batch = {
            "annotations": [
                {
                    "city_object_uid": "tower_A_roof",
                    "concept": "TempRoof",
                    "memberships": [
                        {"asset_uri": "city_mesh", "element_indices": [0]}
                    ],
                }
            ]
        }

        # Strict by default: custom names in a package that did not opt in
        # (e.g. one built from CityGML) fail loudly.
        with pytest.raises(USAPError, match="City object not found"):
            apply_annotation_batch(pkg, batch)

        batch["create_missing_city_objects"] = True
        apply_annotation_batch(pkg, batch)

        assert pkg.elements_for_city_object("tower_A_roof", expand=True)


def test_carriers_are_queryable_in_every_graph(tmp_path: Path) -> None:
    # A CityGML import creates a second named graph (citygml_import); a
    # carrier created afterwards has no edges in any graph, and an edgeless
    # object is the case that used to disappear from the default
    # include_descendants query. It must answer for itself in EVERY graph,
    # named or not, since nothing about it is graph-specific.
    from usap import apply_annotation_batch as apply_batch
    from usap import import_citygml_semantics

    (tmp_path / "city.gml").write_text(TINY_CITYGML, encoding="utf-8")

    with _minimal_pkg(tmp_path) as pkg:
        seed_citygml_concepts(pkg)
        import_citygml_semantics(pkg, tmp_path / "city.gml")

        apply_batch(
            pkg,
            {
                "create_missing_city_objects": True,
                "annotations": [
                    {
                        "city_object_uid": "tower_A_roof",
                        "concept": "TempRoof",
                        "memberships": [
                            {"asset_uri": "city_mesh", "element_indices": [0]}
                        ],
                    }
                ],
            },
        )

        report = pkg.validate_report()
        assert report.is_ok, [i.format() for i in report.issues]

        for graph_name in ["usap_default", "citygml_import", "never_used"]:
            blocks = pkg.elements_for_city_object(
                "tower_A_roof",
                graph_name=graph_name,
                expand=True,
            )

            assert [b["elements"] for b in blocks] == [[0]], graph_name

        assert pkg.list_city_objects(descendants_of="tower_A_roof") != []


def test_new_carrier_requires_a_concept(tmp_path: Path) -> None:
    with _minimal_pkg(tmp_path) as pkg:
        batch = {
            "create_missing_city_objects": True,
            "annotations": [
                {
                    "city_object_uid": "tower_A_roof",
                    "memberships": [
                        {"asset_uri": "city_mesh", "element_indices": [0]}
                    ],
                }
            ],
        }

        with pytest.raises(ValueError, match="needs 'concept'"):
            apply_annotation_batch(pkg, batch)


def test_part_reference_strictness(tmp_path: Path) -> None:
    with _minimal_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="two_parts", asset_kind="mesh")
        pkg.register_asset_part(asset_id, "geometry/0", "face", 5)
        pkg.register_asset_part(asset_id, "geometry/1", "face", 5)

        def batch_with(membership: dict) -> dict:
            return {
                "create_missing_city_objects": True,
                "annotations": [
                    {
                        "city_object_uid": "obj_x",
                        "concept": "TempRoof",
                        "memberships": [membership],
                    }
                ],
            }

        with pytest.raises(USAPAmbiguityError, match="part_path"):
            apply_annotation_batch(
                pkg,
                batch_with({"asset_uri": "two_parts", "element_indices": [0]}),
            )

        with pytest.raises(ValueError, match="exactly one"):
            apply_annotation_batch(
                pkg,
                batch_with(
                    {
                        "asset_part_id": 1,
                        "asset_uri": "city_mesh",
                        "element_indices": [0],
                    }
                ),
            )

        with pytest.raises(USAPError, match="kind mismatch"):
            apply_annotation_batch(
                pkg,
                batch_with(
                    {
                        "asset_uri": "city_mesh",
                        "element_kind": "point",
                        "element_indices": [0],
                    }
                ),
            )

        # part_path disambiguates; element_kind defaults to the part's kind.
        apply_annotation_batch(
            pkg,
            batch_with(
                {
                    "asset_uri": "two_parts",
                    "part_path": "geometry/1",
                    "element_indices": [0, 4],
                }
            ),
        )

        blocks = pkg.elements_for_city_object("obj_x", expand=True)
        assert [b["elements"] for b in blocks] == [[0, 4]]


def test_procedure_3_update_adds_assets_and_edits(tmp_path: Path) -> None:
    config_path = _procedure_1_files(tmp_path)
    build_project_package_from_file(config_path)

    # Second config against the same db: a new asset + an edit of the same
    # (derived-uid) annotation shrinking its membership.
    _write_tiny_mesh(tmp_path / "extra_mesh.ply")

    _write_json(
        tmp_path / "edit.json",
        {
            "annotations": [
                {
                    "city_object_uid": "building_1_roof_1",
                    "memberships": [
                        {"asset_uri": "city_mesh", "element_indices": [0]}
                    ],
                }
            ]
        },
    )

    update_config = _write_json(
        tmp_path / "update.json",
        {
            "db_path": "proc1.usap.gpkg",
            "meshes": [
                {
                    "path": "extra_mesh.ply",
                    "uri": "extra_mesh",
                    "representation_name": "extra_mesh",
                }
            ],
            "annotation_batches": ["edit.json"],
        },
    )

    result = build_project_package_from_file(update_config, update=True)

    with USAPPackage.open(result.db_path) as pkg:
        # New asset present, old data intact, membership replaced.
        assert pkg.resolve_asset_part("extra_mesh") > 0

        blocks = pkg.elements_for_city_object("building_1_roof_1", expand=True)
        assert [b["elements"] for b in blocks] == [[0]]

        assert pkg.get_annotation(
            annotation_uid="ann_building_1_roof_1_RoofSurface"
        ) is not None

        assert_package_valid(pkg)


def test_update_rerun_does_not_duplicate_relationships(tmp_path: Path) -> None:
    # A config with a CityGML section re-imports the same edges on every
    # update run; link_city_objects must dedup identical edges or each
    # re-run silently doubles the relationship graph (and its usap_default
    # mirror), bloating the file and breaking the idempotency contract.
    config_path = _procedure_1_files(tmp_path)

    def _relationship_count(db_path: Path) -> int:
        with USAPPackage.open(db_path) as pkg:
            rel = pkg.conn.execute(
                "SELECT COUNT(*) AS n FROM usap_city_object_relationship"
            ).fetchone()["n"]
        return rel

    result = build_project_package_from_file(config_path)
    before = _relationship_count(result.db_path)

    assert before > 0

    again = build_project_package_from_file(config_path, update=True)

    assert _relationship_count(again.db_path) == before

    with USAPPackage.open(again.db_path) as pkg:
        report = pkg.validate_report()

        assert "DUPLICATE_RELATIONSHIP_EDGE" not in {
            issue.code for issue in report.issues
        }
        assert_package_valid(pkg)


def test_update_mode_requires_an_existing_package(tmp_path: Path) -> None:
    config = _write_json(
        tmp_path / "missing.json",
        {"db_path": "nope.usap.gpkg"},
    )

    with pytest.raises(USAPError, match="does not exist"):
        build_project_package_from_file(config, update=True)

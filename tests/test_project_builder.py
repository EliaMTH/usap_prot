from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import CITYGML_SCHEMA_FIXTURE
from conftest import write_tiny_las as _write_tiny_las, write_tiny_mesh as _write_tiny_mesh
from usap import (
    USAPError,
    USAPPackage,
    build_project_package,
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
      <core:boundary>
        <con:WallSurface gml:id="building_1_wall_1">
          <con:fillingSurface>
            <con:WindowSurface gml:id="building_1_window_1"/>
          </con:fillingSurface>
        </con:WallSurface>
      </core:boundary>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""


def test_build_project_package_from_config(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "outputs"

    data_dir.mkdir()
    output_dir.mkdir()

    citygml_path = data_dir / "tiny_city.gml"
    las_path = data_dir / "tiny.las"
    lod2_path = data_dir / "lod2.ply"
    config_path = tmp_path / "project.json"

    db_path = output_dir / "project.usap.gpkg"
    manifest_path = output_dir / "project_manifest.json"

    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")
    _write_tiny_las(las_path)
    _write_tiny_mesh(lod2_path)

    config = {
        "db_path": str(db_path),
        "manifest_path": str(manifest_path),
        # No "schema_path": that one does default to the shipped file, being
        # the database schema. "vocabularies" has no default -- concepts come
        # only from what a config names, here the CityGML XSDs.
        "citygml_schema": str(CITYGML_SCHEMA_FIXTURE),
        "citygml": {
            "path": str(citygml_path),
        },
        "las": [
            {
                "path": str(las_path)
            }
        ],
        "meshes": [
            {
                "path": str(lod2_path),
                "representation_name": "buildings_lod2",
                "representation_kind": "building_mesh",
                "lod": "LoD2"
            }
        ]
    }

    config_path.write_text(
        json.dumps(config),
        encoding="utf-8",
    )

    result = build_project_package_from_file(config_path)

    assert result.db_path == db_path
    assert result.manifest_path == manifest_path
    assert result.citygml is not None
    assert result.citygml.object_count == 4
    assert len(result.las_assets) == 1
    assert len(result.mesh_assets) == 1
    assert result.accepted_concept_count > 0

    assert db_path.exists()
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["summary"]["las_assets"] == 1
    assert manifest["summary"]["mesh_assets"] == 1
    assert manifest["citygml"]["object_count"] == 4

    assert manifest["las"][0]["asset_part_id"] > 0
    assert manifest["las"][0]["element_kind"] == "point"

    assert manifest["meshes"][0]["parts"][0]["asset_part_id"] > 0
    assert manifest["meshes"][0]["parts"][0]["element_kind"] == "face"

    city_object_uids = {
        item["object_uid"]
        for item in manifest["city_objects_sample"]
    }

    assert "building_1" in city_object_uids
    assert "building_1_roof_1" in city_object_uids

def test_build_project_package_from_dict(tmp_path: Path) -> None:
    # The dict entry point (for pipelines that assemble configs in code)
    # must resolve relative config paths against base_dir, not the CWD.
    _write_tiny_mesh(tmp_path / "lod2.ply")

    result = build_project_package(
        {
            "db_path": "dict.usap.gpkg",
            "meshes": [
                {
                    "path": "lod2.ply",
                    "uri": "lod2",
                    "representation_name": "buildings_lod2",
                }
            ],
        },
        base_dir=tmp_path,
    )

    assert result.db_path == tmp_path / "dict.usap.gpkg"
    assert result.db_path.exists()
    assert len(result.mesh_assets) == 1


def test_failed_build_leaves_no_package(tmp_path: Path) -> None:
    # The probe that motivated this: a build seeded the default concepts,
    # then failed on a missing mesh, and left a package on disk reporting
    # {"assets": 0, "concepts": 3}. That file looks like a real package to
    # everything downstream, so a failed build must leave nothing at all.
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "outputs"

    data_dir.mkdir()
    output_dir.mkdir()

    citygml_path = data_dir / "tiny_city.gml"
    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")

    db_path = output_dir / "partial.usap.gpkg"

    config = {
        "db_path": str(db_path),
        "citygml_schema": str(CITYGML_SCHEMA_FIXTURE),
        "citygml": {"path": str(citygml_path)},
        "meshes": [
            {
                "path": str(data_dir / "does_not_exist.ply"),
                "representation_name": "buildings_lod2",
            }
        ],
    }

    with pytest.raises(FileNotFoundError):
        build_project_package(config, base_dir=tmp_path)

    assert not db_path.exists()


def test_failed_update_leaves_the_previous_package_intact(tmp_path: Path) -> None:
    # update=True edits a package someone already has. A failure part-way
    # must not be able to half-modify it: the file on disk stays exactly the
    # valid package it was before the run.
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "outputs"

    data_dir.mkdir()
    output_dir.mkdir()

    citygml_path = data_dir / "tiny_city.gml"
    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")

    las_path = data_dir / "tiny.las"
    _write_tiny_las(las_path)

    db_path = output_dir / "updatable.usap.gpkg"

    config = {
        "db_path": str(db_path),
        "citygml_schema": str(CITYGML_SCHEMA_FIXTURE),
        "citygml": {"path": str(citygml_path)},
        "las": [{"path": str(las_path)}],
    }

    result = build_project_package(config, base_dir=tmp_path)

    with USAPPackage.open(result.db_path) as pkg:
        before = (
            len(pkg.list_assets()),
            len(pkg.list_city_objects()),
            len(pkg.list_accepted_concepts()),
        )

    broken = dict(config)
    broken["meshes"] = [
        {
            "path": str(data_dir / "missing.ply"),
            "representation_name": "buildings_lod2",
        }
    ]

    with pytest.raises(FileNotFoundError):
        build_project_package(broken, base_dir=tmp_path, update=True)

    with USAPPackage.open(db_path) as pkg:
        after = (
            len(pkg.list_assets()),
            len(pkg.list_city_objects()),
            len(pkg.list_accepted_concepts()),
        )

        assert after == before
        assert pkg.validate_report().is_ok


def test_removed_mirror_key_is_refused_not_ignored(tmp_path: Path) -> None:
    # 'also_usap_default' switched off a mirror that no longer exists: the
    # import writes one graph now. Ignoring the key would leave the config
    # file asserting behaviour the build does not perform.
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    citygml_path = data_dir / "tiny_city.gml"
    citygml_path.write_text(TINY_CITYGML, encoding="utf-8")

    config = {
        "db_path": str(tmp_path / "mirror.usap.gpkg"),
        "citygml_schema": str(CITYGML_SCHEMA_FIXTURE),
        "citygml": {"path": str(citygml_path), "also_usap_default": True},
    }

    with pytest.raises(USAPError, match="also_usap_default"):
        build_project_package(config, base_dir=tmp_path)


def test_a_config_naming_no_vocabulary_seeds_no_concepts(tmp_path: Path) -> None:
    # A package starts with zero concepts and USAP asserts no taxonomy of its
    # own -- so a build must load exactly what the config named and nothing
    # else. 'vocabularies' used to default to the ADE registry shipped inside
    # the package, quietly seeding 15 concepts no config asked for, which made
    # the config describe less than the package contained.
    _write_tiny_mesh(tmp_path / "lod2.ply")

    build_project_package(
        {
            "db_path": "empty.usap.gpkg",
            "meshes": [{"path": "lod2.ply", "representation_name": "m"}],
        },
        base_dir=tmp_path,
    )

    with USAPPackage.open(tmp_path / "empty.usap.gpkg") as pkg:
        assert pkg.list_accepted_concepts() == []

        # And the consequence is loud rather than silent: annotating against a
        # concept nothing registered raises.
        with pytest.raises(USAPError):
            pkg.resolve_semantic_class("EnergyRoof")


def test_vocabulary_folder_matches_the_key_by_key_form(tmp_path: Path) -> None:
    # 'vocabulary_folder' is the application startup path (US-DATA-04): one
    # directory, every source in it, dispatched by suffix. It has to seed the
    # same package as naming those sources one key at a time, or the config
    # path and the app path would drift.
    folder = tmp_path / "vocabulary"
    folder.mkdir()

    for xsd in CITYGML_SCHEMA_FIXTURE.rglob("*.xsd"):
        (folder / xsd.name).write_bytes(xsd.read_bytes())

    (folder / "local.json").write_text(
        json.dumps({"scheme": "local", "concepts": [{"local_name": "SolarPanel"}]}),
        encoding="utf-8",
    )

    build_project_package(
        {"db_path": str(tmp_path / "folder.usap.gpkg"),
         "vocabulary_folder": str(folder)},
        base_dir=tmp_path,
    )
    build_project_package(
        {"db_path": str(tmp_path / "keys.usap.gpkg"),
         "citygml_schema": str(CITYGML_SCHEMA_FIXTURE),
         "vocabularies": [str(folder / "local.json")]},
        base_dir=tmp_path,
    )

    def names(db: Path) -> set[str]:
        with USAPPackage.open(db) as pkg:
            return {c["local_name"] for c in pkg.list_accepted_concepts()}

    from_folder = names(tmp_path / "folder.usap.gpkg")

    assert from_folder == names(tmp_path / "keys.usap.gpkg")
    assert {"Building", "SolarPanel"} <= from_folder


def test_unrecognised_config_keys_are_refused(tmp_path: Path) -> None:
    # The failure this exists for: 'annotation_batch' for 'annotation_batches'
    # built a package with zero annotations, exit code 0, and a clean
    # validation report. Nothing reads an unknown key, so nothing could say the
    # intent had been dropped.
    _write_tiny_mesh(tmp_path / "lod2.ply")

    base = {
        "db_path": str(tmp_path / "typo.usap.gpkg"),
        "meshes": [{"path": "lod2.ply", "representation_name": "m"}],
    }

    with pytest.raises(USAPError, match="annotation_batch"):
        build_project_package(
            {**base, "annotation_batch": ["x.json"]}, base_dir=tmp_path)

    # Nested blocks too -- a misspelled compute_hash silently left an asset
    # unhashed, which trades away change detection without saying so.
    with pytest.raises(USAPError, match="compute_hashh"):
        build_project_package(
            {**base,
             "meshes": [{"path": "lod2.ply", "representation_name": "m",
                         "compute_hashh": False}]},
            base_dir=tmp_path,
        )

    # '_'-prefixed keys are comments: JSON has nowhere else to put them.
    build_project_package(
        {**base, "_comment": "why this config exists"}, base_dir=tmp_path)

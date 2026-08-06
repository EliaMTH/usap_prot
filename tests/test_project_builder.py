from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_tiny_las as _write_tiny_las, write_tiny_mesh as _write_tiny_mesh
from usap import (
    USAPPackage,
    build_project_package,
    build_project_package_from_file,
)


TINY_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel
    xmlns:core="http://www.opengis.net/citygml/2.0"
    xmlns:gml="http://www.opengis.net/gml"
    xmlns:bldg="http://www.opengis.net/citygml/building/2.0">
  <core:cityObjectMember>
    <bldg:Building gml:id="building_1">
      <bldg:boundedBy>
        <bldg:RoofSurface gml:id="building_1_roof_1"/>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="building_1_wall_1">
          <bldg:opening>
            <bldg:Window gml:id="building_1_window_1"/>
          </bldg:opening>
        </bldg:WallSurface>
      </bldg:boundedBy>
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
        # No "schema_path"/"vocabularies": both default to the files shipped
        # inside the package, which is what a normal install has.
        "citygml": {
            "path": str(citygml_path),
            "graph_name": "citygml_import",
            "also_usap_default": True
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

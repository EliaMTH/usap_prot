from __future__ import annotations

import json
from pathlib import Path

from conftest import write_tiny_las as _write_tiny_las, write_tiny_mesh as _write_tiny_mesh
from usap import build_project_package, build_project_package_from_file


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
        "schema_path": "sql/schema.sql",
        "vocabularies": [
            "vocabularies/citygml_3_0_mvp.json",
            "vocabularies/usap_ade_prototype.json"
        ],
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

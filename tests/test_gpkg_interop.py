"""
GeoPackage interoperability tests.

Step 1 — browsable: curated attribute views registered in gpkg_contents.
Step 2 — mappable: one derived 2D extent box per asset (features layer),
plus the SRS plumbing (config srs_id, LAS EPSG sniffing, blob re-encoding).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import assert_package_valid
from conftest import write_tiny_las as _write_tiny_las
from conftest import write_tiny_mesh as _write_tiny_mesh

from usap import (
    USAPPackage,
    apply_annotation_batch,
    build_project_package_from_file,
    epsg_from_wkt,
    register_las_asset,
    register_mesh_asset,
)
from usap.geopackage import (
    USAP_ATTRIBUTE_LAYERS,
    USAP_FEATURES_LAYER,
    decode_gpkg_envelope,
    encode_gpkg_bbox_polygon,
    set_package_srs,
)

SCHEMA_PATH = Path("sql/schema.sql").resolve()

WKT1_25833 = (
    'PROJCS["ETRS89 / UTM zone 33N",'
    'GEOGCS["ETRS89",AUTHORITY["EPSG","4258"]],'
    'AUTHORITY["EPSG","25833"]]'
)
WKT2_25833 = 'PROJCRS["ETRS89 / UTM zone 33N",ID["EPSG",25833]]'


def _make_pkg(tmp_path: Path, name: str = "pkg.usap.gpkg") -> USAPPackage:
    return USAPPackage.create(
        tmp_path / name,
        schema_path=SCHEMA_PATH,
        overwrite=True,
    )


# ---------------------------------------------------------------------------
# Step 1 — browsable
# ---------------------------------------------------------------------------

def test_attribute_layers_are_registered(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        rows = {
            row["table_name"]: row["data_type"]
            for row in pkg.conn.execute(
                "SELECT table_name, data_type FROM gpkg_contents"
            ).fetchall()
        }

        for view_name, _identifier, _description in USAP_ATTRIBUTE_LAYERS:
            assert rows.get(view_name) == "attributes"

        assert rows.get(USAP_FEATURES_LAYER) == "features"


def test_annotations_view_is_readable(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        pkg.create_semantic_class(
            scheme="s", class_uri="s:Roof", local_name="Roof"
        )
        roof_id = pkg.create_city_object(object_uid="b1_roof")
        asset_id = pkg.register_asset(uri="mesh", asset_kind="mesh")
        part = pkg.register_asset_part(asset_id, "g/0", "face", 10)

        pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_view_check",
            asset_part_id=part,
            element_kind="face",
            element_indices=[1, 2, 3],
            city_object_id=roof_id,
        )

        row = pkg.conn.execute(
            "SELECT * FROM usap_annotations_view WHERE annotation_uid = ?",
            ("ann_view_check",),
        ).fetchone()

        assert row is not None
        assert row["concept"] == "Roof"
        assert row["city_object_uid"] == "b1_roof"
        assert row["selected_element_count"] == 3
        assert row["value_field_count"] == 0

        concept_row = pkg.conn.execute(
            "SELECT * FROM usap_concepts_view WHERE local_name = 'Roof'"
        ).fetchone()

        assert concept_row["annotation_count"] == 1
        assert concept_row["in_use"] == 1


def test_city_objects_view_shows_temporary_carriers(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        pkg.create_semantic_class(
            scheme="local", class_uri="local:TempRoof", local_name="TempRoof"
        )
        asset_id = pkg.register_asset(uri="mesh", asset_kind="mesh")
        pkg.register_asset_part(asset_id, "g/0", "face", 10)

        apply_annotation_batch(
            pkg,
            {
                "create_missing_city_objects": True,
                "annotations": [
                    {
                        "city_object_uid": "tower_A_roof",
                        "concept": "TempRoof",
                        "memberships": [
                            {"asset_uri": "mesh", "element_indices": [0]}
                        ],
                    }
                ],
            },
        )

        row = pkg.conn.execute(
            "SELECT * FROM usap_city_objects_view WHERE object_uid = ?",
            ("tower_A_roof",),
        ).fetchone()

        assert row is not None
        assert row["semantic_class"] == "TempRoof"
        assert row["object_status"] == "temporary"


# ---------------------------------------------------------------------------
# Step 2 — mappable
# ---------------------------------------------------------------------------

def test_asset_extent_is_union_of_part_bounds(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="scene", asset_kind="mesh")

        pkg.register_asset_part(
            asset_id, "g/0", "face", 10,
            minx=0.0, miny=1.0, minz=0.0, maxx=4.0, maxy=5.0, maxz=2.0,
        )
        pkg.register_asset_part(
            asset_id, "g/1", "face", 5,
            minx=-2.0, miny=0.0, minz=0.0, maxx=1.0, maxy=9.0, maxz=1.0,
        )

        rows = pkg.conn.execute(
            "SELECT asset_id, geom FROM usap_asset_extent"
        ).fetchall()

        assert len(rows) == 1  # one box per asset, not per part

        blob = rows[0]["geom"]
        assert blob[:2] == b"GP"

        envelope = decode_gpkg_envelope(blob)
        assert (envelope["minx"], envelope["miny"]) == (-2.0, 0.0)
        assert (envelope["maxx"], envelope["maxy"]) == (4.0, 9.0)
        assert envelope["srs_id"] == -1  # undeclared CRS

        view_row = pkg.conn.execute(
            "SELECT * FROM usap_asset_extents WHERE fid = ?",
            (asset_id,),
        ).fetchone()

        assert view_row["uri"] == "scene"
        assert view_row["part_count"] == 2
        assert view_row["element_count"] == 15

        assert_package_valid(pkg)


def test_part_without_bounds_gets_no_extent(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="mesh", asset_kind="mesh")
        pkg.register_asset_part(asset_id, "g/0", "face", 10)

        count = pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_asset_extent"
        ).fetchone()["n"]

        assert count == 0
        # No extent for an unbounded asset is fine — not even a warning.
        assert pkg.validate_report().issues == []


def test_adapters_produce_extents(tmp_path: Path) -> None:
    las_path = tmp_path / "tiny.las"
    mesh_path = tmp_path / "tiny.ply"
    _write_tiny_las(las_path)
    _write_tiny_mesh(mesh_path)

    with _make_pkg(tmp_path) as pkg:
        las = register_las_asset(pkg, las_path)
        mesh = register_mesh_asset(
            pkg, mesh_path, representation_name="tiny"
        )

        extents = {
            int(row["asset_id"]): decode_gpkg_envelope(row["geom"])
            for row in pkg.conn.execute(
                "SELECT asset_id, geom FROM usap_asset_extent"
            ).fetchall()
        }

        assert set(extents) == {las.asset_id, mesh.asset_id}
        assert extents[las.asset_id]["minx"] == las.minx
        assert extents[las.asset_id]["maxy"] == las.maxy

        assert_package_valid(pkg)


def test_asset_delete_cascades_extent(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="mesh", asset_kind="mesh")
        pkg.register_asset_part(
            asset_id, "g/0", "face", 10,
            minx=0.0, miny=0.0, minz=0.0, maxx=1.0, maxy=1.0, maxz=1.0,
        )

        with pkg.transaction():
            pkg.conn.execute(
                "DELETE FROM usap_asset WHERE asset_id = ?", (asset_id,)
            )

        count = pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_asset_extent"
        ).fetchone()["n"]

        assert count == 0
        assert_package_valid(pkg)


def test_tampered_extent_fails_validation(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="mesh", asset_kind="mesh")
        pkg.register_asset_part(
            asset_id, "g/0", "face", 10,
            minx=0.0, miny=0.0, minz=0.0, maxx=1.0, maxy=1.0, maxz=1.0,
        )

        with pkg.transaction():
            pkg.conn.execute(
                "UPDATE usap_asset_extent SET geom = ? WHERE asset_id = ?",
                (
                    encode_gpkg_bbox_polygon(0.0, 0.0, 99.0, 99.0, -1),
                    asset_id,
                ),
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "EXTENT_ENVELOPE_MISMATCH" in {i.code for i in report.errors}

        with pkg.transaction():
            pkg.conn.execute(
                "UPDATE usap_asset_extent SET geom = X'00' WHERE asset_id = ?",
                (asset_id,),
            )

        report = pkg.validate_report()
        assert "CORRUPT_EXTENT_BLOB" in {i.code for i in report.errors}


# ---------------------------------------------------------------------------
# SRS
# ---------------------------------------------------------------------------

def test_epsg_from_wkt() -> None:
    assert epsg_from_wkt(WKT1_25833) == 25833  # last AUTHORITY wins
    assert epsg_from_wkt(WKT2_25833) == 25833
    assert epsg_from_wkt('LOCAL_CS["unnamed"]') is None
    assert epsg_from_wkt(None) is None
    assert epsg_from_wkt("") is None


def test_set_package_srs_updates_layer_and_blobs(tmp_path: Path) -> None:
    with _make_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="mesh", asset_kind="mesh")
        pkg.register_asset_part(
            asset_id, "g/0", "face", 10,
            minx=0.0, miny=0.0, minz=0.0, maxx=1.0, maxy=1.0, maxz=1.0,
        )

        # Declared after registration: the existing blob must be re-encoded.
        with pkg.transaction():
            set_package_srs(pkg.conn, 25833, definition_wkt=WKT1_25833)

        for table in ("gpkg_contents", "gpkg_geometry_columns"):
            srs = pkg.conn.execute(
                f"SELECT srs_id FROM {table} WHERE table_name = ?",
                (USAP_FEATURES_LAYER,),
            ).fetchone()["srs_id"]
            assert srs == 25833

        blob = pkg.conn.execute(
            "SELECT geom FROM usap_asset_extent WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()["geom"]

        envelope = decode_gpkg_envelope(blob)
        assert envelope["srs_id"] == 25833
        assert envelope["maxx"] == 1.0  # coordinates untouched

        srs_row = pkg.conn.execute(
            "SELECT definition FROM gpkg_spatial_ref_sys WHERE srs_id = 25833"
        ).fetchone()
        assert srs_row is not None
        assert "25833" in srs_row["definition"]

        assert_package_valid(pkg)


def test_builder_config_srs_and_las_sniffing(tmp_path: Path, monkeypatch) -> None:
    _write_tiny_las(tmp_path / "tiny.las")

    config_path = tmp_path / "project.json"
    config_path.write_text(
        json.dumps(
            {
                "db_path": "srs.usap.gpkg",
                "las": [{"path": "tiny.las", "compute_hash": False}],
                "srs_id": 3857,
            }
        ),
        encoding="utf-8",
    )

    result = build_project_package_from_file(config_path)

    with USAPPackage.open(result.db_path) as pkg:
        srs = pkg.conn.execute(
            "SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?",
            (USAP_FEATURES_LAYER,),
        ).fetchone()["srs_id"]
        assert srs == 3857  # config wins

        blob = pkg.conn.execute(
            "SELECT geom FROM usap_asset_extent"
        ).fetchone()["geom"]
        assert decode_gpkg_envelope(blob)["srs_id"] == 3857

    # Without a config key, a CRS sniffed from the LAS WKT is promoted.
    monkeypatch.setattr(
        "usap.adapters.las_adapter._try_read_crs_wkt",
        lambda header: WKT1_25833,
    )

    config_path.write_text(
        json.dumps(
            {
                "db_path": "sniffed.usap.gpkg",
                "las": [{"path": "tiny.las", "compute_hash": False}],
            }
        ),
        encoding="utf-8",
    )

    result = build_project_package_from_file(config_path)

    with USAPPackage.open(result.db_path) as pkg:
        srs = pkg.conn.execute(
            "SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?",
            (USAP_FEATURES_LAYER,),
        ).fetchone()["srs_id"]
        assert srs == 25833

        las_asset_srs = pkg.conn.execute(
            "SELECT srs_id FROM usap_asset WHERE asset_kind = 'pointcloud'"
        ).fetchone()["srs_id"]
        assert las_asset_srs == 25833

        assert_package_valid(pkg)

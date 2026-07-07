from __future__ import annotations

from pathlib import Path

from usap import (
    GPKG_APPLICATION_ID,
    GPKG_USER_VERSION,
    USAP_EXTENSION_NAME,
    USAPPackage,
    read_geopackage_header,
)


def test_created_package_is_a_geopackage(tmp_path: Path) -> None:
    """
    One fresh package, all static GeoPackage-identity facts: header pragmas,
    core tables, default SRS rows, and usap_core extension registration.
    (Validation of these is exercised throughout the suite via
    validate_report; the GIS layers have their own tests in
    test_gpkg_interop.py.)
    """
    db_path = tmp_path / "test.usap.gpkg"

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        header = read_geopackage_header(pkg.conn)

        assert header["application_id"] == GPKG_APPLICATION_ID
        assert header["user_version"] == GPKG_USER_VERSION

        tables = {
            row["name"]
            for row in pkg.conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

        assert "gpkg_spatial_ref_sys" in tables
        assert "gpkg_contents" in tables
        assert "gpkg_extensions" in tables
        assert "gpkg_geometry_columns" in tables

        srs_ids = {
            int(row["srs_id"])
            for row in pkg.conn.execute(
                """
                SELECT srs_id
                FROM gpkg_spatial_ref_sys
                """
            ).fetchall()
        }

        assert {-1, 0, 4326} <= srs_ids

        rows = pkg.conn.execute(
            """
            SELECT table_name, extension_name, scope
            FROM gpkg_extensions
            WHERE extension_name = ?
            """,
            (USAP_EXTENSION_NAME,),
        ).fetchall()

        table_names = {row["table_name"] for row in rows}

        assert "usap_profile" in table_names
        assert "usap_asset" in table_names
        assert "usap_membership_block" in table_names
        assert "usap_asset_extent" in table_names

        for row in rows:
            assert row["scope"] == "read-write"

        report = pkg.validate_report()
        assert report.is_ok, [issue.format() for issue in report.issues]
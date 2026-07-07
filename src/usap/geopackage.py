from __future__ import annotations

import sqlite3


GPKG_APPLICATION_ID = 1196444487
GPKG_USER_VERSION = 10300

USAP_EXTENSION_NAME = "usap_core"
USAP_EXTENSION_SCOPE = "read-write"
USAP_EXTENSION_DEFINITION = (
    "USAP Urban Semantic Annotation Package profile. "
    "This extension registers USAP semantic annotation tables."
)

USAP_EXTENSION_TABLES = [
    "usap_profile",
    "usap_asset",
    "usap_asset_part",
    "usap_semantic_class",
    "usap_semantic_class_closure",
    "usap_city_object",
    "usap_city_object_relationship",
    "usap_city_object_closure",
    "usap_annotation",
    "usap_annotation_object",
    "usap_membership_block",
    "usap_value_block",
    "usap_edit_log",
]


def initialize_geopackage_metadata(
    conn: sqlite3.Connection,
    profile_version: str,
) -> None:
    """
    Initialize minimal GeoPackage metadata for a USAP package.

    This does three things:

    1. Sets SQLite header pragmas so the file identifies as GeoPackage-like.
    2. Inserts required/default spatial reference rows.
    3. Registers USAP tables as a package extension in gpkg_extensions.

    It intentionally does not register USAP tables as gpkg_contents rows,
    because they are not normal GeoPackage feature, tile, or attribute layers.
    """
    conn.execute(f"PRAGMA application_id = {GPKG_APPLICATION_ID}")
    conn.execute(f"PRAGMA user_version = {GPKG_USER_VERSION}")

    _insert_default_srs_rows(conn)
    _register_usap_extension(conn, profile_version=profile_version)


def _insert_default_srs_rows(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO gpkg_spatial_ref_sys (
            srs_name,
            srs_id,
            organization,
            organization_coordsys_id,
            definition,
            description
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "Undefined Cartesian SRS",
                -1,
                "NONE",
                -1,
                "undefined",
                "Undefined Cartesian coordinate reference system",
            ),
            (
                "Undefined Geographic SRS",
                0,
                "NONE",
                0,
                "undefined",
                "Undefined geographic coordinate reference system",
            ),
            (
                "WGS 84 geodetic",
                4326,
                "EPSG",
                4326,
                (
                    'GEOGCS["WGS 84",'
                    'DATUM["WGS_1984",'
                    'SPHEROID["WGS 84",6378137,298.257223563]],'
                    'PRIMEM["Greenwich",0],'
                    'UNIT["degree",0.0174532925199433],'
                    'AXIS["Latitude",NORTH],'
                    'AXIS["Longitude",EAST],'
                    'AUTHORITY["EPSG","4326"]]'
                ),
                "longitude/latitude coordinates in decimal degrees on WGS 84",
            ),
        ],
    )


def _register_usap_extension(
    conn: sqlite3.Connection,
    profile_version: str,
) -> None:
    definition = f"{USAP_EXTENSION_DEFINITION} Version: {profile_version}"

    rows = [
        (
            table_name,
            None,
            USAP_EXTENSION_NAME,
            definition,
            USAP_EXTENSION_SCOPE,
        )
        for table_name in USAP_EXTENSION_TABLES
    ]

    conn.executemany(
        """
        INSERT OR IGNORE INTO gpkg_extensions (
            table_name,
            column_name,
            extension_name,
            definition,
            scope
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )


def read_geopackage_header(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Return SQLite header metadata relevant to GeoPackage identification.
    """
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]

    return {
        "application_id": int(application_id),
        "user_version": int(user_version),
    }
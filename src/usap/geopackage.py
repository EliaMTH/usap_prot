from __future__ import annotations

import re
import sqlite3
import struct
from typing import Any

from .errors import USAPError


GPKG_APPLICATION_ID = 1196444487
GPKG_USER_VERSION = 10300

USAP_EXTENSION_NAME = "usap_core"
USAP_EXTENSION_SCOPE = "read-write"

# The GeoPackage standard requires gpkg_extensions.definition to hold a
# permalink, URI, or reference to the document defining the extension — not a
# prose description, which is what used to be written here.
#
# TODO: replace with the real permalink once the extension document has a
# public home; usap.invalid is RFC 2606's reserved TLD, so this can never
# accidentally resolve to someone else's page and reads unmistakably as a
# placeholder.
USAP_EXTENSION_DEFINITION_BASE = "https://usap.invalid/extensions/usap_core"


def usap_extension_definition(profile_version: str) -> str:
    """
    Return the gpkg_extensions.definition URI for a profile version.

    The version rides in the URI path, so it has to be built from the version
    actually being written rather than hardcoded — otherwise every package
    claims to conform to whichever version happened to be current when the
    constant was last edited.
    """
    return f"{USAP_EXTENSION_DEFINITION_BASE}/{profile_version}"

USAP_EXTENSION_TABLES = [
    "usap_profile",
    "usap_asset",
    "usap_asset_part",
    "usap_asset_extent",
    "usap_semantic_class",
    "usap_semantic_class_closure",
    "usap_city_object",
    "usap_relationship_type",
    "usap_city_object_relationship",
    "usap_annotation",
    "usap_annotation_object",
    "usap_assessment",
    "usap_membership_block",
    "usap_value_block",
    "usap_edit_log",
]

# GIS-facing layers: curated views registered in gpkg_contents so QGIS/GDAL
# can browse USAP content. Attribute layers are non-spatial tables; the
# features layer draws one derived 2D box per asset.
USAP_ATTRIBUTE_LAYERS = [
    ("usap_annotations_view", "USAP annotations",
     "Annotations with concept, city object, and element counts."),
    ("usap_assessments_view", "USAP assessments",
     "Dated evaluations of each annotation, per 3D asset."),
    ("usap_concepts_view", "USAP concepts",
     "Accepted concept registry with usage counts."),
    ("usap_city_objects_view", "USAP city objects",
     "City objects with class, status, and source asset."),
]

USAP_FEATURES_LAYER = "usap_asset_extents"

# Layer CRS until one is declared (set_package_srs / config "srs_id"):
# undefined Cartesian — honest for local-coordinate meshes.
DEFAULT_EXTENT_SRS_ID = -1


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
    4. Registers the GIS-facing views in gpkg_contents: four 'attributes'
       layers plus the derived asset-extent 'features' layer, so generic
       GIS tools (QGIS/GDAL) can browse and map the package.

    Raw USAP tables (blocks, closures, edit log) are deliberately not
    registered as layers — the curated views are the GIS surface.
    """
    conn.execute(f"PRAGMA application_id = {GPKG_APPLICATION_ID}")
    conn.execute(f"PRAGMA user_version = {GPKG_USER_VERSION}")

    _insert_default_srs_rows(conn)
    _register_usap_extension(conn, profile_version=profile_version)
    _register_attribute_layers(conn)
    _register_features_layer(conn)


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
    # The definition must stay a bare URI (the standard asks for a reference
    # to the defining document); the profile version rides in the URI path
    # rather than as appended prose.
    definition = usap_extension_definition(profile_version)

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


def _register_attribute_layers(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO gpkg_contents (
            table_name,
            data_type,
            identifier,
            description
        )
        VALUES (?, 'attributes', ?, ?)
        """,
        USAP_ATTRIBUTE_LAYERS,
    )


def _register_features_layer(conn: sqlite3.Connection) -> None:
    # The contents row must exist before the geometry-columns row (FK).
    conn.execute(
        """
        INSERT OR IGNORE INTO gpkg_contents (
            table_name,
            data_type,
            identifier,
            description,
            srs_id
        )
        VALUES (?, 'features', 'USAP asset extents',
                'Derived 2D bounding box per registered asset (not authoritative geometry).',
                ?)
        """,
        (USAP_FEATURES_LAYER, DEFAULT_EXTENT_SRS_ID),
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO gpkg_geometry_columns (
            table_name,
            column_name,
            geometry_type_name,
            srs_id,
            z,
            m
        )
        VALUES (?, 'geom', 'POLYGON', ?, 0, 0)
        """,
        (USAP_FEATURES_LAYER, DEFAULT_EXTENT_SRS_ID),
    )


# ---------------------------------------------------------------------------
# GPKG geometry blobs (GeoPackageBinary: 'GP' header + envelope + WKB)
# ---------------------------------------------------------------------------

def encode_gpkg_bbox_polygon(
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    srs_id: int,
) -> bytes:
    """
    Encode a 2D bounding box as a GeoPackageBinary POLYGON blob
    (little-endian, envelope indicator 1).
    """
    # flags 0x03: header little-endian (bit 0) + [minx,maxx,miny,maxy]
    # envelope (indicator 1, bits 1-3).
    header = struct.pack("<2sBBi", b"GP", 0, 0x03, srs_id)
    envelope = struct.pack("<4d", minx, maxx, miny, maxy)

    ring = [
        (minx, miny),
        (maxx, miny),
        (maxx, maxy),
        (minx, maxy),
        (minx, miny),
    ]

    # WKB: little-endian marker, type 3 (Polygon), 1 ring, 5 points.
    wkb = struct.pack("<BIII", 1, 3, 1, len(ring))

    for x, y in ring:
        wkb += struct.pack("<2d", x, y)

    return header + envelope + wkb


def decode_gpkg_envelope(blob: bytes) -> dict[str, Any]:
    """
    Read the srs_id and 2D envelope out of a GeoPackageBinary blob.
    """
    if len(blob) < 40 or blob[:2] != b"GP":
        raise USAPError("Not a GeoPackage geometry blob (bad magic or length).")

    flags = blob[3]
    order = "<" if flags & 0x01 else ">"
    envelope_indicator = (flags >> 1) & 0x07

    if envelope_indicator != 1:
        raise USAPError(
            f"Unsupported GPKG envelope indicator: {envelope_indicator}."
        )

    (srs_id,) = struct.unpack(order + "i", blob[4:8])
    minx, maxx, miny, maxy = struct.unpack(order + "4d", blob[8:40])

    return {
        "srs_id": srs_id,
        "minx": minx,
        "maxx": maxx,
        "miny": miny,
        "maxy": maxy,
    }


# ---------------------------------------------------------------------------
# SRS handling
# ---------------------------------------------------------------------------

_EPSG_WKT_PATTERNS = (
    re.compile(r'AUTHORITY\[\s*"EPSG"\s*,\s*"?(\d+)"?\s*\]', re.IGNORECASE),
    re.compile(r'\bID\[\s*"EPSG"\s*,\s*(\d+)\s*\]', re.IGNORECASE),
)


def epsg_from_wkt(wkt: str | None) -> int | None:
    """
    Best-effort EPSG code from a CRS WKT string (WKT1 AUTHORITY[...] or
    WKT2 ID[...]). The last occurrence is the whole-CRS authority. Returns
    None when absent — no pyproj, no guessing.
    """
    if not wkt:
        return None

    for pattern in _EPSG_WKT_PATTERNS:
        matches = pattern.findall(wkt)

        if matches:
            return int(matches[-1])

    return None


def wkt_for_epsg(srs_id: int) -> str | None:
    """
    Look up an EPSG code's WKT definition, or None when that is not possible.

    pyproj (the 'crs' extra) carries the EPSG database; without it USAP has no
    way to turn a bare code into the definition a conformant GeoPackage needs,
    and says so rather than inventing one.
    """
    try:
        from pyproj import CRS
    except ImportError:
        return None

    try:
        return CRS.from_epsg(srs_id).to_wkt()
    except Exception:
        return None


def ensure_srs_row(
    conn: sqlite3.Connection,
    srs_id: int,
    definition_wkt: str | None = None,
    name: str | None = None,
) -> None:
    """
    Idempotently insert a gpkg_spatial_ref_sys row for an EPSG code.

    A real SRS needs a real definition: the GeoPackage standard reserves the
    "undefined" definitions for the built-in ids -1 and 0, and requires
    records defining every SRS the package actually uses. Writing
    definition='undefined' under a positive EPSG code produced a row that
    named a CRS without defining it.

    A pre-existing incomplete row is repaired when a definition finally
    arrives — INSERT OR IGNORE alone left it broken forever, so a package
    that first saw the code without WKT could never be fixed.
    """
    if srs_id in (-1, 0):
        return

    if not definition_wkt:
        definition_wkt = wkt_for_epsg(srs_id)

    if not definition_wkt:
        raise USAPError(
            f"Cannot register SRS {srs_id} without a definition: the "
            "GeoPackage standard reserves undefined definitions for srs_id "
            "-1 and 0, and requires a record defining every SRS the package "
            "uses. Either supply the CRS WKT (project config 'srs_wkt'), or "
            "install usap[crs] so it can be looked up from the EPSG code."
        )

    conn.execute(
        """
        INSERT OR IGNORE INTO gpkg_spatial_ref_sys (
            srs_name,
            srs_id,
            organization,
            organization_coordsys_id,
            definition,
            description
        )
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (
            name or f"EPSG:{srs_id}",
            srs_id,
            "EPSG",
            srs_id,
            definition_wkt,
        ),
    )

    conn.execute(
        """
        UPDATE gpkg_spatial_ref_sys
        SET definition = ?,
            srs_name = ?
        WHERE srs_id = ?
          AND (definition IS NULL OR definition = 'undefined')
        """,
        (definition_wkt, name or f"EPSG:{srs_id}", srs_id),
    )


def set_package_srs(
    conn: sqlite3.Connection,
    srs_id: int,
    definition_wkt: str | None = None,
) -> None:
    """
    Declare the CRS of the asset-extents features layer (USAP assumes one
    CRS per package). Ensures the SRS row, updates the layer registration,
    and re-encodes any already-written extent blobs so their header srs_id
    matches — safe to call before or after asset registration.
    """
    ensure_srs_row(conn, srs_id, definition_wkt=definition_wkt)

    conn.execute(
        "UPDATE gpkg_contents SET srs_id = ? WHERE table_name = ?",
        (srs_id, USAP_FEATURES_LAYER),
    )
    conn.execute(
        "UPDATE gpkg_geometry_columns SET srs_id = ? WHERE table_name = ?",
        (srs_id, USAP_FEATURES_LAYER),
    )

    # Positional access: works with or without a Row factory on conn.
    for asset_id, geom in conn.execute(
        "SELECT asset_id, geom FROM usap_asset_extent"
    ).fetchall():
        envelope = decode_gpkg_envelope(geom)

        if envelope["srs_id"] == srs_id:
            continue

        conn.execute(
            "UPDATE usap_asset_extent SET geom = ? WHERE asset_id = ?",
            (
                encode_gpkg_bbox_polygon(
                    envelope["minx"],
                    envelope["miny"],
                    envelope["maxx"],
                    envelope["maxy"],
                    srs_id,
                ),
                asset_id,
            ),
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
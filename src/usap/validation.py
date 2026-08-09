from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ._util import parse_content_hash, sha256_file
from .constants import (
    ANNOTATION_STATUSES,
    CITY_OBJECT_STATUSES,
    CONFIDENCE_RANGE,
    DEFAULT_ENCODING,
    VALUE_DTYPES,
)
from .encoding import decode_roaring_bitmap, decode_value_block
from .errors import USAPError
from .geopackage import (
    GPKG_APPLICATION_ID,
    GPKG_USER_VERSION,
    USAP_ATTRIBUTE_LAYERS,
    USAP_EXTENSION_NAME,
    USAP_EXTENSION_TABLES,
    USAP_FEATURES_LAYER,
    decode_gpkg_envelope,
    read_geopackage_header,
)


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    table: str | None = None
    row_id: int | None = None
    details: dict[str, Any] | None = None

    def format(self) -> str:
        location = ""

        if self.table is not None:
            location += f" table={self.table}"

        if self.row_id is not None:
            location += f" row_id={self.row_id}"

        return f"[{self.severity.upper()}] {self.code}:{location} {self.message}"


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        table: str | None = None,
        row_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                severity=severity,
                code=code,
                message=message,
                table=table,
                row_id=row_id,
                details=details,
            )
        )

    def print(self) -> None:
        if not self.issues:
            print("Validation OK")
            return

        for issue in self.issues:
            print(issue.format())


VALIDATION_LEVELS = ("basic", "deep", "external")


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row["n"])


def validate_connection(
    conn: sqlite3.Connection,
    level: str = "deep",
) -> ValidationReport:
    """
    Validate a USAP package, at one of three levels.

    ``basic``     structure only, entirely in SQL: profile, GeoPackage
                  metadata and layers, concept registry, orphan references,
                  primary-object link agreement, duplicate edges, class
                  closure. Never reads a block payload, so it stays cheap on
                  a package with millions of membership blocks.

    ``deep``      (default) everything in ``basic``, plus every membership
                  and value payload decoded and checked against its stored
                  counts/bounds, containment acyclicity, asset-extent
                  recomputation, and annotation domain constraints
                  (status/confidence/attributes JSON).

    ``external``  everything in ``deep``, plus each registered asset file:
                  does it still exist, and does its SHA-256 still match the
                  hash recorded at registration. Opt-in because hashing a
                  10 GB point cloud costs minutes.

    ``basic`` and ``deep`` work from the database connection alone — they
    need no geometry files, CityGML, or CityJSON. Only ``external`` touches
    the filesystem.
    """
    if level not in VALIDATION_LEVELS:
        raise USAPError(
            f"Unknown validation level {level!r}. "
            f"Use one of: {', '.join(VALIDATION_LEVELS)}."
        )

    deep = level in ("deep", "external")

    report = ValidationReport()

    # The checks read columns by name, so the connection must produce
    # sqlite3.Row rows; restore whatever factory the caller had set.
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row

    try:
        _validate_geopackage_metadata(conn, report)
        _validate_gis_layers(conn, report)
        _validate_profile(conn, report)
        _validate_semantic_class_registry(conn, report)
        _validate_orphans(conn, report)
        _validate_annotation_object_links(conn, report)
        _validate_membership_blocks(conn, report, decode_payloads=deep)
        _validate_value_blocks(conn, report, decode_payloads=deep)
        _validate_semantic_class_closure(conn, report)
        _validate_city_object_relationships(conn, report)
        _validate_relationship_types(conn, report)

        if deep:
            _validate_asset_extents(conn, report)
            _validate_asset_crs(conn, report)
            _validate_content_hashes(conn, report)
            _validate_city_object_graph(conn, report)
            _validate_annotation_domain(conn, report)

        if level == "external":
            _validate_external_assets(conn, report)
    finally:
        conn.row_factory = original_row_factory

    return report


def _validate_profile(conn: sqlite3.Connection, report: ValidationReport) -> None:
    n = _count(
        conn,
        """
        SELECT COUNT(*) AS n
        FROM usap_profile
        WHERE profile_id = 1
        """,
    )

    if n != 1:
        report.add(
            severity="error",
            code="MISSING_PROFILE",
            message="Expected exactly one usap_profile row with profile_id = 1.",
            table="usap_profile",
        )

        # Every other check here reads that row.
        return

    row = conn.execute(
        """
        SELECT package_iri
        FROM usap_profile
        WHERE profile_id = 1
        """
    ).fetchone()

    package_iri = (row["package_iri"] or "").strip()

    # The column is NOT NULL, so this catches a package written by something
    # other than USAPPackage.create: whitespace, or a value that is not an
    # absolute IRI and so cannot identify the package outside this file.
    if not package_iri or ":" not in package_iri:
        report.add(
            severity="error",
            code="INVALID_PACKAGE_IRI",
            message=(
                "usap_profile.package_iri must be an absolute IRI "
                "(USAPPackage.create mints a 'urn:uuid:...')."
            ),
            table="usap_profile",
            details={"package_iri": row["package_iri"]},
        )


def _validate_semantic_class_registry(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Validate the accepted concept registry.

    Duplicate local names across different schemes are allowed.
    class_uri is globally unique, so a class_uri must not appear more than once.
    """
    rows = conn.execute(
        """
        SELECT
            class_uri,
            COUNT(*) AS n
        FROM usap_semantic_class
        GROUP BY class_uri
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for row in rows:
        report.add(
            severity="error",
            code="DUPLICATE_SEMANTIC_CLASS_REGISTRATION",
            message="Duplicate semantic class registration for the same class_uri.",
            table="usap_semantic_class",
            details={
                "class_uri": row["class_uri"],
                "count": int(row["n"]),
            },
        )

def _validate_relationship_types(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Report link types in use that no ontology has classified, and edges whose
    target is not in this package.

    Both are warnings, and both exist so that an accepted consequence of the
    design stays *visible*. USAP ships no link vocabulary: an import registers
    whatever types the document uses, and until something supplies a category
    those edges are stored and queryable by name but sit outside the default
    "and its parts" traversal. That must be reported, never silent — a
    descendants query quietly returning the root alone is the failure mode this
    check exists to prevent.
    """
    unclassified = conn.execute(
        """
        SELECT
            rt.local_name,
            rt.code_space,
            COUNT(*) AS n
        FROM usap_city_object_relationship AS r
        JOIN usap_relationship_type AS rt
            ON rt.relationship_type_id = r.relationship_type_id
        WHERE rt.category IS NULL
        GROUP BY rt.relationship_type_id
        ORDER BY rt.local_name
        """
    ).fetchall()

    for row in unclassified:
        report.add(
            severity="warning",
            code="UNCLASSIFIED_RELATIONSHIP_TYPE",
            message=(
                "Relationship type has no category, so its edges are stored "
                "but not followed by the default traversal."
            ),
            table="usap_relationship_type",
            details={
                "local_name": row["local_name"],
                "code_space": row["code_space"],
                "edge_count": int(row["n"]),
            },
        )

    unresolved = conn.execute(
        """
        SELECT to_external_uri
        FROM usap_city_object_relationship
        WHERE to_external_uri IS NOT NULL
        ORDER BY to_external_uri
        LIMIT 5
        """
    ).fetchall()

    if unresolved:
        total = _count(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM usap_city_object_relationship
            WHERE to_external_uri IS NOT NULL
            """,
        )

        report.add(
            severity="warning",
            code="UNRESOLVED_RELATIONSHIP_TARGET",
            message=(
                "Relationship target is outside this package; the link is "
                "recorded but its target cannot be dereferenced here."
            ),
            table="usap_city_object_relationship",
            details={
                "count": total,
                "sample": [row["to_external_uri"] for row in unresolved],
            },
        )


def _validate_city_object_relationships(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Flag duplicate relationship edges.

    link_city_objects is idempotent, so identical edges should not repeat;
    duplicates indicate a package built before that guard existed (or raw
    SQL writes). Warning, not error: such packages must keep opening.
    """
    rows = conn.execute(
        """
        SELECT
            r.graph_name,
            r.from_city_object_id,
            r.to_city_object_id,
            r.to_external_uri,
            rt.local_name AS relationship_type,
            rt.code_space,
            COUNT(*) AS n
        FROM usap_city_object_relationship AS r
        JOIN usap_relationship_type AS rt
            ON rt.relationship_type_id = r.relationship_type_id
        GROUP BY
            r.graph_name,
            r.from_city_object_id,
            r.to_city_object_id,
            r.to_external_uri,
            r.relationship_type_id,
            r.role,
            r.source_relation_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for row in rows:
        report.add(
            severity="warning",
            code="DUPLICATE_RELATIONSHIP_EDGE",
            message="Identical relationship edge stored more than once.",
            table="usap_city_object_relationship",
            details={
                "graph_name": row["graph_name"],
                "from_city_object_id": int(row["from_city_object_id"]),
                "to_city_object_id": (
                    int(row["to_city_object_id"])
                    if row["to_city_object_id"] is not None
                    else None
                ),
                "to_external_uri": row["to_external_uri"],
                # Joined back to a name: the stored column is an id, and a
                # report that printed it would be unreadable.
                "relationship_type": row["relationship_type"],
                "code_space": row["code_space"],
                "count": int(row["n"]),
            },
        )

def _validate_annotation_object_links(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Flag annotations whose primary object has no matching 'represents' link.

    An annotation's primary city object is stored twice: as
    usap_annotation.primary_city_object_id and as a 'represents' row in
    usap_annotation_object. The write paths keep both in step; when they
    disagree, city-object queries can return the annotation under an object it
    no longer belongs to (or, per relationship_types, miss it entirely).

    Extra 'represents' rows for *other* objects are not flagged: an annotation
    may legitimately represent several city objects (link_annotation_to_object),
    and only the primary one is knowable from the annotation row.
    """
    rows = conn.execute(
        """
        SELECT
            a.annotation_id,
            a.annotation_uid,
            a.primary_city_object_id
        FROM usap_annotation AS a
        WHERE a.primary_city_object_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM usap_annotation_object AS ao
              WHERE ao.annotation_id = a.annotation_id
                AND ao.city_object_id = a.primary_city_object_id
                AND ao.relation_type = 'represents'
          )
        """
    ).fetchall()

    for row in rows:
        report.add(
            severity="error",
            code="ANNOTATION_PRIMARY_OBJECT_LINK_MISSING",
            message=(
                "Annotation names a primary city object but has no matching "
                "'represents' link."
            ),
            table="usap_annotation",
            details={
                "annotation_id": int(row["annotation_id"]),
                "annotation_uid": row["annotation_uid"],
                "primary_city_object_id": int(row["primary_city_object_id"]),
            },
        )


def _validate_orphans(conn: sqlite3.Connection, report: ValidationReport) -> None:
    checks = [
        (
            "ORPHAN_MEMBERSHIP_ANNOTATION",
            "usap_membership_block",
            """
            SELECT COUNT(*) AS n
            FROM usap_membership_block AS mb
            LEFT JOIN usap_annotation AS a
                ON a.annotation_id = mb.annotation_id
            WHERE a.annotation_id IS NULL
            """,
            "Membership block references a missing annotation.",
        ),
        (
            "ORPHAN_MEMBERSHIP_ASSET_PART",
            "usap_membership_block",
            """
            SELECT COUNT(*) AS n
            FROM usap_membership_block AS mb
            LEFT JOIN usap_asset_part AS ap
                ON ap.asset_part_id = mb.asset_part_id
            WHERE ap.asset_part_id IS NULL
            """,
            "Membership block references a missing asset part.",
        ),
        (
            "ORPHAN_VALUE_BLOCK_ANNOTATION",
            "usap_value_block",
            """
            SELECT COUNT(*) AS n
            FROM usap_value_block AS vb
            LEFT JOIN usap_annotation AS a
                ON a.annotation_id = vb.annotation_id
            WHERE a.annotation_id IS NULL
            """,
            "Value block references a missing annotation.",
        ),
        (
            "ORPHAN_VALUE_BLOCK_ASSET_PART",
            "usap_value_block",
            """
            SELECT COUNT(*) AS n
            FROM usap_value_block AS vb
            LEFT JOIN usap_asset_part AS ap
                ON ap.asset_part_id = vb.asset_part_id
            WHERE ap.asset_part_id IS NULL
            """,
            "Value block references a missing asset part.",
        ),
        (
            "ORPHAN_ANNOTATION_CLASS",
            "usap_annotation",
            """
            SELECT COUNT(*) AS n
            FROM usap_annotation AS a
            LEFT JOIN usap_semantic_class AS sc
                ON sc.semantic_class_id = a.semantic_class_id
            WHERE sc.semantic_class_id IS NULL
            """,
            "Annotation references a missing semantic class.",
        ),
        (
            "ORPHAN_ANNOTATION_OBJECT_ANNOTATION",
            "usap_annotation_object",
            """
            SELECT COUNT(*) AS n
            FROM usap_annotation_object AS ao
            LEFT JOIN usap_annotation AS a
                ON a.annotation_id = ao.annotation_id
            WHERE a.annotation_id IS NULL
            """,
            "Annotation-object link references a missing annotation.",
        ),
        (
            "ORPHAN_ANNOTATION_OBJECT_CITY_OBJECT",
            "usap_annotation_object",
            """
            SELECT COUNT(*) AS n
            FROM usap_annotation_object AS ao
            LEFT JOIN usap_city_object AS co
                ON co.city_object_id = ao.city_object_id
            WHERE co.city_object_id IS NULL
            """,
            "Annotation-object link references a missing city object.",
        ),
        (
            "ORPHAN_RELATIONSHIP_FROM",
            "usap_city_object_relationship",
            """
            SELECT COUNT(*) AS n
            FROM usap_city_object_relationship AS r
            LEFT JOIN usap_city_object AS co
                ON co.city_object_id = r.from_city_object_id
            WHERE co.city_object_id IS NULL
            """,
            "City-object relationship references a missing source object.",
        ),
        (
            # The IS NOT NULL guard is load-bearing: an edge whose target is
            # an external URI legitimately has no to_city_object_id, and
            # without it every xlink-carrying package reports as invalid.
            "ORPHAN_RELATIONSHIP_TO",
            "usap_city_object_relationship",
            """
            SELECT COUNT(*) AS n
            FROM usap_city_object_relationship AS r
            LEFT JOIN usap_city_object AS co
                ON co.city_object_id = r.to_city_object_id
            WHERE r.to_city_object_id IS NOT NULL
              AND co.city_object_id IS NULL
            """,
            "City-object relationship references a missing target object.",
        ),
        (
            "ORPHAN_RELATIONSHIP_TYPE",
            "usap_city_object_relationship",
            """
            SELECT COUNT(*) AS n
            FROM usap_city_object_relationship AS r
            LEFT JOIN usap_relationship_type AS rt
                ON rt.relationship_type_id = r.relationship_type_id
            WHERE rt.relationship_type_id IS NULL
            """,
            "City-object relationship references a missing relationship type.",
        ),
    ]

    for code, table, sql, message in checks:
        n = _count(conn, sql)

        if n:
            report.add(
                severity="error",
                code=code,
                message=f"{message} Count: {n}.",
                table=table,
            )


def _validate_membership_blocks(
    conn: sqlite3.Connection,
    report: ValidationReport,
    *,
    decode_payloads: bool,
) -> None:
    """
    Check membership blocks.

    With decode_payloads=False the payload column is not even selected: the
    blobs are the bulk of a large package, and reading them off disk is the
    cost 'basic' exists to avoid. Everything checkable from the block's own
    columns is still checked.
    """
    payload_column = "mb.payload," if decode_payloads else "NULL AS payload,"

    rows = conn.execute(
        f"""
        SELECT
            mb.membership_block_id,
            mb.annotation_id,
            mb.asset_part_id,
            mb.element_kind,
            mb.block_start,
            mb.block_size,
            mb.encoding,
            mb.element_count,
            mb.min_element_index,
            mb.max_element_index,
            {payload_column}

            ap.element_kind AS asset_part_element_kind,
            ap.element_count AS asset_part_element_count
        FROM usap_membership_block AS mb
        LEFT JOIN usap_asset_part AS ap
            ON ap.asset_part_id = mb.asset_part_id
        ORDER BY mb.membership_block_id
        """
    ).fetchall()

    for row in rows:
        block_id = int(row["membership_block_id"])

        encoding = row["encoding"]

        if encoding != DEFAULT_ENCODING:
            report.add(
                severity="error",
                code="UNSUPPORTED_MEMBERSHIP_ENCODING",
                message=f"Unsupported membership encoding: {encoding!r}.",
                table="usap_membership_block",
                row_id=block_id,
            )
            continue

        block_size = int(row["block_size"])
        block_start = int(row["block_start"])
        declared_element_count = int(row["element_count"])
        min_element_index = int(row["min_element_index"])
        max_element_index = int(row["max_element_index"])

        if block_size <= 0:
            report.add(
                severity="error",
                code="INVALID_BLOCK_SIZE",
                message="Membership block has non-positive block_size.",
                table="usap_membership_block",
                row_id=block_id,
            )
            continue

        if block_start < 0:
            report.add(
                severity="error",
                code="INVALID_BLOCK_START",
                message="Membership block has negative block_start.",
                table="usap_membership_block",
                row_id=block_id,
            )

        if block_start % block_size != 0:
            report.add(
                severity="warning",
                code="MISALIGNED_BLOCK_START",
                message="block_start is not aligned to block_size.",
                table="usap_membership_block",
                row_id=block_id,
                details={
                    "block_start": block_start,
                    "block_size": block_size,
                },
            )

        if declared_element_count <= 0:
            report.add(
                severity="error",
                code="EMPTY_MEMBERSHIP_BLOCK",
                message="Membership block has zero or negative element_count.",
                table="usap_membership_block",
                row_id=block_id,
            )

        if min_element_index > max_element_index:
            report.add(
                severity="error",
                code="INVALID_MEMBERSHIP_MIN_MAX",
                message="min_element_index is greater than max_element_index.",
                table="usap_membership_block",
                row_id=block_id,
            )

        asset_part_element_kind = row["asset_part_element_kind"]

        if asset_part_element_kind is not None:
            asset_part_element_kind = int(asset_part_element_kind)

            if int(row["element_kind"]) != asset_part_element_kind:
                report.add(
                    severity="error",
                    code="MEMBERSHIP_ELEMENT_KIND_MISMATCH",
                    message=(
                        "Membership element_kind differs from asset part "
                        "element_kind."
                    ),
                    table="usap_membership_block",
                    row_id=block_id,
                    details={
                        "membership_element_kind": int(row["element_kind"]),
                        "asset_part_element_kind": asset_part_element_kind,
                    },
                )

        if not decode_payloads:
            continue

        try:
            bitmap = decode_roaring_bitmap(row["payload"])
        except Exception as exc:
            report.add(
                severity="error",
                code="CORRUPT_MEMBERSHIP_PAYLOAD",
                message=f"Could not decode roaring payload: {exc}",
                table="usap_membership_block",
                row_id=block_id,
            )
            continue

        # A roaring bitmap is a set: it cannot carry duplicate or unsorted
        # offsets, so the checks that used to look for them are gone. What a
        # payload can still do is disagree with the row that describes it,
        # which is what the rest of this block checks.
        decoded_count = len(bitmap)

        if decoded_count != declared_element_count:
            report.add(
                severity="error",
                code="MEMBERSHIP_COUNT_MISMATCH",
                message=(
                    "Decoded payload count does not match declared "
                    "element_count."
                ),
                table="usap_membership_block",
                row_id=block_id,
                details={
                    "declared": declared_element_count,
                    "decoded": decoded_count,
                },
            )

        if decoded_count and bitmap.max() >= block_size:
            report.add(
                severity="error",
                code="OFFSET_OUTSIDE_BLOCK",
                message="Decoded offset is outside the block range.",
                table="usap_membership_block",
                row_id=block_id,
                details={
                    "offset": int(bitmap.max()),
                    "block_size": block_size,
                },
            )

        if decoded_count:
            # min()/max() are O(1) on a roaring bitmap — no sort, and no
            # decode to an array, which matters when validating a package
            # that annotates a 10 GB point cloud block by block.
            actual_min = block_start + int(bitmap.min())
            actual_max = block_start + int(bitmap.max())

            if actual_min != min_element_index:
                report.add(
                    severity="error",
                    code="MIN_ELEMENT_MISMATCH",
                    message="Stored min_element_index does not match payload.",
                    table="usap_membership_block",
                    row_id=block_id,
                    details={
                        "stored": min_element_index,
                        "actual": actual_min,
                    },
                )

            if actual_max != max_element_index:
                report.add(
                    severity="error",
                    code="MAX_ELEMENT_MISMATCH",
                    message="Stored max_element_index does not match payload.",
                    table="usap_membership_block",
                    row_id=block_id,
                    details={
                        "stored": max_element_index,
                        "actual": actual_max,
                    },
                )

            asset_part_element_count = row["asset_part_element_count"]

            if asset_part_element_count is not None:
                asset_part_element_count = int(asset_part_element_count)

                if actual_min < 0 or actual_max >= asset_part_element_count:
                    report.add(
                        severity="error",
                        code="MEMBERSHIP_OUT_OF_ASSET_PART_RANGE",
                        message=(
                            "Membership block contains element indices outside "
                            "the asset part element_count."
                        ),
                        table="usap_membership_block",
                        row_id=block_id,
                        details={
                            "actual_min": actual_min,
                            "actual_max": actual_max,
                            "asset_part_element_count": asset_part_element_count,
                        },
                    )


def _validate_value_blocks(
    conn: sqlite3.Connection,
    report: ValidationReport,
    *,
    decode_payloads: bool,
) -> None:
    """
    Check value blocks. See _validate_membership_blocks on decode_payloads:
    the stored value_min/value_max can only be confirmed against the payload,
    so that comparison is the part 'basic' gives up.
    """
    payload_column = "vb.payload," if decode_payloads else "NULL AS payload,"

    rows = conn.execute(
        f"""
        SELECT
            vb.value_block_id,
            vb.annotation_id,
            vb.asset_part_id,
            vb.element_kind,
            vb.block_start,
            vb.element_count,
            vb.value_dtype,
            vb.value_min,
            vb.value_max,
            {payload_column}

            ap.element_kind AS asset_part_element_kind,
            ap.element_count AS asset_part_element_count
        FROM usap_value_block AS vb
        LEFT JOIN usap_asset_part AS ap
            ON ap.asset_part_id = vb.asset_part_id
        ORDER BY
            vb.annotation_id,
            vb.asset_part_id,
            vb.element_kind,
            vb.block_start
        """
    ).fetchall()

    groups: dict[tuple[int, int, int], list[sqlite3.Row]] = {}

    for row in rows:
        key = (
            int(row["annotation_id"]),
            int(row["asset_part_id"]),
            int(row["element_kind"]),
        )
        groups.setdefault(key, []).append(row)

    for (annotation_id, asset_part_id, element_kind), block_rows in groups.items():
        expected_next_start = 0
        has_gap = False

        for row in block_rows:
            block_id = int(row["value_block_id"])
            block_start = int(row["block_start"])
            element_count = int(row["element_count"])
            value_dtype = row["value_dtype"]

            if value_dtype not in VALUE_DTYPES:
                report.add(
                    severity="error",
                    code="UNSUPPORTED_VALUE_DTYPE",
                    message=f"Unsupported value_dtype: {value_dtype!r}.",
                    table="usap_value_block",
                    row_id=block_id,
                )
                continue

            if element_count <= 0:
                report.add(
                    severity="error",
                    code="EMPTY_VALUE_BLOCK",
                    message="Value block has zero or negative element_count.",
                    table="usap_value_block",
                    row_id=block_id,
                )
                continue

            if block_start < 0:
                report.add(
                    severity="error",
                    code="INVALID_VALUE_BLOCK_START",
                    message="Value block has negative block_start.",
                    table="usap_value_block",
                    row_id=block_id,
                )

            asset_part_element_kind = row["asset_part_element_kind"]

            if (
                asset_part_element_kind is not None
                and element_kind != int(asset_part_element_kind)
            ):
                report.add(
                    severity="error",
                    code="VALUE_ELEMENT_KIND_MISMATCH",
                    message=(
                        "Value block element_kind differs from asset part "
                        "element_kind."
                    ),
                    table="usap_value_block",
                    row_id=block_id,
                    details={
                        "value_block_element_kind": element_kind,
                        "asset_part_element_kind": int(asset_part_element_kind),
                    },
                )

            asset_part_element_count = row["asset_part_element_count"]

            if (
                asset_part_element_count is not None
                and block_start + element_count > int(asset_part_element_count)
            ):
                report.add(
                    severity="error",
                    code="VALUE_BLOCK_OUT_OF_RANGE",
                    message=(
                        "Value block covers element indices outside the "
                        "asset part element_count."
                    ),
                    table="usap_value_block",
                    row_id=block_id,
                    details={
                        "block_start": block_start,
                        "element_count": element_count,
                        "asset_part_element_count": int(asset_part_element_count),
                    },
                )

            # Overlap is corruption; gaps are just partial coverage.
            if block_start < expected_next_start:
                report.add(
                    severity="error",
                    code="OVERLAPPING_VALUE_BLOCKS",
                    message=(
                        "Value block overlaps the previous block of the "
                        "same field."
                    ),
                    table="usap_value_block",
                    row_id=block_id,
                    details={
                        "block_start": block_start,
                        "expected_next_start": expected_next_start,
                    },
                )
            elif block_start > expected_next_start:
                has_gap = True

            expected_next_start = max(
                expected_next_start, block_start + element_count
            )

            if not decode_payloads:
                continue

            try:
                values = decode_value_block(
                    row["payload"], value_dtype, element_count
                )
            except Exception as exc:
                report.add(
                    severity="error",
                    code="CORRUPT_VALUE_PAYLOAD",
                    message=f"Could not decode value-block payload: {exc}",
                    table="usap_value_block",
                    row_id=block_id,
                )
                continue

            if values.dtype.kind == "f":
                real = values[~np.isnan(values)]
            else:
                real = values

            actual_min = float(real.min()) if real.size else None
            actual_max = float(real.max()) if real.size else None

            stored_min = row["value_min"]
            stored_max = row["value_max"]

            if stored_min != actual_min or stored_max != actual_max:
                report.add(
                    severity="error",
                    code="VALUE_MIN_MAX_MISMATCH",
                    message=(
                        "Stored value_min/value_max do not match the payload."
                    ),
                    table="usap_value_block",
                    row_id=block_id,
                    details={
                        "stored": [stored_min, stored_max],
                        "actual": [actual_min, actual_max],
                    },
                )

        # One field = one dtype; readers refuse mixed-dtype fields as
        # corrupt, so a clean report must refuse them too.
        group_dtypes = sorted({row["value_dtype"] for row in block_rows})

        if len(group_dtypes) > 1:
            report.add(
                severity="error",
                code="MIXED_VALUE_DTYPE_FIELD",
                message="Value field mixes dtypes across its blocks.",
                table="usap_value_block",
                details={
                    "annotation_id": annotation_id,
                    "asset_part_id": asset_part_id,
                    "element_kind": element_kind,
                    "value_dtypes": group_dtypes,
                },
            )

        # V1 writers always produce full coverage; partial coverage is a
        # future format, not corruption — flag it softly.
        asset_part_element_count = block_rows[0]["asset_part_element_count"]

        if asset_part_element_count is not None and (
            has_gap or expected_next_start != int(asset_part_element_count)
        ):
            report.add(
                severity="warning",
                code="PARTIAL_VALUE_FIELD_COVERAGE",
                message=(
                    "Value field does not cover the whole asset part; "
                    "v1 readers will reject it."
                ),
                table="usap_value_block",
                details={
                    "annotation_id": annotation_id,
                    "asset_part_id": asset_part_id,
                    "element_kind": element_kind,
                    "covered_until": expected_next_start,
                    "asset_part_element_count": int(asset_part_element_count),
                },
            )


def _validate_annotation_domain(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Check the annotation fields the SDK constrains on write.

    create_annotation/update_annotation refuse these values, so a package
    containing them was written by raw SQL or by an older build. They matter
    because readers act on them: an unknown status drops out of every status
    filter, a confidence outside [0, 1] cannot be compared with any other,
    and attributes that are not JSON cannot be read back at all.
    """
    minimum, maximum = CONFIDENCE_RANGE

    checks = [
        (
            "ANNOTATION_UNKNOWN_STATUS",
            "Annotation status is not one of the recognised values.",
            f"""
            SELECT annotation_id, annotation_uid, status AS value
            FROM usap_annotation
            WHERE status NOT IN ({",".join("?" for _ in ANNOTATION_STATUSES)})
            """,
            ANNOTATION_STATUSES,
        ),
        (
            "ANNOTATION_CONFIDENCE_OUT_OF_RANGE",
            f"Annotation confidence is outside [{minimum}, {maximum}].",
            """
            SELECT annotation_id, annotation_uid, confidence AS value
            FROM usap_annotation
            WHERE confidence IS NOT NULL
              AND (confidence < ? OR confidence > ?)
            """,
            (minimum, maximum),
        ),
        (
            "ANNOTATION_ATTRIBUTES_NOT_JSON",
            "Annotation attributes_json does not parse as JSON.",
            """
            SELECT annotation_id, annotation_uid, attributes_json AS value
            FROM usap_annotation
            WHERE attributes_json IS NOT NULL
              AND json_valid(attributes_json) = 0
            """,
            (),
        ),
    ]

    for code, message, sql, params in checks:
        for row in conn.execute(sql, params).fetchall():
            report.add(
                severity="error",
                code=code,
                message=message,
                table="usap_annotation",
                row_id=int(row["annotation_id"]),
                details={
                    "annotation_uid": row["annotation_uid"],
                    "value": row["value"],
                },
            )

    unknown_object_status = conn.execute(
        f"""
        SELECT city_object_id, object_uid, object_status
        FROM usap_city_object
        WHERE object_status NOT IN (
            {",".join("?" for _ in CITY_OBJECT_STATUSES)}
        )
        """,
        CITY_OBJECT_STATUSES,
    ).fetchall()

    for row in unknown_object_status:
        report.add(
            severity="error",
            code="CITY_OBJECT_UNKNOWN_STATUS",
            message="City object status is not one of the recognised values.",
            table="usap_city_object",
            row_id=int(row["city_object_id"]),
            details={
                "object_uid": row["object_uid"],
                "value": row["object_status"],
            },
        )


def verify_assets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """
    Re-check every registered asset file against what was recorded for it.

    Returns one dict per asset with ``status``:
        ``ok``        the file is there and its hash still matches
        ``missing``   the uri does not resolve to an existing file
        ``changed``   the file exists but its SHA-256 differs
        ``unhashed``  registered with compute_hash=False, nothing to compare

    USAP annotations are bound to *one immutable version* of an external
    file by element index; if the file changed, the indices may now point at
    different elements, and nothing in the package can detect that on its
    own. Hashing is a full read of the file, so this is never part of a
    normal validate_report() — it is the 'external' level, or call it
    directly.
    """
    rows = conn.execute(
        """
        SELECT asset_id, uri, content_hash
        FROM usap_asset
        ORDER BY asset_id
        """
    ).fetchall()

    results: list[dict[str, Any]] = []

    for row in rows:
        uri = row["uri"]
        recorded_hash = row["content_hash"]
        path = Path(uri)

        recorded = parse_content_hash(recorded_hash)

        if not path.exists():
            status = "missing"
            actual_hash = None
        elif recorded is None:
            # No hash at all, or a caller-supplied token that is not a digest:
            # either way there is nothing to compare against, and re-hashing
            # the file could only produce a bogus "changed".
            status = "unhashed"
            actual_hash = None
        else:
            algorithm, recorded_digest = recorded

            if algorithm != "sha256":
                # Recording an algorithm we cannot compute is not a mismatch.
                status = "unhashed"
                actual_hash = None
            else:
                # Compare digests, not spellings: a record written before the
                # canonical form (bare hex) still matches a freshly computed
                # one. actual_hash is reported canonically either way.
                actual_digest = sha256_file(path)
                actual_hash = f"sha256:{actual_digest}"
                status = "ok" if actual_digest == recorded_digest else "changed"

        results.append(
            {
                "asset_id": int(row["asset_id"]),
                "uri": uri,
                "status": status,
                "recorded_hash": recorded_hash,
                "actual_hash": actual_hash,
            }
        )

    return results


def _validate_external_assets(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    for item in verify_assets(conn):
        if item["status"] == "missing":
            report.add(
                severity="error",
                code="ASSET_FILE_MISSING",
                message="Registered asset file does not exist.",
                table="usap_asset",
                row_id=item["asset_id"],
                details={"uri": item["uri"]},
            )
        elif item["status"] == "changed":
            report.add(
                severity="error",
                code="ASSET_FILE_CHANGED",
                message=(
                    "Registered asset file no longer matches the hash "
                    "recorded at registration; element indices may no "
                    "longer mean what they meant."
                ),
                table="usap_asset",
                row_id=item["asset_id"],
                details={
                    "uri": item["uri"],
                    "recorded_hash": item["recorded_hash"],
                    "actual_hash": item["actual_hash"],
                },
            )
        elif item["status"] == "unhashed":
            report.add(
                severity="warning",
                code="ASSET_NOT_HASHED",
                message=(
                    "Asset was registered without a content hash, so a "
                    "change to it cannot be detected."
                ),
                table="usap_asset",
                row_id=item["asset_id"],
                details={"uri": item["uri"]},
            )


def _validate_semantic_class_closure(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    missing_self = conn.execute(
        """
        SELECT sc.semantic_class_id
        FROM usap_semantic_class AS sc
        LEFT JOIN usap_semantic_class_closure AS c
            ON c.ancestor_class_id = sc.semantic_class_id
           AND c.descendant_class_id = sc.semantic_class_id
           AND c.depth = 0
        WHERE c.ancestor_class_id IS NULL
        """
    ).fetchall()

    for row in missing_self:
        report.add(
            severity="error",
            code="MISSING_SEMANTIC_CLASS_SELF_CLOSURE",
            message="Semantic class is missing depth-0 self closure row.",
            table="usap_semantic_class_closure",
            row_id=int(row["semantic_class_id"]),
        )

    missing_parent = conn.execute(
        """
        SELECT
            sc.semantic_class_id,
            sc.parent_class_id
        FROM usap_semantic_class AS sc
        LEFT JOIN usap_semantic_class_closure AS c
            ON c.ancestor_class_id = sc.parent_class_id
           AND c.descendant_class_id = sc.semantic_class_id
        WHERE sc.parent_class_id IS NOT NULL
          AND c.ancestor_class_id IS NULL
        """
    ).fetchall()

    for row in missing_parent:
        report.add(
            severity="error",
            code="MISSING_SEMANTIC_CLASS_PARENT_CLOSURE",
            message="Semantic class is missing closure row from parent.",
            table="usap_semantic_class_closure",
            row_id=int(row["semantic_class_id"]),
            details={
                "parent_class_id": int(row["parent_class_id"]),
            },
        )


def _validate_city_object_graph(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Flag containment cycles per named graph.

    "An object and its parts" is only meaningful if containment is acyclic:
    a cycle makes each object on it its own part. Descendant queries survive
    one (the recursive CTE deduplicates), but the package is stating something
    it cannot mean, so it is an error rather than a silent oddity.

    Only containment edges are checked — the graph is typed, and a cycle of
    peer edges (adjacentTo, predecessor/successor) is perfectly legitimate.
    An unclassified type is not containment and is reported separately by
    _validate_relationship_types, so it is skipped rather than assumed.

    Edges that leave the document are excluded: an external URI is not a node
    and cannot take part in a cycle within this package.
    """
    rows = conn.execute(
        """
        SELECT
            r.graph_name,
            r.from_city_object_id,
            r.to_city_object_id
        FROM usap_city_object_relationship AS r
        JOIN usap_relationship_type AS rt
            ON rt.relationship_type_id = r.relationship_type_id
        WHERE rt.category = 'containment'
          AND r.to_city_object_id IS NOT NULL
        """
    ).fetchall()

    edges_by_graph: dict[str, list[tuple[int, int]]] = {}

    for row in rows:
        edges_by_graph.setdefault(row["graph_name"], []).append(
            (int(row["from_city_object_id"]), int(row["to_city_object_id"]))
        )

    for graph_name, edges in sorted(edges_by_graph.items()):
        for object_id in _cycle_members(edges):
            report.add(
                severity="error",
                code="CITY_OBJECT_GRAPH_CYCLE",
                message=(
                    "City object takes part in a containment cycle, so it "
                    "would be its own part."
                ),
                table="usap_city_object_relationship",
                row_id=object_id,
                details={"graph_name": graph_name},
            )


def _cycle_members(edges: list[tuple[int, int]]) -> list[int]:
    """
    Return the object ids left over after a Kahn topological sort, i.e. the
    ones on (or downstream of) a cycle. Sorted, so reports are stable.
    """
    children: dict[int, list[int]] = {}
    in_degree: dict[int, int] = {}

    for parent_id, child_id in edges:
        children.setdefault(parent_id, []).append(child_id)
        in_degree.setdefault(parent_id, 0)
        in_degree[child_id] = in_degree.get(child_id, 0) + 1

    queue = [node for node, degree in in_degree.items() if degree == 0]
    settled = 0

    while queue:
        node = queue.pop()
        settled += 1

        for child_id in children.get(node, []):
            in_degree[child_id] -= 1

            if in_degree[child_id] == 0:
                queue.append(child_id)

    if settled == len(in_degree):
        return []

    return sorted(node for node, degree in in_degree.items() if degree > 0)


def _view_exists(conn: sqlite3.Connection, view_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'view'
          AND name = ?
        """,
        (view_name,),
    ).fetchone()

    return row is not None


def _validate_gis_layers(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Check that the GIS-facing views are present and registered so generic
    tools (QGIS/GDAL) can browse the package. Absence is a warning: packages
    created before the layers existed are still valid USAP files.
    """
    if not _table_exists(conn, "gpkg_contents"):
        return  # already reported by _validate_geopackage_metadata

    for view_name, _identifier, _description in USAP_ATTRIBUTE_LAYERS:
        registered = conn.execute(
            """
            SELECT 1
            FROM gpkg_contents
            WHERE table_name = ?
              AND data_type = 'attributes'
            """,
            (view_name,),
        ).fetchone()

        if not _view_exists(conn, view_name) or registered is None:
            report.add(
                severity="warning",
                code="MISSING_GIS_ATTRIBUTE_LAYER",
                message=(
                    "GIS attribute layer is missing or not registered in "
                    "gpkg_contents."
                ),
                table="gpkg_contents",
                details={"layer": view_name},
            )

    features_row = conn.execute(
        """
        SELECT srs_id
        FROM gpkg_contents
        WHERE table_name = ?
          AND data_type = 'features'
        """,
        (USAP_FEATURES_LAYER,),
    ).fetchone()

    geometry_row = None

    if _table_exists(conn, "gpkg_geometry_columns"):
        geometry_row = conn.execute(
            """
            SELECT srs_id
            FROM gpkg_geometry_columns
            WHERE table_name = ?
            """,
            (USAP_FEATURES_LAYER,),
        ).fetchone()

    has_view = _view_exists(conn, USAP_FEATURES_LAYER)

    if features_row is None and geometry_row is None and not has_view:
        report.add(
            severity="warning",
            code="MISSING_GIS_FEATURES_LAYER",
            message="GIS features layer (asset extents) is not present.",
            table="gpkg_contents",
            details={"layer": USAP_FEATURES_LAYER},
        )
        return

    if features_row is None or geometry_row is None or not has_view:
        report.add(
            severity="error",
            code="INCONSISTENT_FEATURES_LAYER",
            message=(
                "Asset-extents features layer is only partially registered "
                "(view / gpkg_contents / gpkg_geometry_columns disagree)."
            ),
            table="gpkg_contents",
            details={
                "layer": USAP_FEATURES_LAYER,
                "view_exists": has_view,
                "contents_row": features_row is not None,
                "geometry_columns_row": geometry_row is not None,
            },
        )
        return

    if features_row["srs_id"] != geometry_row["srs_id"]:
        report.add(
            severity="error",
            code="FEATURES_LAYER_SRS_MISMATCH",
            message=(
                "gpkg_contents and gpkg_geometry_columns declare different "
                "srs_id for the asset-extents layer."
            ),
            table="gpkg_geometry_columns",
            details={
                "gpkg_contents_srs_id": features_row["srs_id"],
                "gpkg_geometry_columns_srs_id": geometry_row["srs_id"],
            },
        )


def _validate_content_hashes(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Report content hashes that are not in the canonical 'algorithm:digest'
    form.

    A warning, not an error: content_hash is a free-text column and
    register_asset accepts any token, so a package written by another tool is
    unusual rather than corrupt. What it costs is verifiability — a value that
    does not parse as a digest can never be checked against the file
    (verify_assets reports it as 'unhashed'), and it cannot be exported as a
    digest-bearing state.

    A bare 64-char hex digest is canonical enough to pass: parse_content_hash
    reads it as sha-256, so it still compares equal to a freshly computed one.
    """
    rows = conn.execute(
        """
        SELECT asset_id, uri, content_hash
        FROM usap_asset
        WHERE content_hash IS NOT NULL
        ORDER BY asset_id
        """
    ).fetchall()

    for row in rows:
        if parse_content_hash(row["content_hash"]) is not None:
            continue

        report.add(
            severity="warning",
            code="NON_CANONICAL_CONTENT_HASH",
            message=(
                "Asset content_hash is not a recognizable digest; it cannot "
                "be verified against the file. Expected 'algorithm:digest', "
                "e.g. 'sha256:a48f...'."
            ),
            table="usap_asset",
            row_id=int(row["asset_id"]),
            details={
                "uri": row["uri"],
                "content_hash": row["content_hash"],
            },
        )


def _validate_asset_crs(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    """
    Flag assets recorded in more than one CRS.

    USAP assumes one CRS per package: set_package_srs declares it and rewrites
    the extent blobs' srs_id, but it does not *transform* any coordinate. That
    is only sound while every stored bound is already in the declared CRS, and
    nothing in the build enforces it across meshes and point clouds. A warning
    rather than an error — mixed CRSs are a real (if unsupported) state, and
    such packages must keep opening.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT srs_id
        FROM usap_asset
        WHERE srs_id IS NOT NULL
        ORDER BY srs_id
        """
    ).fetchall()

    srs_ids = [int(row["srs_id"]) for row in rows]

    if len(srs_ids) > 1:
        report.add(
            severity="warning",
            code="MIXED_ASSET_CRS",
            message=(
                "Assets are registered in more than one CRS. USAP assumes one "
                "CRS per package and never transforms coordinates, so the "
                "extent layer misplaces every asset not already in the "
                "declared CRS."
            ),
            table="usap_asset",
            details={"srs_ids": srs_ids},
        )


def _validate_asset_extents(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    if not _table_exists(conn, "usap_asset_extent"):
        return  # old package; the layer warning already covers it

    layer_srs_id = None

    if _table_exists(conn, "gpkg_geometry_columns"):
        row = conn.execute(
            """
            SELECT srs_id
            FROM gpkg_geometry_columns
            WHERE table_name = ?
            """,
            (USAP_FEATURES_LAYER,),
        ).fetchone()

        if row is not None:
            layer_srs_id = int(row["srs_id"])

    rows = conn.execute(
        """
        SELECT
            e.asset_id,
            e.geom,
            a.asset_id AS existing_asset_id
        FROM usap_asset_extent AS e
        LEFT JOIN usap_asset AS a
            ON a.asset_id = e.asset_id
        ORDER BY e.asset_id
        """
    ).fetchall()

    for row in rows:
        asset_id = int(row["asset_id"])

        if row["existing_asset_id"] is None:
            report.add(
                severity="error",
                code="ORPHAN_ASSET_EXTENT",
                message="Asset extent references a missing asset.",
                table="usap_asset_extent",
                row_id=asset_id,
            )
            continue

        try:
            envelope = decode_gpkg_envelope(row["geom"])
        except USAPError as exc:
            report.add(
                severity="error",
                code="CORRUPT_EXTENT_BLOB",
                message=f"Could not decode extent geometry blob: {exc}",
                table="usap_asset_extent",
                row_id=asset_id,
            )
            continue

        box = conn.execute(
            """
            SELECT
                MIN(minx) AS minx,
                MIN(miny) AS miny,
                MAX(maxx) AS maxx,
                MAX(maxy) AS maxy
            FROM usap_asset_part
            WHERE asset_id = ?
              AND minx IS NOT NULL
              AND miny IS NOT NULL
              AND maxx IS NOT NULL
              AND maxy IS NOT NULL
            """,
            (asset_id,),
        ).fetchone()

        expected = (
            None
            if box is None or box["minx"] is None
            else {
                "minx": float(box["minx"]),
                "miny": float(box["miny"]),
                "maxx": float(box["maxx"]),
                "maxy": float(box["maxy"]),
            }
        )

        actual = {
            "minx": envelope["minx"],
            "miny": envelope["miny"],
            "maxx": envelope["maxx"],
            "maxy": envelope["maxy"],
        }

        if expected != actual:
            report.add(
                severity="error",
                code="EXTENT_ENVELOPE_MISMATCH",
                message=(
                    "Stored extent envelope does not match the union of the "
                    "asset's part bounds."
                ),
                table="usap_asset_extent",
                row_id=asset_id,
                details={"stored": actual, "expected": expected},
            )

        if layer_srs_id is not None and envelope["srs_id"] != layer_srs_id:
            report.add(
                severity="error",
                code="EXTENT_SRS_MISMATCH",
                message=(
                    "Extent blob srs_id differs from the features layer's "
                    "declared srs_id."
                ),
                table="usap_asset_extent",
                row_id=asset_id,
                details={
                    "blob_srs_id": envelope["srs_id"],
                    "layer_srs_id": layer_srs_id,
                },
            )

    missing = conn.execute(
        """
        SELECT DISTINCT ap.asset_id
        FROM usap_asset_part AS ap
        LEFT JOIN usap_asset_extent AS e
            ON e.asset_id = ap.asset_id
        WHERE ap.minx IS NOT NULL
          AND ap.miny IS NOT NULL
          AND ap.maxx IS NOT NULL
          AND ap.maxy IS NOT NULL
          AND e.asset_id IS NULL
        ORDER BY ap.asset_id
        """
    ).fetchall()

    for row in missing:
        report.add(
            severity="warning",
            code="MISSING_ASSET_EXTENT",
            message=(
                "Asset has bounded parts but no extent row; the features "
                "layer will not show it."
            ),
            table="usap_asset_extent",
            row_id=int(row["asset_id"]),
        )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def _validate_geopackage_metadata(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    header = read_geopackage_header(conn)

    if header["application_id"] != GPKG_APPLICATION_ID:
        report.add(
            severity="error",
            code="INVALID_GPKG_APPLICATION_ID",
            message=(
                "SQLite application_id does not identify the file as GPKG."
            ),
            details={
                "stored": header["application_id"],
                "expected": GPKG_APPLICATION_ID,
            },
        )

    if header["user_version"] < GPKG_USER_VERSION:
        report.add(
            severity="warning",
            code="OLD_GPKG_USER_VERSION",
            message="SQLite user_version is older than the SDK target.",
            details={
                "stored": header["user_version"],
                "expected_minimum": GPKG_USER_VERSION,
            },
        )

    for table_name in [
        "gpkg_spatial_ref_sys",
        "gpkg_contents",
        "gpkg_extensions",
    ]:
        if not _table_exists(conn, table_name):
            report.add(
                severity="error",
                code="MISSING_GPKG_TABLE",
                message=f"Missing GeoPackage metadata table: {table_name}.",
                table=table_name,
            )

    if not _table_exists(conn, "gpkg_spatial_ref_sys"):
        return

    required_srs_ids = [-1, 0, 4326]

    for srs_id in required_srs_ids:
        n = _count(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM gpkg_spatial_ref_sys
            WHERE srs_id = ?
            """,
            (srs_id,),
        )

        if n != 1:
            report.add(
                severity="error",
                code="MISSING_GPKG_SRS",
                message=f"Missing required/default SRS row: {srs_id}.",
                table="gpkg_spatial_ref_sys",
                row_id=srs_id,
            )

    if not _table_exists(conn, "gpkg_extensions"):
        return

    for table_name in USAP_EXTENSION_TABLES:
        n = _count(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM gpkg_extensions
            WHERE table_name = ?
              AND column_name IS NULL
              AND extension_name = ?
            """,
            (table_name, USAP_EXTENSION_NAME),
        )

        if n != 1:
            report.add(
                severity="warning",
                code="MISSING_USAP_EXTENSION_ROW",
                message=(
                    "USAP table is not registered in gpkg_extensions."
                ),
                table="gpkg_extensions",
                details={
                    "usap_table": table_name,
                    "extension_name": USAP_EXTENSION_NAME,
                },
            )
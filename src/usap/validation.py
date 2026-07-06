from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .constants import DEFAULT_ENCODING
from .encoding import decode_u32_zlib
from .geopackage import (
    GPKG_APPLICATION_ID,
    GPKG_USER_VERSION,
    USAP_EXTENSION_NAME,
    USAP_EXTENSION_TABLES,
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


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row["n"])


def validate_connection(conn: sqlite3.Connection) -> ValidationReport:
    """
    Validate a USAP SQLite connection.

    This function intentionally works only from the database connection.
    It does not require geometry files, CityGML, CityJSON, or external assets.

    It validates internal USAP consistency, not external asset correctness.
    """

    report = ValidationReport()

    # The checks read columns by name, so the connection must produce
    # sqlite3.Row rows; restore whatever factory the caller had set.
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row

    try:
        _validate_geopackage_metadata(conn, report)
        _validate_profile(conn, report)
        _validate_semantic_class_registry(conn, report)
        _validate_orphans(conn, report)
        _validate_membership_blocks(conn, report)
        _validate_semantic_class_closure(conn, report)
        _validate_city_object_closure(conn, report)
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
            "ORPHAN_RELATIONSHIP_PARENT",
            "usap_city_object_relationship",
            """
            SELECT COUNT(*) AS n
            FROM usap_city_object_relationship AS r
            LEFT JOIN usap_city_object AS co
                ON co.city_object_id = r.parent_city_object_id
            WHERE co.city_object_id IS NULL
            """,
            "City-object relationship references a missing parent object.",
        ),
        (
            "ORPHAN_RELATIONSHIP_CHILD",
            "usap_city_object_relationship",
            """
            SELECT COUNT(*) AS n
            FROM usap_city_object_relationship AS r
            LEFT JOIN usap_city_object AS co
                ON co.city_object_id = r.child_city_object_id
            WHERE co.city_object_id IS NULL
            """,
            "City-object relationship references a missing child object.",
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
) -> None:
    rows = conn.execute(
        """
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
            mb.payload,

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

        try:
            offsets = decode_u32_zlib(row["payload"])
        except Exception as exc:
            report.add(
                severity="error",
                code="CORRUPT_MEMBERSHIP_PAYLOAD",
                message=f"Could not decode u32-zlib payload: {exc}",
                table="usap_membership_block",
                row_id=block_id,
            )
            continue

        if len(offsets) != declared_element_count:
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
                    "decoded": len(offsets),
                },
            )

        if len(offsets) != len(set(offsets)):
            report.add(
                severity="error",
                code="DUPLICATE_MEMBERSHIP_OFFSETS",
                message="Decoded payload contains duplicate offsets.",
                table="usap_membership_block",
                row_id=block_id,
            )

        if offsets != sorted(offsets):
            report.add(
                severity="warning",
                code="UNSORTED_MEMBERSHIP_OFFSETS",
                message="Decoded payload offsets are not sorted.",
                table="usap_membership_block",
                row_id=block_id,
            )

        for offset in offsets:
            if offset < 0 or offset >= block_size:
                report.add(
                    severity="error",
                    code="OFFSET_OUTSIDE_BLOCK",
                    message="Decoded offset is outside the block range.",
                    table="usap_membership_block",
                    row_id=block_id,
                    details={
                        "offset": offset,
                        "block_size": block_size,
                    },
                )
                break

        if offsets:
            actual_min = block_start + min(offsets)
            actual_max = block_start + max(offsets)

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


def _validate_city_object_closure(
    conn: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    graph_rows = conn.execute(
        """
        SELECT DISTINCT graph_name
        FROM usap_city_object_relationship
        ORDER BY graph_name
        """
    ).fetchall()

    graph_names = [row["graph_name"] for row in graph_rows]

    for graph_name in graph_names:
        missing_self = conn.execute(
            """
            SELECT co.city_object_id
            FROM usap_city_object AS co
            LEFT JOIN usap_city_object_closure AS c
                ON c.graph_name = ?
               AND c.ancestor_city_object_id = co.city_object_id
               AND c.descendant_city_object_id = co.city_object_id
               AND c.depth = 0
            WHERE c.ancestor_city_object_id IS NULL
            """,
            (graph_name,),
        ).fetchall()

        for row in missing_self:
            report.add(
                severity="error",
                code="MISSING_CITY_OBJECT_SELF_CLOSURE",
                message=(
                    "City object is missing depth-0 self closure row for graph."
                ),
                table="usap_city_object_closure",
                row_id=int(row["city_object_id"]),
                details={"graph_name": graph_name},
            )

        missing_direct_edges = conn.execute(
            """
            SELECT
                r.relationship_id,
                r.parent_city_object_id,
                r.child_city_object_id
            FROM usap_city_object_relationship AS r
            LEFT JOIN usap_city_object_closure AS c
                ON c.graph_name = r.graph_name
               AND c.ancestor_city_object_id = r.parent_city_object_id
               AND c.descendant_city_object_id = r.child_city_object_id
               AND c.depth = 1
            WHERE r.graph_name = ?
              AND c.ancestor_city_object_id IS NULL
            """,
            (graph_name,),
        ).fetchall()

        for row in missing_direct_edges:
            report.add(
                severity="error",
                code="MISSING_CITY_OBJECT_CLOSURE_DIRECT",
                message=(
                    "City-object relationship is missing corresponding "
                    "depth-1 closure row."
                ),
                table="usap_city_object_relationship",
                row_id=int(row["relationship_id"]),
                details={
                    "graph_name": graph_name,
                    "parent_city_object_id": int(row["parent_city_object_id"]),
                    "child_city_object_id": int(row["child_city_object_id"]),
                },
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
"""
USAP core SDK — the ``USAPPackage`` class.

``USAPPackage`` is a thin, explicit wrapper over a single SQLite/GeoPackage
file (``*.usap.gpkg``). It owns all common edits and queries. Heavier or
self-contained concerns live in sibling modules and are only orchestrated from
here: membership ``encoding``, GeoPackage header/metadata (``geopackage``),
integrity checks (``validation``), and the file readers (``adapters``).

Data model (each level references — never copies — the one above it)::

    asset            an external file: a .las, a mesh, a .gml      → usap_asset
      asset_part     a stable region whose element indices are valid
                     (all LAS points; one mesh geometry)           → usap_asset_part
        elements     individual point / face indices inside a part
          membership the exact indices an annotation covers, stored
                     as zlib-compressed uint32 offset blocks        → usap_membership_block
            annotation  an editable claim: concept + status + attrs → usap_annotation
              semantic_class  what kind of thing it is (RoofSurface) → usap_semantic_class
              city_object     which object it is (building_1_roof_1) → usap_city_object

Two independent hierarchies are kept as transitive-closure tables so
"this class and its subclasses" / "this object and its descendants" are single
indexed lookups instead of recursive walks:

    usap_semantic_class_closure   class → subclass (parentage from vocabularies)
    usap_city_object_closure      object → descendant, per named graph

One annotation can carry membership in several asset parts at once (LAS points
*and* mesh faces *and* a triangulation), which is what makes a claim
"cross-representation".

Conventions used throughout this file:

  * Idempotent creates. ``register_asset``, ``create_semantic_class``,
    ``create_city_object``, ``create_annotation`` etc. look up the natural key
    first (``uri+hash``, ``class_uri``, ``object_uid``, ``annotation_uid``) and
    return the existing id instead of inserting a duplicate.
  * Nested transactions. ``transaction()`` is re-entrant: the outermost ``with``
    block commits/rolls back, inner ones are no-ops. This lets each public
    method be safe when called alone yet cheap when batched.
  * Every write appends a row to ``usap_edit_log`` via ``log_edit``.
  * Element kinds arrive as friendly strings ("point"/"face") and are coerced to
    integer constants by ``normalize_element_kind``.

Method map (matches the ``# ---`` section banners below):

    Opening / creating ......... create, open, close, context manager
    Small internal helpers ..... get_default_block_size, log_edit
    Assets ..................... register_asset, register_asset_part
    Semantic classes ........... create_semantic_class (+ closure maintenance)
    City objects and graph ..... create_city_object, link_city_objects,
                                  rebuild_city_object_closure
    Annotations ................ get/list/update/delete_annotation,
                                  create_annotation, link_annotation_to_object
    Membership editing ......... replace_annotation_membership (+ validation)
    Queries .................... annotations_for_elements (reverse: elements →
                                  annotations), elements_for_annotation /
                                  _semantic_class / _city_object (forward)
    Value fields ............... annotate_value_field, replace_value_field,
                                  values_for_annotation, elements_where,
                                  value_field_stats (dense per-element scalar
                                  fields, asset-bound — never city-object-bound)
    Validation ................. validate_report
    Concept-level API .......... resolve_semantic_class / resolve_city_object /
                                  resolve_asset_part, create_concept_annotation,
                                  annotate_elements, attach_annotation_elements
                                  (the high-level entry points most callers use)
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict, deque
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any
import json
import uuid

import numpy as np

from .errors import USAPAmbiguityError, USAPError
from .constants import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_ENCODING,
    DEFAULT_GRAPH_NAME,
    DEFAULT_VALUE_DTYPE,
    VALUE_CHUNK_SIZE,
    VALUE_DTYPES,
    normalize_element_kind,
    normalize_value_dtype,
)
from .encoding import (
    block_start_for_index,
    decode_u32_zlib,
    decode_value_block,
    encode_u32_zlib,
    encode_value_block,
    split_indices_into_blocks,
)
from .sqlite_utils import require_lastrowid
from .validation import validate_connection
from .geopackage import initialize_geopackage_metadata

_UNSET = object()

# Anchored to the repo root (src/usap/ -> repo), not the process CWD, so the
# default schema loads no matter where the caller runs from.
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"

# SQLite binds one variable per IN-list member and its variable limit can be
# as low as 999, so large id lists are queried in chunks of this size.
_MAX_SQL_IN_VARS = 900

_COMPARISON_OPS = {
    ">": np.greater,
    ">=": np.greater_equal,
    "<": np.less,
    "<=": np.less_equal,
    "==": np.equal,
    "!=": np.not_equal,
}


def _block_cannot_match(
    op: str,
    threshold: float,
    value_min: float | None,
    value_max: float | None,
) -> bool:
    """
    True when a value block's stored NaN-ignoring [min, max] range proves the
    comparison cannot match any element (NaN never matches any predicate).
    """
    if value_min is None or value_max is None:
        return True  # all-NaN block

    if op == ">":
        return value_max <= threshold
    if op == ">=":
        return value_max < threshold
    if op == "<":
        return value_min >= threshold
    if op == "<=":
        return value_min > threshold
    if op == "==":
        return threshold < value_min or threshold > value_max
    # "!=": every real value equals the threshold -> nothing can differ.
    return value_min == value_max == threshold


def _check_value_cast(
    array: np.ndarray,
    target_dtype: np.dtype,
    value_dtype: str,
) -> None:
    """
    Reject casts that would silently corrupt values: wraparound/truncation
    into integer dtypes, or finite values overflowing to inf in a narrower
    float dtype. Plain precision rounding (f8 -> f4) is inherent to the
    requested dtype and stays allowed.
    """
    if array.size == 0 or array.dtype.kind not in "buif":
        return

    if target_dtype.kind in "ui":
        if array.dtype.kind == "f" and not bool(np.isfinite(array).all()):
            raise USAPError(
                f"Non-finite values are not representable in integer "
                f"value_dtype {value_dtype!r}. Use a float dtype."
            )

        info = np.iinfo(target_dtype)
        lo = array.min()
        hi = array.max()

        if lo < info.min or hi > info.max:
            raise USAPError(
                f"Value field has values outside the {value_dtype!r} range "
                f"[{info.min}, {info.max}]: min {lo}, max {hi}. "
                "They would wrap around silently."
            )

        if array.dtype.kind == "f" and bool((np.floor(array) != array).any()):
            raise USAPError(
                f"Value field has non-integral values; integer value_dtype "
                f"{value_dtype!r} would truncate them. Use a float dtype."
            )
    elif array.dtype != target_dtype:
        finite = array

        if array.dtype.kind == "f":
            finite = array[np.isfinite(array)]

        if finite.size:
            magnitude = max(abs(float(finite.min())), abs(float(finite.max())))

            if magnitude > float(np.finfo(target_dtype).max):
                raise USAPError(
                    f"Value field has finite values beyond the "
                    f"{value_dtype!r} range "
                    f"(max magnitude {np.finfo(target_dtype).max}); "
                    "the cast would produce inf."
                )


class USAPPackage:
    """
    Tiny phase-1 USAP SDK. It controls common database edits and queries.
    """

    def __init__(self, db_path: str | Path): # Constructor of the package
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._transaction_depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """
        Open a write transaction.

        If already inside a USAP transaction, this becomes a no-op nested
        transaction. This lets SDK methods be safe when called individually,
        but also fast when many edits are grouped together.

        If a transaction is already open on the connection from raw writes on
        ``pkg.conn`` (sqlite3 starts one implicitly), the outermost block
        adopts it: no ``BEGIN`` is issued, but the block still commits or
        rolls back at exit, raw writes included. Otherwise those writes would
        silently be rolled back when the package is closed.

        Example:

            with pkg.transaction():
                pkg.create_city_object(...)
                pkg.create_annotation(...)
                pkg.replace_annotation_membership(...)
        """
        if self._transaction_depth > 0:
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1
            return

        self._transaction_depth = 1

        try:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        finally:
            self._transaction_depth = 0

    # ---------------------------------------------------------------------
    # Opening / creating
    # ---------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        db_path: str | Path,
        schema_path: str | Path = DEFAULT_SCHEMA_PATH,
        overwrite: bool = False,
        profile_version: str = "0.1.0",
    ) -> "USAPPackage":
        db_path = Path(db_path)
        schema_path = Path(schema_path)

        if db_path.exists():
            if overwrite:
                os.remove(db_path)
            else:
                raise USAPError(f"Database already exists: {db_path}")

        if not schema_path.exists():
            raise USAPError(f"Schema file not found: {schema_path}")

        pkg = cls(db_path)

        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        pkg.conn.executescript(schema_sql)

        with pkg.transaction():
            initialize_geopackage_metadata(
                pkg.conn,
                profile_version=profile_version,
            )

            pkg.conn.execute(
                """
                INSERT INTO usap_profile (
                    profile_id,
                    profile_version,
                    default_block_size,
                    default_encoding,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    1,
                    profile_version,
                    DEFAULT_BLOCK_SIZE,
                    DEFAULT_ENCODING,
                    None,
                ),
            )

        return pkg
    
    @classmethod
    def open(cls, db_path: str | Path) -> "USAPPackage":
        db_path = Path(db_path)

        if not db_path.exists():
            raise USAPError(f"Database does not exist: {db_path}")

        return cls(db_path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "USAPPackage":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    # ---------------------------------------------------------------------
    # Small internal helpers
    # ---------------------------------------------------------------------

    def get_default_block_size(self) -> int:
        row = self.conn.execute(
            """
            SELECT default_block_size
            FROM usap_profile
            WHERE profile_id = 1
            """
        ).fetchone()

        if row is None:
            return DEFAULT_BLOCK_SIZE

        return int(row["default_block_size"])

    def log_edit(
        self,
        operation: str,
        target_table: str | None = None,
        target_id: int | None = None,
        details_json: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO usap_edit_log (
                operation,
                target_table,
                target_id,
                details_json
            )
            VALUES (?, ?, ?, ?)
            """,
            (operation, target_table, target_id, details_json),
        )

    # ---------------------------------------------------------------------
    # Assets
    # ---------------------------------------------------------------------

    def register_asset(
        self,
        uri: str,
        asset_kind: str,
        media_type: str | None = None,
        content_hash: str | None = None,
        srs_id: int | None = None,
        metadata_json: str | None = None,
    ) -> int:
        """
        Register an external asset.

        Returns asset_id.
        """
        existing = self.conn.execute(
            """
            SELECT asset_id
            FROM usap_asset
            WHERE uri = ?
              AND content_hash IS ?
            """,
            (uri, content_hash),
        ).fetchone()

        if existing is not None:
            return int(existing["asset_id"])

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_asset (
                    uri,
                    asset_kind,
                    media_type,
                    content_hash,
                    srs_id,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    uri,
                    asset_kind,
                    media_type,
                    content_hash,
                    srs_id,
                    metadata_json,
                ),
            )
            asset_id = require_lastrowid(cur)
            self.log_edit("register_asset", "usap_asset", asset_id)

        return asset_id

    def register_asset_part(
        self,
        asset_id: int,
        part_path: str,
        element_kind: int,
        element_count: int,
        index_origin: str = "zero_based",
        minx: float | None = None,
        miny: float | None = None,
        minz: float | None = None,
        maxx: float | None = None,
        maxy: float | None = None,
        maxz: float | None = None,
        metadata_json: str | None = None,
    ) -> int:
        """
        Register a stable sub-location inside an asset.

        Returns asset_part_id.
        """
        element_kind = normalize_element_kind(element_kind)
        if element_count < 0:
            raise USAPError("element_count cannot be negative")

        existing = self.conn.execute(
            """
            SELECT asset_part_id
            FROM usap_asset_part
            WHERE asset_id = ?
              AND part_path = ?
              AND element_kind = ?
            """,
            (asset_id, part_path, element_kind),
        ).fetchone()

        if existing is not None:
            return int(existing["asset_part_id"])

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_asset_part (
                    asset_id,
                    part_path,
                    element_kind,
                    element_count,
                    index_origin,
                    minx,
                    miny,
                    minz,
                    maxx,
                    maxy,
                    maxz,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    part_path,
                    element_kind,
                    element_count,
                    index_origin,
                    minx,
                    miny,
                    minz,
                    maxx,
                    maxy,
                    maxz,
                    metadata_json,
                ),
            )
            asset_part_id = require_lastrowid(cur)
            self.log_edit(
                "register_asset_part",
                "usap_asset_part",
                asset_part_id,
            )

        return asset_part_id

    # ---------------------------------------------------------------------
    # Semantic classes
    # ---------------------------------------------------------------------

    def create_semantic_class(
        self,
        scheme: str,
        class_uri: str,
        local_name: str,
        scheme_version: str | None = None,
        parent_class_id: int | None = None,
        is_ade: bool = False,
        metadata_json: str | None = None,
    ) -> int:
        """
        Create or reuse a semantic class.

        Also updates usap_semantic_class_closure.
        """
        existing = self.conn.execute(
            """
            SELECT semantic_class_id, parent_class_id
            FROM usap_semantic_class
            WHERE class_uri = ?
            """,
            (class_uri,),
        ).fetchone()

        if existing is not None:
            existing_parent_id = existing["parent_class_id"]

            # A requested None parent makes no claim, so plain re-creates and
            # re-seeding stay idempotent; only a contradicting parent raises.
            if (
                parent_class_id is not None
                and existing_parent_id != parent_class_id
            ):
                raise USAPError(
                    f"Semantic class already exists with a different parent: "
                    f"{class_uri!r} has parent_class_id {existing_parent_id}, "
                    f"requested {parent_class_id}. "
                    "Changing a concept's parent is not supported yet."
                )

            return int(existing["semantic_class_id"])

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_semantic_class (
                    scheme,
                    scheme_version,
                    class_uri,
                    local_name,
                    parent_class_id,
                    is_ade,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scheme,
                    scheme_version,
                    class_uri,
                    local_name,
                    parent_class_id,
                    1 if is_ade else 0,
                    metadata_json,
                ),
            )
            class_id = require_lastrowid(cur)

            # Every class is its own descendant.
            self.conn.execute(
                """
                INSERT INTO usap_semantic_class_closure (
                    ancestor_class_id,
                    descendant_class_id,
                    depth
                )
                VALUES (?, ?, ?)
                """,
                (class_id, class_id, 0),
            )

            # If the class has a parent, inherit all parent ancestors.
            if parent_class_id is not None:
                parent_ancestors = self.conn.execute(
                    """
                    SELECT ancestor_class_id, depth
                    FROM usap_semantic_class_closure
                    WHERE descendant_class_id = ?
                    """,
                    (parent_class_id,),
                ).fetchall()

                for row in parent_ancestors:
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO usap_semantic_class_closure (
                            ancestor_class_id,
                            descendant_class_id,
                            depth
                        )
                        VALUES (?, ?, ?)
                        """,
                        (
                            int(row["ancestor_class_id"]),
                            class_id,
                            int(row["depth"]) + 1,
                        ),
                    )

            self.log_edit(
                "create_semantic_class",
                "usap_semantic_class",
                class_id,
            )

        return class_id

    # ---------------------------------------------------------------------
    # City objects and graph
    # ---------------------------------------------------------------------

    def create_city_object(
        self,
        object_uid: str,
        semantic_class_id: int | None = None,
        gml_id: str | None = None,
        source_asset_id: int | None = None,
        source_object_id: str | None = None,
        object_status: str = "accepted",
        attributes_json: str | None = None,
    ) -> int:
        existing = self.conn.execute(
            """
            SELECT city_object_id
            FROM usap_city_object
            WHERE object_uid = ?
            """,
            (object_uid,),
        ).fetchone()

        if existing is not None:
            return int(existing["city_object_id"])

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_city_object (
                    object_uid,
                    semantic_class_id,
                    gml_id,
                    source_asset_id,
                    source_object_id,
                    object_status,
                    attributes_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    object_uid,
                    semantic_class_id,
                    gml_id,
                    source_asset_id,
                    source_object_id,
                    object_status,
                    attributes_json,
                ),
            )
            city_object_id = require_lastrowid(cur)
            self.log_edit(
                "create_city_object",
                "usap_city_object",
                city_object_id,
            )

        return city_object_id

    def link_city_objects(
        self,
        parent_city_object_id: int,
        child_city_object_id: int,
        relationship_type: str,
        role: str | None = None,
        graph_name: str = DEFAULT_GRAPH_NAME,
        source_asset_id: int | None = None,
        source_relation_id: str | None = None,
        metadata_json: str | None = None,
        rebuild_closure: bool = True,
    ) -> int:
        """
        Add one typed edge between two city objects.
        """
        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_city_object_relationship (
                    graph_name,
                    parent_city_object_id,
                    child_city_object_id,
                    relationship_type,
                    role,
                    source_asset_id,
                    source_relation_id,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_name,
                    parent_city_object_id,
                    child_city_object_id,
                    relationship_type,
                    role,
                    source_asset_id,
                    source_relation_id,
                    metadata_json,
                ),
            )
            relationship_id = require_lastrowid(cur)

            self.log_edit(
                "link_city_objects",
                "usap_city_object_relationship",
                relationship_id,
            )

        if rebuild_closure:
            self.rebuild_city_object_closure(graph_name=graph_name)

        return relationship_id

    def rebuild_city_object_closure(
        self,
        graph_name: str = DEFAULT_GRAPH_NAME,
    ) -> None:
        """
        Rebuild the ancestor/descendant closure for one named graph.

        The closure exists to accelerate annotation retrieval: it lets
        elements_for_city_object answer "annotations for this object and its
        parts" with a single indexed lookup instead of walking the relationship
        edges. usap_default is the graph those queries traverse; the several
        named graphs are mainly plumbing for mirroring CityGML structure, not a
        general-purpose graph database.
        """
        objects = self.conn.execute(
            """
            SELECT city_object_id
            FROM usap_city_object
            """
        ).fetchall()

        object_ids = [int(row["city_object_id"]) for row in objects]

        edges = self.conn.execute(
            """
            SELECT parent_city_object_id, child_city_object_id
            FROM usap_city_object_relationship
            WHERE graph_name = ?
            """,
            (graph_name,),
        ).fetchall()

        children_by_parent: dict[int, list[int]] = defaultdict(list)

        for row in edges:
            parent_id = int(row["parent_city_object_id"])
            child_id = int(row["child_city_object_id"])
            children_by_parent[parent_id].append(child_id)

        closure_rows: list[tuple[str, int, int, int]] = []

        for start_id in object_ids:
            closure_rows.append((graph_name, start_id, start_id, 0))

            queue = deque([(start_id, 0)])
            visited = {start_id}

            while queue:
                current_id, current_depth = queue.popleft()

                for child_id in children_by_parent.get(current_id, []):
                    if child_id in visited:
                        continue

                    visited.add(child_id)

                    depth = current_depth + 1
                    closure_rows.append((graph_name, start_id, child_id, depth))
                    queue.append((child_id, depth))

        with self.transaction():
            self.conn.execute(
                """
                DELETE FROM usap_city_object_closure
                WHERE graph_name = ?
                """,
                (graph_name,),
            )

            self.conn.executemany(
                """
                INSERT INTO usap_city_object_closure (
                    graph_name,
                    ancestor_city_object_id,
                    descendant_city_object_id,
                    depth
                )
                VALUES (?, ?, ?, ?)
                """,
                closure_rows,
            )

            self.log_edit(
                "rebuild_city_object_closure",
                "usap_city_object_closure",
                None,
                f'{{"graph_name": "{graph_name}"}}',
            )

    # ---------------------------------------------------------------------
    # Annotations
    # ---------------------------------------------------------------------

    def get_annotation(
        self,
        annotation_id: int | None = None,
        *,
        annotation_uid: str | None = None,
        include_membership_summary: bool = True,
    ) -> dict[str, Any] | None:
        """
        Read one annotation by id or uid.

        Returns None if no annotation is found.

        include_membership_summary=True also attaches value_field_summary
        (both are per-asset-part payload rollups).
        """
        if annotation_id is None and annotation_uid is None:
            raise USAPError("Provide annotation_id or annotation_uid.")

        if annotation_id is not None and annotation_uid is not None:
            raise USAPError("Provide only one of annotation_id or annotation_uid.")

        where_sql = "a.annotation_id = ?"
        params: list[Any] = [annotation_id]

        if annotation_uid is not None:
            where_sql = "a.annotation_uid = ?"
            params = [annotation_uid]

        row = self.conn.execute(
            f"""
            SELECT
                a.annotation_id,
                a.annotation_uid,
                a.semantic_class_id,
                sc.scheme AS semantic_scheme,
                sc.scheme_version AS semantic_scheme_version,
                sc.class_uri AS semantic_class_uri,
                sc.local_name AS semantic_class,
                sc.is_ade AS semantic_is_ade,
                a.primary_city_object_id,
                co.object_uid AS primary_city_object_uid,
                co.gml_id AS primary_city_object_gml_id,
                a.label,
                a.status,
                a.confidence,
                a.attributes_json
            FROM usap_annotation AS a
            JOIN usap_semantic_class AS sc
                ON sc.semantic_class_id = a.semantic_class_id
            LEFT JOIN usap_city_object AS co
                ON co.city_object_id = a.primary_city_object_id
            WHERE {where_sql}
            """,
            params,
        ).fetchone()

        if row is None:
            return None

        result = dict(row)

        if include_membership_summary:
            result["membership_summary"] = self._annotation_membership_summary(
                int(result["annotation_id"])
            )
            result["value_field_summary"] = self._annotation_value_field_summary(
                int(result["annotation_id"])
            )

        return result


    def list_annotations(
        self,
        *,
        status: str | None = None,
        semantic_class_id: int | None = None,
        semantic_class_local_name: str | None = None,
        city_object_id: int | None = None,
        city_object_uid: str | None = None,
        include_membership_summary: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        List annotations using simple prototype filters.

        Filters are AND-combined.

        city_object_id / city_object_uid matches either:
        - annotation.primary_city_object_id
        - usap_annotation_object links

        include_membership_summary=True also attaches value_field_summary
        (both are per-asset-part payload rollups).
        """
        where: list[str] = []
        params: list[Any] = []

        if status is not None:
            where.append("a.status = ?")
            params.append(status)

        if semantic_class_id is not None:
            where.append("a.semantic_class_id = ?")
            params.append(semantic_class_id)

        if semantic_class_local_name is not None:
            where.append("sc.local_name = ?")
            params.append(semantic_class_local_name)

        if city_object_id is not None:
            where.append(
                """
                (
                    a.primary_city_object_id = ?
                    OR EXISTS (
                        SELECT 1
                        FROM usap_annotation_object AS ao
                        WHERE ao.annotation_id = a.annotation_id
                        AND ao.city_object_id = ?
                    )
                )
                """
            )
            params.extend([city_object_id, city_object_id])

        if city_object_uid is not None:
            where.append(
                """
                (
                    primary_co.object_uid = ?
                    OR EXISTS (
                        SELECT 1
                        FROM usap_annotation_object AS ao
                        JOIN usap_city_object AS linked_co
                            ON linked_co.city_object_id = ao.city_object_id
                        WHERE ao.annotation_id = a.annotation_id
                        AND linked_co.object_uid = ?
                    )
                )
                """
            )
            params.extend([city_object_uid, city_object_uid])

        where_sql = ""

        if where:
            where_sql = "WHERE " + " AND ".join(where)

        limit_sql = ""

        if limit is not None:
            if limit <= 0:
                raise USAPError("limit must be positive when provided.")

            limit_sql = "LIMIT ?"
            params.append(limit)

        rows = self.conn.execute(
            f"""
            SELECT DISTINCT
                a.annotation_id,
                a.annotation_uid,
                a.semantic_class_id,
                sc.scheme AS semantic_scheme,
                sc.scheme_version AS semantic_scheme_version,
                sc.class_uri AS semantic_class_uri,
                sc.local_name AS semantic_class,
                sc.is_ade AS semantic_is_ade,
                a.primary_city_object_id,
                primary_co.object_uid AS primary_city_object_uid,
                primary_co.gml_id AS primary_city_object_gml_id,
                a.label,
                a.status,
                a.confidence,
                a.attributes_json
            FROM usap_annotation AS a
            JOIN usap_semantic_class AS sc
                ON sc.semantic_class_id = a.semantic_class_id
            LEFT JOIN usap_city_object AS primary_co
                ON primary_co.city_object_id = a.primary_city_object_id
            {where_sql}
            ORDER BY a.annotation_id
            {limit_sql}
            """,
            params,
        ).fetchall()

        result = [dict(row) for row in rows]

        if include_membership_summary:
            for item in result:
                item["membership_summary"] = self._annotation_membership_summary(
                    int(item["annotation_id"])
                )
                item["value_field_summary"] = self._annotation_value_field_summary(
                    int(item["annotation_id"])
                )

        return result


    def update_annotation(
        self,
        annotation_id: int,
        *,
        annotation_uid: object = _UNSET,
        semantic_class_id: object = _UNSET,
        primary_city_object_id: object = _UNSET,
        label: object = _UNSET,
        status: object = _UNSET,
        confidence: object = _UNSET,
        attributes_json: object = _UNSET,
    ) -> dict[str, Any]:
        """
        Update annotation metadata.

        Omitted fields are preserved.
        Passing None explicitly stores NULL.
        """
        updates: list[str] = []
        params: list[Any] = []

        def add_update(column: str, value: object) -> None:
            if value is _UNSET:
                return

            updates.append(f"{column} = ?")
            params.append(value)

        add_update("annotation_uid", annotation_uid)
        add_update("semantic_class_id", semantic_class_id)
        add_update("primary_city_object_id", primary_city_object_id)
        add_update("label", label)
        add_update("status", status)
        add_update("confidence", confidence)
        add_update("attributes_json", attributes_json)

        if not updates:
            existing = self.get_annotation(annotation_id)

            if existing is None:
                raise USAPError(f"Annotation not found: {annotation_id}")

            return existing

        # Keep updated_at meaningful: it is set to CURRENT_TIMESTAMP on creation
        # and must advance on every real edit, otherwise it just mirrors
        # created_at. CURRENT_TIMESTAMP is inline SQL, so it needs no parameter.
        updates.append("updated_at = CURRENT_TIMESTAMP")

        params.append(annotation_id)

        with self.transaction():
            try:
                cur = self.conn.execute(
                    f"""
                    UPDATE usap_annotation
                    SET {", ".join(updates)}
                    WHERE annotation_id = ?
                    """,
                    params,
                )
            except sqlite3.IntegrityError as exc:
                raise USAPError(
                    f"Annotation update violates a constraint: {exc}"
                ) from exc

            if cur.rowcount != 1:
                raise USAPError(f"Annotation not found: {annotation_id}")

            self.log_edit("update_annotation", "usap_annotation", annotation_id)

        updated = self.get_annotation(annotation_id)

        if updated is None:
            raise USAPError(f"Annotation disappeared after update: {annotation_id}")

        return updated


    def delete_annotation(
        self,
        annotation_id: int,
        *,
        missing_ok: bool = False,
    ) -> bool:
        """
        Delete an annotation.

        Membership blocks and annotation-object links should be removed by
        ON DELETE CASCADE.

        Returns True if an annotation was deleted.
        Returns False only when missing_ok=True and the annotation did not exist.
        """
        with self.transaction():
            cur = self.conn.execute(
                """
                DELETE FROM usap_annotation
                WHERE annotation_id = ?
                """,
                (annotation_id,),
            )

            if cur.rowcount == 0:
                if missing_ok:
                    return False

                raise USAPError(f"Annotation not found: {annotation_id}")

            self.log_edit("delete_annotation", "usap_annotation", annotation_id)

        return True


    def _annotation_membership_summary(
        self,
        annotation_id: int,
    ) -> list[dict[str, Any]]:
        """
        Summarize which asset parts an annotation touches.
        """
        rows = self.conn.execute(
            """
            SELECT
                mb.asset_part_id,
                ap.asset_id,
                asset.uri AS asset_uri,
                asset.asset_kind,
                ap.part_path,
                ap.element_kind,
                SUM(mb.element_count) AS selected_count,
                COUNT(*) AS block_count
            FROM usap_membership_block AS mb
            JOIN usap_asset_part AS ap
                ON ap.asset_part_id = mb.asset_part_id
            JOIN usap_asset AS asset
                ON asset.asset_id = ap.asset_id
            WHERE mb.annotation_id = ?
            GROUP BY
                mb.asset_part_id,
                ap.asset_id,
                asset.uri,
                asset.asset_kind,
                ap.part_path,
                ap.element_kind
            ORDER BY mb.asset_part_id
            """,
            (annotation_id,),
        ).fetchall()

        return [dict(row) for row in rows]


    def create_annotation(
        self,
        annotation_uid: str,
        semantic_class_id: int,
        primary_city_object_id: int | None = None,
        label: str | None = None,
        status: str = "accepted",
        confidence: float | None = None,
        attributes_json: str | None = None,
        link_primary_object: bool = True,
    ) -> int:
        existing = self.conn.execute(
            """
            SELECT annotation_id, semantic_class_id
            FROM usap_annotation
            WHERE annotation_uid = ?
            """,
            (annotation_uid,),
        ).fetchone()

        if existing is not None:
            existing_class_id = int(existing["semantic_class_id"])

            if existing_class_id != semantic_class_id:
                raise USAPError(
                    f"Annotation already exists with a different semantic class: "
                    f"{annotation_uid!r} has semantic_class_id {existing_class_id}, "
                    f"requested {semantic_class_id}. "
                    "Use update_annotation to change its class."
                )

            return int(existing["annotation_id"])

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_annotation (
                    annotation_uid,
                    semantic_class_id,
                    primary_city_object_id,
                    label,
                    status,
                    confidence,
                    attributes_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    annotation_uid,
                    semantic_class_id,
                    primary_city_object_id,
                    label,
                    status,
                    confidence,
                    attributes_json,
                ),
            )

            annotation_id = require_lastrowid(cur)

            if primary_city_object_id is not None and link_primary_object:
                self._link_annotation_object(annotation_id, primary_city_object_id, "represents")

            self.log_edit(
                "create_annotation",
                "usap_annotation",
                annotation_id,
            )

        return annotation_id

    def link_annotation_to_object(
        self,
        annotation_id: int,
        city_object_id: int,
        relation_type: str = "represents",
    ) -> None:
        with self.transaction():
            self._link_annotation_object(annotation_id, city_object_id, relation_type)
            self.log_edit(
                "link_annotation_to_object",
                "usap_annotation_object",
                annotation_id,
            )

    # ---------------------------------------------------------------------
    # Membership editing
    # ---------------------------------------------------------------------

    def _validate_membership_indices(
        self,
        asset_part_id: int,
        element_kind: int,
        element_indices: list[int],
    ) -> list[int]:
        asset_part = self.conn.execute(
            """
            SELECT element_kind, element_count
            FROM usap_asset_part
            WHERE asset_part_id = ?
            """,
            (asset_part_id,),
        ).fetchone()

        if asset_part is None:
            raise USAPError(f"Unknown asset_part_id: {asset_part_id}")

        expected_kind = int(asset_part["element_kind"])
        element_count = int(asset_part["element_count"])

        if element_kind != expected_kind:
            raise USAPError(
                f"Element kind mismatch: asset part expects "
                f"{expected_kind}, got {element_kind}"
            )

        unique_indices = sorted(set(element_indices))

        # The list is sorted, so checking both ends covers every index.
        if unique_indices:
            if unique_indices[0] < 0:
                raise USAPError(f"Negative element index: {unique_indices[0]}")
            if unique_indices[-1] >= element_count:
                raise USAPError(
                    f"Element index {unique_indices[-1]} is out of range. "
                    f"Asset part has {element_count} elements."
                )

        return unique_indices

    def replace_annotation_membership(
        self,
        annotation_id: int,
        asset_part_id: int,
        element_kind: int,
        element_indices: list[int],
        encoding: str = DEFAULT_ENCODING,
    ) -> None:
        """
        Replace all membership blocks for one annotation in one asset part.

        This is an edit operation, so we validate first and write in a transaction.

        Blocks are always sized at the package default
        (usap_profile.default_block_size). The reverse query
        annotations_for_elements derives block boundaries from that single
        global size, so membership must not be written at any other size or the
        reverse lookup would silently miss it.
        """
        element_kind = normalize_element_kind(element_kind)
        if encoding != "u32-zlib":
            raise USAPError(f"Unsupported encoding in phase 1: {encoding}")

        annotation = self.conn.execute(
            """
            SELECT annotation_id
            FROM usap_annotation
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        if annotation is None:
            raise USAPError(f"Annotation not found: {annotation_id}")

        block_size = self.get_default_block_size()

        unique_indices = self._validate_membership_indices(
            asset_part_id, element_kind, element_indices
        )

        blocks = split_indices_into_blocks(unique_indices, block_size)

        with self.transaction():
            self.conn.execute(
                """
                DELETE FROM usap_membership_block
                WHERE annotation_id = ?
                AND asset_part_id = ?
                AND element_kind = ?
                """,
                (
                    annotation_id,
                    asset_part_id,
                    element_kind,
                ),
            )

            for block_start, offsets in blocks.items():
                payload = encode_u32_zlib(offsets)
                min_element_index = block_start + min(offsets)
                max_element_index = block_start + max(offsets)

                self.conn.execute(
                    """
                    INSERT INTO usap_membership_block (
                        annotation_id,
                        asset_part_id,
                        element_kind,
                        block_start,
                        block_size,
                        encoding,
                        element_count,
                        min_element_index,
                        max_element_index,
                        payload
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        annotation_id,
                        asset_part_id,
                        element_kind,
                        block_start,
                        block_size,
                        encoding,
                        len(offsets),
                        min_element_index,
                        max_element_index,
                        payload,
                    ),
                )

            self.log_edit(
                "replace_annotation_membership",
                "usap_annotation",
                annotation_id,
                f'{{"asset_part_id": {asset_part_id}, '
                f'"element_kind": {element_kind}, '
                f'"element_count": {len(unique_indices)}}}',
            )

    def _link_annotation_object(
        self,
        annotation_id: int,
        city_object_id: int,
        relation_type: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO usap_annotation_object (
                annotation_id,
                city_object_id,
                relation_type
            )
            VALUES (?, ?, ?)
            """,
            (annotation_id, city_object_id, relation_type),
        )

    # ---------------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------------
    def _membership_block_row_to_dict(
        self,
        row: sqlite3.Row,
        expand: bool,
    ) -> dict[str, Any]:
        item = {
            "membership_block_id": int(row["membership_block_id"]),
            "annotation_id": int(row["annotation_id"]),
            "asset_part_id": int(row["asset_part_id"]),
            "element_kind": int(row["element_kind"]),
            "block_start": int(row["block_start"]),
            "block_size": int(row["block_size"]),
            "encoding": row["encoding"],
            "element_count": int(row["element_count"]),
            "min_element_index": int(row["min_element_index"]),
            "max_element_index": int(row["max_element_index"]),
        }

        if expand:
            offsets = decode_u32_zlib(row["payload"])
            item["elements"] = [
                int(row["block_start"]) + offset
                for offset in offsets
            ]

        return item


    def annotations_for_elements(
        self,
        asset_part_id: int,
        element_kind: int,
        selected_indices: list[int],
    ) -> list[dict[str, Any]]:
        """
        Core reverse query:

            selected faces/points -> annotations

        Optimized version:
        - groups selected indices by block_start
        - queries all relevant block_start values in one SQL query
        - decodes only candidate membership blocks
        """
        element_kind = normalize_element_kind(element_kind)
        if not selected_indices:
            return []

        block_size = self.get_default_block_size()

        selected_by_block: dict[int, set[int]] = defaultdict(set)

        for index in selected_indices:
            if index < 0:
                raise USAPError(f"Negative selected index: {index}")

            block_start = block_start_for_index(index, block_size)
            offset = index - block_start
            selected_by_block[block_start].add(offset)

        block_starts = sorted(selected_by_block)

        # Chunked so a huge selection cannot exceed the SQLite variable limit.
        # Chunks partition block_starts, so each candidate block row appears
        # exactly once and the merge below is unaffected.
        rows: list[sqlite3.Row] = []

        for chunk_index in range(0, len(block_starts), _MAX_SQL_IN_VARS):
            chunk = block_starts[chunk_index : chunk_index + _MAX_SQL_IN_VARS]
            placeholders = ",".join("?" for _ in chunk)

            rows.extend(
                self.conn.execute(
                    f"""
                    SELECT
                        mb.annotation_id,
                        mb.block_start,
                        mb.payload,

                        a.annotation_uid,
                        a.label,
                        a.status,

                        sc.local_name AS semantic_class,
                        sc.class_uri AS semantic_class_uri,

                        co.object_uid AS primary_city_object_uid
                    FROM usap_membership_block AS mb
                    JOIN usap_annotation AS a
                        ON a.annotation_id = mb.annotation_id
                    JOIN usap_semantic_class AS sc
                        ON sc.semantic_class_id = a.semantic_class_id
                    LEFT JOIN usap_city_object AS co
                        ON co.city_object_id = a.primary_city_object_id
                    WHERE mb.asset_part_id = ?
                    AND mb.element_kind = ?
                    AND mb.block_start IN ({placeholders})
                    ORDER BY
                        mb.block_start,
                        mb.annotation_id
                    """,
                    (
                        asset_part_id,
                        element_kind,
                        *chunk,
                    ),
                ).fetchall()
            )

        matches_by_annotation: dict[int, dict[str, Any]] = {}

        for row in rows:
            block_start = int(row["block_start"])
            selected_offsets = selected_by_block[block_start]

            encoded_offsets = set(decode_u32_zlib(row["payload"]))
            hit_offsets = selected_offsets.intersection(encoded_offsets)

            if not hit_offsets:
                continue

            annotation_id = int(row["annotation_id"])

            if annotation_id not in matches_by_annotation:
                matches_by_annotation[annotation_id] = {
                    "annotation_id": annotation_id,
                    "annotation_uid": row["annotation_uid"],
                    "label": row["label"],
                    "status": row["status"],
                    "semantic_class": row["semantic_class"],
                    "semantic_class_uri": row["semantic_class_uri"],
                    "primary_city_object_uid": row["primary_city_object_uid"],
                    "matched_elements": [],
                }

            absolute_hits = [
                block_start + offset
                for offset in sorted(hit_offsets)
            ]

            matches_by_annotation[annotation_id]["matched_elements"].extend(
                absolute_hits
            )

        results = list(matches_by_annotation.values())

        for result in results:
            result["matched_elements"] = sorted(set(result["matched_elements"]))

        return results

    def elements_for_annotation(
        self,
        annotation_id: int,
        expand: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Forward query:

            annotation -> membership blocks/elements

        With expand=False, this returns compact block metadata.
        With expand=True, this returns actual element indices.
        """
        rows = self.conn.execute(
            """
            SELECT
                membership_block_id,
                annotation_id,
                asset_part_id,
                element_kind,
                block_start,
                block_size,
                encoding,
                element_count,
                min_element_index,
                max_element_index,
                payload
            FROM usap_membership_block
            WHERE annotation_id = ?
            ORDER BY asset_part_id, element_kind, block_start
            """,
            (annotation_id,),
        ).fetchall()

        return [
            self._membership_block_row_to_dict(row, expand=expand)
            for row in rows
        ]

    def elements_for_semantic_class(
        self,
        semantic_class_id: int,
        include_subclasses: bool = True,
        expand: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Query membership blocks for annotations of a semantic class.

        This optimized version uses one SQL join instead of one query per
        annotation.

        Default expand=False because big results should stay compact.
        """
        if include_subclasses:
            extra_join = """
            JOIN usap_semantic_class_closure AS c
                ON c.descendant_class_id = a.semantic_class_id
            """
            where = "WHERE c.ancestor_class_id = ?"
        else:
            extra_join = ""
            where = "WHERE a.semantic_class_id = ?"

        rows = self.conn.execute(
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
                mb.payload
            FROM usap_membership_block AS mb
            JOIN usap_annotation AS a
                ON a.annotation_id = mb.annotation_id
            {extra_join}
            {where}
            ORDER BY
                mb.asset_part_id,
                mb.element_kind,
                mb.block_start,
                mb.annotation_id
            """,
            (semantic_class_id,),
        ).fetchall()

        return [self._membership_block_row_to_dict(row, expand=expand) for row in rows]

    def elements_for_city_object(
        self,
        object_uid: str,
        include_descendants: bool = True,
        graph_name: str = DEFAULT_GRAPH_NAME,
        expand: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Query a city object and optionally its descendants, then return
        annotation membership blocks.

        An annotation counts as belonging to a city object if it is linked via
        usap_annotation_object OR names it as its primary_city_object_id. Those
        two should always agree (create_annotation links the primary object),
        but matching both means an annotation can never silently drop out of
        this query if they ever diverge.
        """
        city_object = self.conn.execute(
            """
            SELECT city_object_id
            FROM usap_city_object
            WHERE object_uid = ?
            """,
            (object_uid,),
        ).fetchone()

        if city_object is None:
            return []

        city_object_id = int(city_object["city_object_id"])

        if include_descendants:
            object_rows = self.conn.execute(
                """
                SELECT descendant_city_object_id
                FROM usap_city_object_closure
                WHERE graph_name = ?
                  AND ancestor_city_object_id = ?
                """,
                (graph_name, city_object_id),
            ).fetchall()

            object_ids = [
                int(row["descendant_city_object_id"])
                for row in object_rows
            ]
        else:
            object_ids = [city_object_id]

        if not object_ids:
            return []

        # Chunked so a huge descendant set cannot exceed the SQLite variable
        # limit; the id list is bound twice per query, hence the halved chunk.
        # An annotation linked to objects in different chunks returns its
        # blocks more than once, so rows are deduplicated by block id.
        rows_by_block_id: dict[int, sqlite3.Row] = {}
        chunk_size = _MAX_SQL_IN_VARS // 2

        for chunk_index in range(0, len(object_ids), chunk_size):
            chunk = object_ids[chunk_index : chunk_index + chunk_size]
            placeholders = ",".join("?" for _ in chunk)

            rows = self.conn.execute(
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
                    mb.payload
                FROM usap_membership_block AS mb
                WHERE mb.annotation_id IN (
                    SELECT ao.annotation_id
                    FROM usap_annotation_object AS ao
                    WHERE ao.city_object_id IN ({placeholders})
                    UNION
                    SELECT a.annotation_id
                    FROM usap_annotation AS a
                    WHERE a.primary_city_object_id IN ({placeholders})
                )
                """,
                [*chunk, *chunk],
            ).fetchall()

            for row in rows:
                rows_by_block_id[int(row["membership_block_id"])] = row

        merged_rows = sorted(
            rows_by_block_id.values(),
            key=lambda row: (
                int(row["asset_part_id"]),
                int(row["element_kind"]),
                int(row["block_start"]),
                int(row["annotation_id"]),
            ),
        )

        return [
            self._membership_block_row_to_dict(row, expand=expand)
            for row in merged_rows
        ]

    # ---------------------------------------------------------------------
    # Value fields (dense per-element scalar fields)
    # ---------------------------------------------------------------------
    #
    # Membership blocks store WHICH elements are a concept; value blocks
    # store the VALUE of a property at each element (e.g. shadow fraction
    # per face). A value field binds a registered concept to the elements of
    # one asset part — it is a property of the geometry asset, so this API
    # takes no city-object parameters and the owning annotation's
    # primary_city_object_id stays NULL.
    #
    # V1 contract: a field covers every element of its asset part
    # (len(values) == element_count); "no value" is NaN inside a float
    # array. Partial/sub-range fields are a future format — readers raise.

    def replace_value_field(
        self,
        annotation_id: int,
        asset_part_id: int,
        element_kind: int | str,
        values: Any,
        value_dtype: str | None = None,
    ) -> None:
        """
        Replace the whole value field for one annotation on one asset part.

        Editing is whole-field rewrite by design (write-once, read-many).
        Dtype resolution: explicit value_dtype > the ndarray's own dtype when
        it is in VALUE_DTYPES > 'f4'.
        """
        element_kind = normalize_element_kind(element_kind)

        annotation = self.conn.execute(
            """
            SELECT annotation_id
            FROM usap_annotation
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        if annotation is None:
            raise USAPError(f"Annotation not found: {annotation_id}")

        asset_part = self.conn.execute(
            """
            SELECT element_kind, element_count
            FROM usap_asset_part
            WHERE asset_part_id = ?
            """,
            (asset_part_id,),
        ).fetchone()

        if asset_part is None:
            raise USAPError(f"Unknown asset_part_id: {asset_part_id}")

        expected_kind = int(asset_part["element_kind"])
        part_element_count = int(asset_part["element_count"])

        if element_kind != expected_kind:
            raise USAPError(
                f"Element kind mismatch: asset part expects "
                f"{expected_kind}, got {element_kind}"
            )

        array = np.asarray(values)

        if array.ndim != 1:
            raise USAPError(
                f"Value field must be one-dimensional, got shape {array.shape}."
            )

        if len(array) != part_element_count:
            raise USAPError(
                f"Value field must cover the whole asset part: got "
                f"{len(array)} values, asset part has {part_element_count} "
                "elements. Use NaN for elements without a value."
            )

        if value_dtype is not None:
            value_dtype = normalize_value_dtype(value_dtype)
        elif isinstance(values, np.ndarray) and array.dtype.str[1:] in VALUE_DTYPES:
            value_dtype = array.dtype.str[1:]
        else:
            value_dtype = DEFAULT_VALUE_DTYPE

        target_dtype = np.dtype("<" + value_dtype)

        if (
            target_dtype.kind != "f"
            and array.dtype.kind == "f"
            and bool(np.isnan(array).any())
        ):
            raise USAPError(
                f"NaN is not representable in integer value_dtype "
                f"{value_dtype!r}. Use a float dtype for fields with "
                "missing values."
            )

        _check_value_cast(array, target_dtype, value_dtype)

        typed = np.ascontiguousarray(array, dtype=target_dtype)

        with self.transaction():
            self.conn.execute(
                """
                DELETE FROM usap_value_block
                WHERE annotation_id = ?
                AND asset_part_id = ?
                AND element_kind = ?
                """,
                (annotation_id, asset_part_id, element_kind),
            )

            for block_start in range(0, len(typed), VALUE_CHUNK_SIZE):
                chunk = typed[block_start : block_start + VALUE_CHUNK_SIZE]

                if target_dtype.kind == "f":
                    real = chunk[~np.isnan(chunk)]
                else:
                    real = chunk

                value_min = float(real.min()) if real.size else None
                value_max = float(real.max()) if real.size else None

                self.conn.execute(
                    """
                    INSERT INTO usap_value_block (
                        annotation_id,
                        asset_part_id,
                        element_kind,
                        block_start,
                        element_count,
                        value_dtype,
                        value_min,
                        value_max,
                        payload
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        annotation_id,
                        asset_part_id,
                        element_kind,
                        block_start,
                        len(chunk),
                        value_dtype,
                        value_min,
                        value_max,
                        encode_value_block(chunk, value_dtype),
                    ),
                )

            self.log_edit(
                "replace_value_field",
                "usap_annotation",
                annotation_id,
                f'{{"asset_part_id": {asset_part_id}, '
                f'"element_kind": {element_kind}, '
                f'"element_count": {len(typed)}, '
                f'"value_dtype": "{value_dtype}"}}',
            )

    def annotate_value_field(
        self,
        *,
        concept: int | str,
        asset_part_id: int,
        element_kind: int | str,
        values: Any,
        value_dtype: str | None = None,
        annotation_uid: str | None = None,
        label: str | None = None,
        status: str = "draft",
        confidence: float | None = None,
        attributes: dict[str, Any] | None = None,
        attributes_json: str | None = None,
        scheme: str | None = None,
    ) -> dict[str, Any]:
        """
        Create an annotation for a concept and attach a per-element value
        field in one step:

            this asset part carries these values, meaning this concept.

        The concept must be registered (CityGML, ADE, or a minimal local
        vocabulary — any scheme). Field metadata (unit, validAt, method, ...)
        belongs in `attributes`. There are deliberately no city-object
        parameters: a value field is a property of the geometry asset.
        """
        element_kind = normalize_element_kind(element_kind)

        with self.transaction():
            annotation = self.create_concept_annotation(
                concept=concept,
                annotation_uid=annotation_uid,
                label=label,
                status=status,
                confidence=confidence,
                attributes=attributes,
                attributes_json=attributes_json,
                scheme=scheme,
            )

            annotation_id = int(annotation["annotation_id"])

            self.replace_value_field(
                annotation_id=annotation_id,
                asset_part_id=asset_part_id,
                element_kind=element_kind,
                values=values,
                value_dtype=value_dtype,
            )

        result = self.get_annotation(
            annotation_id,
            include_membership_summary=True,
        )

        if result is None:
            raise USAPError(
                f"Annotation disappeared after value-field annotation: "
                f"{annotation_id}"
            )

        return result

    def _value_blocks_for_annotation(
        self,
        annotation_id: int,
        asset_part_id: int | None,
        element_kind: int | None,
    ) -> list[sqlite3.Row]:
        """
        Fetch one annotation's value blocks (optionally filtered) and check
        that they belong to exactly one (asset part, element kind) pair,
        share one dtype, and tile the whole asset part (v1 full coverage) —
        so every reader enforces the same contract.
        """
        where = ["vb.annotation_id = ?"]
        params: list[Any] = [annotation_id]

        if asset_part_id is not None:
            where.append("vb.asset_part_id = ?")
            params.append(asset_part_id)

        if element_kind is not None:
            where.append("vb.element_kind = ?")
            params.append(element_kind)

        rows = self.conn.execute(
            f"""
            SELECT
                vb.value_block_id,
                vb.asset_part_id,
                vb.element_kind,
                vb.block_start,
                vb.element_count,
                vb.value_dtype,
                vb.value_min,
                vb.value_max,
                vb.payload,
                ap.element_count AS part_element_count
            FROM usap_value_block AS vb
            JOIN usap_asset_part AS ap
                ON ap.asset_part_id = vb.asset_part_id
            WHERE {" AND ".join(where)}
            ORDER BY vb.asset_part_id, vb.element_kind, vb.block_start
            """,
            params,
        ).fetchall()

        if not rows:
            raise USAPError(
                f"No value field found for annotation {annotation_id}."
            )

        targets = {(int(r["asset_part_id"]), int(r["element_kind"])) for r in rows}

        if len(targets) > 1:
            raise USAPError(
                f"Annotation {annotation_id} has value fields on several "
                f"asset parts: {sorted(targets)}. Pass asset_part_id."
            )

        dtypes = {r["value_dtype"] for r in rows}

        if len(dtypes) > 1:
            raise USAPError(
                f"Value field of annotation {annotation_id} mixes dtypes "
                f"{sorted(dtypes)}; the field is corrupt."
            )

        expected_start = 0

        for row in rows:
            if int(row["block_start"]) != expected_start:
                raise USAPError(
                    f"Value field of annotation {annotation_id} has a gap at "
                    f"element {expected_start}; partial fields are not "
                    "supported in v1."
                )

            expected_start += int(row["element_count"])

        part_element_count = int(rows[0]["part_element_count"])

        if expected_start != part_element_count:
            raise USAPError(
                f"Value field of annotation {annotation_id} covers "
                f"{expected_start} of {part_element_count} elements; partial "
                "fields are not supported in v1."
            )

        return rows

    def values_for_annotation(
        self,
        annotation_id: int,
        *,
        asset_part_id: int | None = None,
        element_kind: int | str | None = None,
    ) -> np.ndarray:
        """
        Forward query: annotation -> its dense value array.

        Element i's value is result[i]; the array always spans the whole
        asset part (v1 contract), with NaN marking "no value" in float fields.
        """
        if element_kind is not None:
            element_kind = normalize_element_kind(element_kind)

        rows = self._value_blocks_for_annotation(
            annotation_id, asset_part_id, element_kind
        )

        value_dtype = rows[0]["value_dtype"]

        # Tiling/coverage was already verified by _value_blocks_for_annotation.
        return np.concatenate(
            [
                decode_value_block(
                    row["payload"], value_dtype, int(row["element_count"])
                )
                for row in rows
            ]
        )

    def elements_where(
        self,
        annotation_id: int,
        predicate: tuple[str, float] | Any,
        *,
        asset_part_id: int | None = None,
    ) -> list[int]:
        """
        Value query: element indices where the field satisfies a predicate.

            elements_where(ann_id, (">", 0.5))
            elements_where(ann_id, lambda v: (v > 0.2) & (v < 0.8))

        The output is a sorted element-index set — the same shape as a
        membership query result, so it plugs into the same downstream paths.
        NaN never matches. Comparison predicates skip blocks whose stored
        min/max cannot match.
        """
        op: str | None = None
        threshold: float | None = None
        mask_fn = None

        if isinstance(predicate, tuple):
            if len(predicate) != 2 or predicate[0] not in _COMPARISON_OPS:
                raise USAPError(
                    f"Unsupported predicate {predicate!r}. Use (op, threshold) "
                    f"with op in {sorted(_COMPARISON_OPS)}, or a callable."
                )
            op, threshold = predicate[0], float(predicate[1])
        elif callable(predicate):
            mask_fn = predicate
        else:
            raise USAPError(
                f"Unsupported predicate {predicate!r}. Use (op, threshold) "
                "or a callable returning a boolean mask."
            )

        rows = self._value_blocks_for_annotation(
            annotation_id, asset_part_id, None
        )

        value_dtype = rows[0]["value_dtype"]
        hits: list[np.ndarray] = []

        for row in rows:
            if op is not None and _block_cannot_match(
                op,
                threshold,
                row["value_min"],
                row["value_max"],
            ):
                continue

            block = decode_value_block(
                row["payload"], value_dtype, int(row["element_count"])
            )

            with np.errstate(invalid="ignore"):
                if mask_fn is not None:
                    mask = np.asarray(mask_fn(block), dtype=bool)

                    if mask.shape != block.shape:
                        raise USAPError(
                            "Callable predicate must return a boolean mask of "
                            f"the block's shape; got {mask.shape} for "
                            f"{block.shape}."
                        )
                else:
                    mask = _COMPARISON_OPS[op](block, threshold)

                # NaN means "no value" and never matches — numpy would let
                # NaN satisfy "!=" (and callables may not handle it).
                if block.dtype.kind == "f":
                    mask = mask & ~np.isnan(block)

            block_hits = np.nonzero(mask)[0]

            if block_hits.size:
                hits.append(block_hits + int(row["block_start"]))

        if not hits:
            return []

        # Blocks are visited in ascending block_start order and hits are
        # ascending within each block, so the result is already sorted.
        return np.concatenate(hits).tolist()

    def value_field_stats(
        self,
        annotation_id: int,
        *,
        asset_part_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Field stats from the stored per-block min/max — no payload decode.

        min/max ignore NaN; count is the total number of stored values
        (NaN included).
        """
        where = ["vb.annotation_id = ?"]
        params: list[Any] = [annotation_id]

        if asset_part_id is not None:
            where.append("vb.asset_part_id = ?")
            params.append(asset_part_id)

        rows = self.conn.execute(
            f"""
            SELECT
                vb.asset_part_id,
                vb.element_kind,
                vb.value_dtype,
                MIN(vb.value_min) AS value_min,
                MAX(vb.value_max) AS value_max,
                SUM(vb.element_count) AS value_count,
                MIN(vb.block_start) AS first_block_start,
                COUNT(*) AS block_count,
                ap.element_count AS part_element_count
            FROM usap_value_block AS vb
            JOIN usap_asset_part AS ap
                ON ap.asset_part_id = vb.asset_part_id
            WHERE {" AND ".join(where)}
            GROUP BY vb.asset_part_id, vb.element_kind, vb.value_dtype
            """,
            params,
        ).fetchall()

        if not rows:
            raise USAPError(
                f"No value field found for annotation {annotation_id}."
            )

        if len(rows) > 1:
            targets = {
                (int(r["asset_part_id"]), int(r["element_kind"])) for r in rows
            }

            if len(targets) > 1:
                raise USAPError(
                    f"Annotation {annotation_id} has value fields on several "
                    f"asset parts: {sorted(targets)}. Pass asset_part_id."
                )

            raise USAPError(
                f"Value field of annotation {annotation_id} mixes dtypes "
                f"{sorted(r['value_dtype'] for r in rows)}; the field is "
                "corrupt."
            )

        row = rows[0]

        # Same v1 contract as the decoding readers, kept SQL-only: the field
        # must start at element 0 and account for every element of the part.
        if (
            int(row["first_block_start"]) != 0
            or int(row["value_count"]) != int(row["part_element_count"])
        ):
            raise USAPError(
                f"Value field of annotation {annotation_id} covers "
                f"{int(row['value_count'])} of "
                f"{int(row['part_element_count'])} elements; partial fields "
                "are not supported in v1."
            )

        return {
            "annotation_id": annotation_id,
            "asset_part_id": int(row["asset_part_id"]),
            "element_kind": int(row["element_kind"]),
            "value_dtype": row["value_dtype"],
            "min": row["value_min"],
            "max": row["value_max"],
            "count": int(row["value_count"]),
            "block_count": int(row["block_count"]),
        }

    def _annotation_value_field_summary(
        self,
        annotation_id: int,
    ) -> list[dict[str, Any]]:
        """
        Summarize which asset parts an annotation carries value fields on.
        """
        rows = self.conn.execute(
            """
            SELECT
                vb.asset_part_id,
                ap.asset_id,
                asset.uri AS asset_uri,
                asset.asset_kind,
                ap.part_path,
                vb.element_kind,
                vb.value_dtype,
                SUM(vb.element_count) AS value_count,
                COUNT(*) AS block_count,
                MIN(vb.value_min) AS value_min,
                MAX(vb.value_max) AS value_max
            FROM usap_value_block AS vb
            JOIN usap_asset_part AS ap
                ON ap.asset_part_id = vb.asset_part_id
            JOIN usap_asset AS asset
                ON asset.asset_id = ap.asset_id
            WHERE vb.annotation_id = ?
            GROUP BY
                vb.asset_part_id,
                ap.asset_id,
                asset.uri,
                asset.asset_kind,
                ap.part_path,
                vb.element_kind,
                vb.value_dtype
            ORDER BY vb.asset_part_id
            """,
            (annotation_id,),
        ).fetchall()

        return [dict(row) for row in rows]

    # ---------------------------------------------------------------------
    # Validation
    # ---------------------------------------------------------------------

    def validate_report(self):
        """
        Return a structured validation report.
        """
        return validate_connection(self.conn)

    # ---------------------------------------------------------------------
    # CRUD operations for integrated prototype test
    # --------------------------------------------------------------------- 

    def resolve_semantic_class(
        self,
        concept: int | str,
        *,
        scheme: str | None = None,
        require_unique: bool = True,
    ) -> int:
        """
        Resolve a concept reference to a semantic_class_id.

        Accepted concept forms:
        - semantic_class_id as int
        - class_uri as str
        - local_name as str

        Examples:
        resolve_semantic_class(12)
        resolve_semantic_class("RoofSurface")
        resolve_semantic_class("citygml-3.0:building:RoofSurface")
        resolve_semantic_class("EnergyRoof")
        resolve_semantic_class("usap-ade-prototype:energy:EnergyRoof")
        """
        if isinstance(concept, int):
            row = self.conn.execute(
                """
                SELECT semantic_class_id
                FROM usap_semantic_class
                WHERE semantic_class_id = ?
                """,
                (concept,),
            ).fetchone()

            if row is None:
                raise USAPError(f"Semantic class not found: {concept}")

            return int(row["semantic_class_id"])

        where = """
            (
                local_name = ?
                OR class_uri = ?
            )
        """
        params: list[Any] = [concept, concept]

        if scheme is not None:
            where += " AND scheme = ?"
            params.append(scheme)

        rows = self.conn.execute(
            f"""
            SELECT
                semantic_class_id,
                scheme,
                scheme_version,
                class_uri,
                local_name,
                is_ade
            FROM usap_semantic_class
            WHERE {where}
            ORDER BY semantic_class_id
            """,
            params,
        ).fetchall()

        if not rows:
            raise USAPError(f"Semantic class concept not found: {concept}")

        if require_unique and len(rows) > 1:
            options = [
                {
                    "semantic_class_id": int(row["semantic_class_id"]),
                    "scheme": row["scheme"],
                    "scheme_version": row["scheme_version"],
                    "class_uri": row["class_uri"],
                    "local_name": row["local_name"],
                    "is_ade": bool(row["is_ade"]),
                }
                for row in rows
            ]

            raise USAPAmbiguityError(
                "Semantic class concept is ambiguous. "
                f"Use a class_uri, semantic_class_id, or scheme. "
                f"Concept: {concept}. Options: {options}"
            )

        return int(rows[0]["semantic_class_id"])
    

    def get_semantic_class(
        self,
        concept: int | str,
        *,
        scheme: str | None = None,
    ) -> dict[str, Any]:
        """
        Resolve and return one semantic class record.
        """
        semantic_class_id = self.resolve_semantic_class(
            concept,
            scheme=scheme,
        )

        row = self.conn.execute(
            """
            SELECT
                semantic_class_id,
                scheme,
                scheme_version,
                class_uri,
                local_name,
                parent_class_id,
                is_ade
            FROM usap_semantic_class
            WHERE semantic_class_id = ?
            """,
            (semantic_class_id,),
        ).fetchone()

        if row is None:
            raise USAPError(f"Semantic class not found: {semantic_class_id}")

        result = dict(row)
        result["is_ade"] = bool(result["is_ade"])

        return result


    def list_accepted_concepts(
        self,
        *,
        scheme: str | None = None,
        is_ade: bool | None = None,
        search: str | None = None,
        in_use: bool | None = None,
    ) -> list[dict[str, Any]]:
        """
        List concepts currently registered in the package.

        These are the concepts accepted by annotate_elements(...).

        Each concept carries annotation_count and in_use (True when at least
        one annotation in this package references it). Pass in_use=True/False
        to keep only used/unused concepts.
        """
        where: list[str] = []
        params: list[Any] = []

        if scheme is not None:
            where.append("sc.scheme = ?")
            params.append(scheme)

        if is_ade is not None:
            where.append("sc.is_ade = ?")
            params.append(1 if is_ade else 0)

        if search is not None:
            pattern = f"%{search}%"
            where.append(
                """
                (
                    sc.local_name LIKE ?
                    OR sc.class_uri LIKE ?
                    OR sc.scheme LIKE ?
                )
                """
            )
            params.extend([pattern, pattern, pattern])

        where_sql = ""

        if where:
            where_sql = "WHERE " + " AND ".join(where)

        having_sql = ""

        if in_use is True:
            having_sql = "HAVING COUNT(a.annotation_id) > 0"
        elif in_use is False:
            having_sql = "HAVING COUNT(a.annotation_id) = 0"

        rows = self.conn.execute(
            f"""
            SELECT
                sc.semantic_class_id,
                sc.scheme,
                sc.scheme_version,
                sc.class_uri,
                sc.local_name,
                sc.parent_class_id,
                sc.is_ade,
                COUNT(a.annotation_id) AS annotation_count
            FROM usap_semantic_class AS sc
            LEFT JOIN usap_annotation AS a
                ON a.semantic_class_id = sc.semantic_class_id
            {where_sql}
            GROUP BY sc.semantic_class_id
            {having_sql}
            ORDER BY sc.is_ade, sc.scheme, sc.local_name, sc.semantic_class_id
            """,
            params,
        ).fetchall()

        result = []

        for row in rows:
            item = dict(row)
            item["is_ade"] = bool(item["is_ade"])
            item["annotation_count"] = int(item["annotation_count"])
            item["in_use"] = item["annotation_count"] > 0
            result.append(item)

        return result


    def concept_exists(
        self,
        concept: int | str,
        *,
        scheme: str | None = None,
    ) -> bool:
        """
        Return True if a concept resolves to exactly one semantic class.
        """
        try:
            self.resolve_semantic_class(
                concept,
                scheme=scheme,
            )
        except USAPError:
            return False

        return True
    
    def resolve_asset_part(
        self,
        asset_part: int | str,
        *,
        part_path: str | None = None,
    ) -> int:
        """
        Resolve an asset-part reference to asset_part_id.

        Accepted forms:
        - asset_part_id as int
        - asset uri as str, plus part_path when the asset has several parts

        Lets batch files reference parts by name (the asset's uri) instead of
        the numeric id from the build manifest.
        """
        if isinstance(asset_part, int):
            if part_path is not None:
                raise USAPError(
                    "part_path only applies when the asset part is referenced "
                    "by asset uri."
                )

            row = self.conn.execute(
                """
                SELECT asset_part_id
                FROM usap_asset_part
                WHERE asset_part_id = ?
                """,
                (asset_part,),
            ).fetchone()

            if row is None:
                raise USAPError(f"Unknown asset_part_id: {asset_part}")

            return int(row["asset_part_id"])

        where = "a.uri = ?"
        params: list[Any] = [asset_part]

        if part_path is not None:
            where += " AND ap.part_path = ?"
            params.append(part_path)

        rows = self.conn.execute(
            f"""
            SELECT
                ap.asset_part_id,
                ap.part_path,
                a.uri
            FROM usap_asset_part AS ap
            JOIN usap_asset AS a
                ON a.asset_id = ap.asset_id
            WHERE {where}
            ORDER BY ap.asset_part_id
            """,
            params,
        ).fetchall()

        if not rows:
            raise USAPError(
                f"Asset part not found: {asset_part!r}"
                + (f" (part_path {part_path!r})" if part_path is not None else "")
            )

        if len(rows) > 1:
            options = [
                {
                    "asset_part_id": int(row["asset_part_id"]),
                    "part_path": row["part_path"],
                    "uri": row["uri"],
                }
                for row in rows
            ]

            raise USAPAmbiguityError(
                "Asset part reference is ambiguous. Add part_path or use "
                f"asset_part_id. Reference: {asset_part!r}. Options: {options}"
            )

        return int(rows[0]["asset_part_id"])

    def resolve_city_object(
        self,
        city_object: int | str,
    ) -> int:
        """
        Resolve a city object reference to city_object_id.

        Accepted forms:
        - city_object_id as int
        - object_uid as str
        - gml_id as str
        """
        if isinstance(city_object, int):
            row = self.conn.execute(
                """
                SELECT city_object_id
                FROM usap_city_object
                WHERE city_object_id = ?
                """,
                (city_object,),
            ).fetchone()

            if row is None:
                raise USAPError(f"City object not found: {city_object}")

            return int(row["city_object_id"])

        rows = self.conn.execute(
            """
            SELECT city_object_id, object_uid, gml_id
            FROM usap_city_object
            WHERE object_uid = ?
            OR gml_id = ?
            ORDER BY city_object_id
            """,
            (city_object, city_object),
        ).fetchall()

        if not rows:
            raise USAPError(f"City object not found: {city_object}")

        if len(rows) > 1:
            options = [
                {
                    "city_object_id": int(row["city_object_id"]),
                    "object_uid": row["object_uid"],
                    "gml_id": row["gml_id"],
                }
                for row in rows
            ]

            raise USAPAmbiguityError(
                "City object reference is ambiguous. "
                f"Use city_object_id. Reference: {city_object}. Options: {options}"
            )

        return int(rows[0]["city_object_id"])
    
    def create_concept_annotation(
        self,
        *,
        concept: int | str,
        annotation_uid: str | None = None,
        city_object_id: int | None = None,
        city_object_uid: str | None = None,
        label: str | None = None,
        status: str = "draft",
        confidence: float | None = None,
        attributes: dict[str, Any] | None = None,
        attributes_json: str | None = None,
        scheme: str | None = None,
    ) -> dict[str, Any]:
        """
        Create an annotation using a concept reference instead of a raw
        semantic_class_id.

        The concept can come from CityGML, ADE, or any registered semantic scheme.
        Precondition: the concept must already be registered in the package's
        vocabulary (see seed_vocabulary_file) — it is referenced here, not created.
        Resolution raises USAPError if the concept is unknown.
        """
        semantic_class_id = self.resolve_semantic_class(
            concept,
            scheme=scheme,
        )

        resolved_city_object_id: int | None = None

        if city_object_id is not None and city_object_uid is not None:
            raise USAPError("Provide city_object_id or city_object_uid, not both.")

        if city_object_id is not None:
            resolved_city_object_id = self.resolve_city_object(city_object_id)

        if city_object_uid is not None:
            resolved_city_object_id = self.resolve_city_object(city_object_uid)

        if attributes is not None and attributes_json is not None:
            raise USAPError("Provide attributes or attributes_json, not both.")

        stored_attributes_json = attributes_json

        if attributes is not None:
            stored_attributes_json = json.dumps(attributes)

        if annotation_uid is None:
            annotation_uid = f"ann_{uuid.uuid4().hex}"

        # create_annotation links the primary city object itself
        # (link_primary_object=True), so no extra annotation-object link here.
        annotation_id = self.create_annotation(
            annotation_uid=annotation_uid,
            semantic_class_id=semantic_class_id,
            primary_city_object_id=resolved_city_object_id,
            label=label,
            status=status,
            confidence=confidence,
            attributes_json=stored_attributes_json,
        )

        annotation = self.get_annotation(
            annotation_id,
            include_membership_summary=True,
        )

        if annotation is None:
            raise USAPError(
                f"Annotation disappeared after creation: {annotation_id}"
            )

        return annotation
    
    def annotate_elements(
        self,
        *,
        concept: int | str,
        asset_part_id: int,
        element_kind: str,
        element_indices: list[int],
        annotation_uid: str | None = None,
        city_object_id: int | None = None,
        city_object_uid: str | None = None,
        label: str | None = None,
        status: str = "draft",
        confidence: float | None = None,
        attributes: dict[str, Any] | None = None,
        attributes_json: str | None = None,
        scheme: str | None = None,
    ) -> dict[str, Any]:
        """
        Create an annotation for a concept and immediately attach selected elements.

        This is the main prototype API:

            annotate this asset part, these element indices, as this concept.

        The concept can be CityGML, ADE, or any registered semantic class.
        """
        element_kind = normalize_element_kind(element_kind)
        with self.transaction():
            annotation = self.create_concept_annotation(
                concept=concept,
                annotation_uid=annotation_uid,
                city_object_id=city_object_id,
                city_object_uid=city_object_uid,
                label=label,
                status=status,
                confidence=confidence,
                attributes=attributes,
                attributes_json=attributes_json,
                scheme=scheme,
            )

            annotation_id = int(annotation["annotation_id"])

            self.replace_annotation_membership(
                annotation_id=annotation_id,
                asset_part_id=asset_part_id,
                element_kind=element_kind,
                element_indices=element_indices,
            )

        result = self.get_annotation(
            annotation_id,
            include_membership_summary=True,
        )

        if result is None:
            raise USAPError(
                f"Annotation disappeared after element annotation: {annotation_id}"
            )

        return result
    

    def attach_annotation_elements(
        self,
        *,
        annotation_id: int,
        asset_part_id: int,
        element_kind: str,
        element_indices: list[int],
    ) -> dict[str, Any]:
        """
        Attach or replace selected elements for one annotation on one asset part.

        This is a clearer prototype name for replace_annotation_membership.
        It preserves memberships on other asset parts.
        """
        element_kind = normalize_element_kind(element_kind)
        self.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=asset_part_id,
            element_kind=element_kind,
            element_indices=element_indices,
        )

        annotation = self.get_annotation(
            annotation_id,
            include_membership_summary=True,
        )

        if annotation is None:
            raise USAPError(
                f"Annotation not found after attaching elements: {annotation_id}"
            )

        return annotation

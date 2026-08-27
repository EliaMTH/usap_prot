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
          membership the exact indices one assessment covers, stored
                     as roaring bitmap blocks                       → usap_membership_block
            assessment  one dated evaluation, against one asset     → usap_assessment
              annotation  an editable claim: concept + status + attrs → usap_annotation
                semantic_class  what kind of thing it is (RoofSurface) → usap_semantic_class
                city_object     which object it is (building_1_roof_1) → usap_city_object

An annotation is the logical claim; an assessment is one evaluation of it at a
date, against one asset. Re-surveying the same roof next year is a second
assessment of the same annotation, not a second annotation — which is what keeps
the concept and city-object link from being duplicated and drifting apart.
Callers that never mention assessments get one implicitly (see
_default_assessment_for) and behave exactly as they did before 0.4.0.

"This class and its subclasses" is a single indexed lookup into a stored
transitive closure, maintained as vocabularies are seeded:

    usap_semantic_class_closure   class → subclass (parentage from vocabularies)

"This object and its descendants" is instead walked from the edges themselves
with a recursive CTE (elements_for_city_object): the object graph is edited
one edge at a time and is typed, so a stored closure would owe a rebuild after
every edit and would have to encode the containment-type policy on write.

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
    Assets ..................... register_asset, register_asset_part,
                                  update_asset, list_assets, list_asset_parts
    Assessments ................ create/get/list/update/delete_assessment,
                                  resolve_asset, resolve_assessment
    Semantic classes ........... create_semantic_class (+ closure maintenance)
    City objects and graph ..... create_city_object, link_city_objects,
                                  list_city_objects
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
                                  resolve_asset_part, get_semantic_class,
                                  list_accepted_concepts, concept_exists,
                                  create_concept_annotation, annotate_elements,
                                  attach_annotation_elements (the high-level
                                  entry points most callers use)
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
import json
import uuid

import numpy as np
from pyroaring import BitMap

from ._util import mint_package_iri
from .errors import USAPAmbiguityError, USAPError
from .constants import (
    ANNOTATION_STATUSES,
    CITY_OBJECT_STATUSES,
    CONFIDENCE_RANGE,
    CURRENT_PROFILE_VERSION,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_ENCODING,
    DEFAULT_GRAPH_NAME,
    DEFAULT_TRAVERSAL_CATEGORIES,
    DEFAULT_VALUE_DTYPE,
    RELATIONSHIP_CATEGORIES,
    RELATIONSHIP_DIRECTIONS,
    SUPPORTED_PROFILE_VERSIONS,
    VALUE_BLOCK_ENCODING,
    VALUE_CHUNK_SIZE,
    VALUE_DTYPES,
    normalize_element_kind,
    normalize_value_dtype,
)
from .encoding import (
    IndexArray,
    as_index_array,
    block_start_for_index,
    decode_roaring_array,
    decode_roaring_bitmap,
    decode_value_block,
    encode_roaring,
    encode_value_block,
    roaring_to_array,
    split_indices_into_blocks,
)
from .sqlite_utils import require_lastrowid
from .validation import validate_connection
from .geopackage import (
    DEFAULT_EXTENT_SRS_ID,
    USAP_FEATURES_LAYER,
    encode_gpkg_bbox_polygon,
    initialize_geopackage_metadata,
)

_UNSET = object()

# Shipped inside the package (see pyproject package-data), not next to the
# repo checkout, so the default schema loads from a plain wheel install too.
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "data" / "schema.sql"

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


def _same_value(stored: Any, requested: Any) -> bool:
    """
    Compare a stored column against what a re-registration asks for.

    JSON columns are compared as parsed data, not text: the same metadata
    re-serialized with different key order or spacing is the same metadata,
    and reporting it as a conflict would make idempotent re-runs fail.
    """
    if stored == requested:
        return True

    if isinstance(stored, str) and isinstance(requested, str):
        try:
            return json.loads(stored) == json.loads(requested)
        except ValueError:
            return False

    return False


def _conflicting_fields(
    stored: sqlite3.Row,
    requested: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """
    Fields where an existing row and a re-registration disagree.

    Registration is idempotent on a natural key, which is only safe while
    "already registered" means "registered as the same thing". Returning the
    existing id despite a differing kind, count, or bounds hands the caller a
    row that does not describe what they asked to register.
    """
    return {
        column: {"stored": stored[column], "requested": value}
        for column, value in requested.items()
        if not _same_value(stored[column], value)
    }


def _check_annotation_fields(
    status: Any = _UNSET,
    confidence: Any = _UNSET,
    attributes_json: Any = _UNSET,
) -> None:
    """
    Refuse annotation field values no reader could act on.

    Enforced on the way in rather than only reported afterwards: a rejected
    status silently drops out of every status filter, a confidence outside
    [0, 1] cannot be compared with any other, and attributes that are not
    JSON cannot be read back at all. _UNSET means "not being written".
    """
    if status is not _UNSET and status not in ANNOTATION_STATUSES:
        raise USAPError(
            f"Unknown annotation status {status!r}. "
            f"Use one of: {', '.join(ANNOTATION_STATUSES)}."
        )

    if confidence is not _UNSET and confidence is not None:
        minimum, maximum = CONFIDENCE_RANGE

        if not minimum <= float(confidence) <= maximum:
            raise USAPError(
                f"Confidence {confidence!r} is outside [{minimum}, {maximum}]."
            )

    if attributes_json is not _UNSET and attributes_json is not None:
        try:
            json.loads(attributes_json)
        except (TypeError, ValueError) as exc:
            raise USAPError(
                f"attributes_json is not valid JSON: {exc}"
            ) from exc


def _descendants_cte(edge_type_count: int, direction: str = "out") -> str:
    """
    SQL prefix naming an object and everything reachable from it as
    ``objects(object_id)``.

    Bind (root_city_object_id, graph_name, *relationship_type_ids) in that
    order, before any other parameter of the statement it prefixes; for
    direction='both', bind (root, graph, *ids, graph, *ids). With
    edge_type_count == 0 the set is the root alone.

    Reachability is walked from the edges themselves rather than read from a
    stored transitive closure: no derived table to keep in step with the
    edges, so an object created on its own is still its own descendant, and no
    rebuild is owed after every link_city_objects.

    Direction is a query argument because the edges are directed but not
    hierarchical — 'out' follows from_ -> to_, 'in' the reverse, 'both' either
    way. Only 'both' needs two recursive terms (SQLite >= 3.34).

    Type filtering binds integer ids, resolved by _resolve_edge_type_ids before
    this statement is built. Doing it there rather than joining
    usap_relationship_type inside the recursive term is deliberate: see below.

    UNION (not UNION ALL) both deduplicates and terminates on a cycle.
    CROSS JOIN pins the join order (queue row -> its edges); with a plain JOIN,
    SQLite picks the other index on its graph_name prefix and rescans every
    edge per recursive step (~400x slower). Do not add a join or a subquery to
    this body.

    to_city_object_id IS NOT NULL excludes edges whose target is outside the
    document: an external URI is not a node and must not enter the queue.
    """
    if direction not in RELATIONSHIP_DIRECTIONS:
        raise USAPError(
            f"Unknown direction {direction!r}. "
            f"Use one of: {', '.join(RELATIONSHIP_DIRECTIONS)}."
        )

    if edge_type_count < 1:
        return """
            WITH objects(object_id) AS (
                SELECT ?
            )
        """

    placeholders = ",".join("?" for _ in range(edge_type_count))

    def step(from_column: str, to_column: str, alias: str) -> str:
        return f"""
            SELECT {alias}.{to_column}
            FROM objects AS o
            CROSS JOIN usap_city_object_relationship AS {alias}
                ON {alias}.{from_column} = o.object_id
            WHERE {alias}.graph_name = ?
              AND {alias}.relationship_type_id IN ({placeholders})
              AND {alias}.to_city_object_id IS NOT NULL
        """

    outward = step("from_city_object_id", "to_city_object_id", "r")
    inward = step("to_city_object_id", "from_city_object_id", "r2")

    if direction == "out":
        recursive = outward
    elif direction == "in":
        recursive = inward
    else:
        recursive = f"{outward} UNION {inward}"

    return f"""
        WITH RECURSIVE objects(object_id) AS (
            SELECT ?
            UNION
            {recursive}
        )
    """


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

        # (local_name, code_space or '') -> relationship_type_id. An import
        # resolves the same handful of types once per edge, and the lookup is
        # otherwise a query each time. Cleared on rollback (see transaction).
        self._relationship_type_cache: dict[tuple[str, str], int] = {}

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
            # A relationship type auto-registered inside the rolled-back
            # transaction no longer exists, but the cache would still hand out
            # its id and the next edge insert would die on the foreign key.
            self._relationship_type_cache.clear()
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
        profile_version: str = CURRENT_PROFILE_VERSION,
        package_iri: str | None = None,
    ) -> "USAPPackage":
        """
        Create a new package.

        package_iri is the package's stable identity. It defaults to a freshly
        minted UUID URN, which is globally unique by construction and needs no
        domain, registry, or namespace to be valid — so the caller never has to
        supply one. Pass an explicit IRI only to adopt an identity that already
        exists elsewhere.
        """
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

        # If initialization fails partway, do not leave an open connection
        # and a half-initialized file behind (the file is always ours here:
        # any pre-existing one was removed or raised above).
        try:
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
                        package_iri,
                        default_block_size,
                        default_encoding,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        profile_version,
                        package_iri or mint_package_iri(),
                        DEFAULT_BLOCK_SIZE,
                        DEFAULT_ENCODING,
                        None,
                    ),
                )
        except BaseException:
            pkg.conn.close()
            if db_path.exists():
                os.remove(db_path)
            raise

        return pkg
    
    @classmethod
    def open(cls, db_path: str | Path) -> "USAPPackage":
        """
        Open an existing package.

        The file is gated on being a USAP package this build understands:
        without the check, any SQLite file opens here and fails much later
        with 'no such table: usap_asset' from whatever API call happens to
        run first.
        """
        db_path = Path(db_path)

        if not db_path.exists():
            raise USAPError(f"Database does not exist: {db_path}")

        pkg = cls(db_path)

        try:
            pkg._check_profile_compatibility()
        except BaseException:
            pkg.conn.close()
            raise

        return pkg

    def _check_profile_compatibility(self) -> None:
        try:
            row = self.conn.execute(
                """
                SELECT profile_version
                FROM usap_profile
                WHERE profile_id = 1
                """
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise USAPError(
                f"Not a USAP package: {self.db_path} ({exc})."
            ) from exc

        if row is None:
            raise USAPError(
                f"Not a USAP package: {self.db_path} (no usap_profile row)."
            )

        profile_version = row["profile_version"]

        # No migration path exists yet, so an unknown version must stop here:
        # reading it with this build's assumptions would misinterpret it
        # rather than fail.
        if profile_version not in SUPPORTED_PROFILE_VERSIONS:
            raise USAPError(
                f"Unsupported USAP profile version {profile_version!r} in "
                f"{self.db_path}. This build supports: "
                f"{', '.join(SUPPORTED_PROFILE_VERSIONS)}."
            )

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

    def get_package_iri(self) -> str:
        """
        Return this package's stable identity (a UUID URN by default).

        Unlike get_default_block_size there is no sensible fallback: an
        identity invented on read would differ between two readers of the
        same file, which is the opposite of what it is for.
        """
        row = self.conn.execute(
            """
            SELECT package_iri
            FROM usap_profile
            WHERE profile_id = 1
            """
        ).fetchone()

        package_iri = (row["package_iri"] or "").strip() if row else ""

        # Stripped before the check, matching _validate_profile: a whitespace
        # value is as unusable as an empty one, and the two must not disagree
        # about whether a package has an identity.
        if not package_iri:
            raise USAPError(
                f"Package has no package_iri: {self.db_path}. It was not "
                "created by USAPPackage.create()."
            )

        return package_iri

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

        Idempotent on (uri, content_hash): re-registering the same file
        returns the existing asset_id. That is only sound while the rest of
        the record agrees, so a re-registration that changes the kind, media
        type, SRS, or metadata raises instead of quietly returning a row that
        describes something else.
        """
        existing = self.conn.execute(
            """
            SELECT asset_id, asset_kind, media_type, srs_id, metadata_json
            FROM usap_asset
            WHERE uri = ?
              AND content_hash IS ?
            """,
            (uri, content_hash),
        ).fetchone()

        if existing is not None:
            conflicts = _conflicting_fields(
                existing,
                {
                    "asset_kind": asset_kind,
                    "media_type": media_type,
                    "srs_id": srs_id,
                    "metadata_json": metadata_json,
                },
            )

            if conflicts:
                raise USAPError(
                    f"Asset {uri!r} is already registered with different "
                    f"values: {conflicts}. Re-registering cannot change an "
                    "existing asset; register the new version under its own "
                    "content hash, or fix the caller."
                )

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
        indexing_profile: str | None = None,
    ) -> int:
        """
        Register a stable sub-location inside an asset.

        Returns asset_part_id.

        indexing_profile names the convention that assigned element indices
        (e.g. 'usap:ply-face-record-order-v1'). It is advisory for now, but it
        is compared like every other field below: reading one part under two
        different conventions would repoint its memberships without changing
        a single stored index.

        Idempotent on (asset_id, part_path, element_kind), with the same
        caveat as register_asset: element_count is the index space every
        annotation on this part is validated against, so a re-registration
        that changes it (or the bounds, origin, metadata, or indexing
        profile) raises rather than leaving existing memberships silently
        mis-scoped.
        """
        element_kind = normalize_element_kind(element_kind)
        if element_count < 0:
            raise USAPError("element_count cannot be negative")

        existing = self.conn.execute(
            """
            SELECT
                asset_part_id,
                element_count,
                index_origin,
                minx, miny, minz,
                maxx, maxy, maxz,
                metadata_json,
                indexing_profile
            FROM usap_asset_part
            WHERE asset_id = ?
              AND part_path = ?
              AND element_kind = ?
            """,
            (asset_id, part_path, element_kind),
        ).fetchone()

        if existing is not None:
            conflicts = _conflicting_fields(
                existing,
                {
                    "element_count": element_count,
                    "index_origin": index_origin,
                    "minx": minx,
                    "miny": miny,
                    "minz": minz,
                    "maxx": maxx,
                    "maxy": maxy,
                    "maxz": maxz,
                    "metadata_json": metadata_json,
                    "indexing_profile": indexing_profile,
                },
            )

            if conflicts:
                raise USAPError(
                    f"Asset part {part_path!r} of asset {asset_id} is already "
                    f"registered with different values: {conflicts}. Existing "
                    "annotations are indexed against the stored element "
                    "count; register the changed asset as a new version "
                    "instead."
                )

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
                    metadata_json,
                    indexing_profile
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    indexing_profile,
                ),
            )
            asset_part_id = require_lastrowid(cur)
            self._refresh_asset_extent(asset_id)
            self.log_edit(
                "register_asset_part",
                "usap_asset_part",
                asset_part_id,
            )

        return asset_part_id

    def update_asset(
        self,
        asset_id: int,
        *,
        uri: object = _UNSET,
        content_hash: object = _UNSET,
        srs_id: object = _UNSET,
        media_type: object = _UNSET,
        metadata_json: object = _UNSET,
    ) -> dict[str, Any]:
        """
        Repair an asset record in place. Omitted fields are preserved.

        register_asset is deliberately unforgiving — re-registering the same uri
        with a different kind or count raises — but that left no way to fix a
        record that has become *wrong about the same file*: the usual case is a
        project moved on disk, so the stored uri no longer resolves and
        verify_assets reports it missing.

        This does not re-index anything: element counts, parts and memberships
        are untouched, because the file is the same file. Changing content_hash
        is how you record a re-hash after moving or re-exporting a byte-identical
        asset; it is NOT a way to adopt a changed file, which would leave every
        stored index pointing at different geometry. asset_kind cannot be changed
        at all — a mesh does not become a point cloud.

        Returns the updated row.
        """
        updates: list[str] = []
        params: list[Any] = []

        for column, value in (
            ("uri", uri),
            ("content_hash", content_hash),
            ("srs_id", srs_id),
            ("media_type", media_type),
            ("metadata_json", metadata_json),
        ):
            if value is _UNSET:
                continue

            updates.append(f"{column} = ?")
            params.append(value)

        if not updates:
            row = self.conn.execute(
                """
                SELECT asset_id, uri, asset_kind, media_type, content_hash,
                       srs_id, metadata_json
                FROM usap_asset
                WHERE asset_id = ?
                """,
                (asset_id,),
            ).fetchone()

            if row is None:
                raise USAPError(f"Asset not found: {asset_id}")

            return dict(row)

        params.append(asset_id)

        with self.transaction():
            try:
                cur = self.conn.execute(
                    f"""
                    UPDATE usap_asset
                    SET {", ".join(updates)}
                    WHERE asset_id = ?
                    """,
                    params,
                )
            except sqlite3.IntegrityError as exc:
                raise USAPError(
                    f"Asset update violates a constraint: {exc}. An asset is "
                    "unique on (uri, content_hash)."
                ) from exc

            if cur.rowcount != 1:
                raise USAPError(f"Asset not found: {asset_id}")

            self.log_edit(
                "update_asset",
                "usap_asset",
                asset_id,
                details_json=json.dumps(sorted(
                    column for column, value in (
                        ("uri", uri),
                        ("content_hash", content_hash),
                        ("srs_id", srs_id),
                        ("media_type", media_type),
                        ("metadata_json", metadata_json),
                    ) if value is not _UNSET
                )),
            )

        return self.update_asset(asset_id)

    def _refresh_asset_extent(self, asset_id: int) -> None:
        """
        Upsert the asset's derived 2D extent box (the union of its parts'
        stored bounds) for the GIS features layer. Parts without full 2D
        bounds are ignored; an asset with none keeps no extent row.
        """
        has_extent_table = self.conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'usap_asset_extent'
            """
        ).fetchone()

        if has_extent_table is None:
            return  # package predates the extents layer

        box = self.conn.execute(
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

        if box is None or box["minx"] is None:
            return

        srs_row = self.conn.execute(
            """
            SELECT srs_id
            FROM gpkg_geometry_columns
            WHERE table_name = ?
            """,
            (USAP_FEATURES_LAYER,),
        ).fetchone()

        srs_id = (
            int(srs_row["srs_id"]) if srs_row is not None
            else DEFAULT_EXTENT_SRS_ID
        )

        self.conn.execute(
            """
            INSERT OR REPLACE INTO usap_asset_extent (asset_id, geom)
            VALUES (?, ?)
            """,
            (
                asset_id,
                encode_gpkg_bbox_polygon(
                    float(box["minx"]),
                    float(box["miny"]),
                    float(box["maxx"]),
                    float(box["maxy"]),
                    srs_id,
                ),
            ),
        )

    def list_assets(
        self,
        *,
        asset_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        List registered assets, each with its part and element counts.

        Pass asset_kind to keep only one kind (e.g. 'mesh', 'pointcloud').
        """
        where = ""
        params: list[Any] = []

        if asset_kind is not None:
            where = "WHERE a.asset_kind = ?"
            params.append(asset_kind)

        rows = self.conn.execute(
            f"""
            SELECT
                a.asset_id,
                a.uri,
                a.asset_kind,
                a.media_type,
                a.content_hash,
                a.srs_id,
                (
                    SELECT COUNT(*)
                    FROM usap_asset_part AS ap
                    WHERE ap.asset_id = a.asset_id
                ) AS part_count,
                (
                    SELECT COALESCE(SUM(ap.element_count), 0)
                    FROM usap_asset_part AS ap
                    WHERE ap.asset_id = a.asset_id
                ) AS element_count
            FROM usap_asset AS a
            {where}
            ORDER BY a.asset_id
            """,
            params,
        ).fetchall()

        return [dict(row) for row in rows]

    def list_asset_parts(
        self,
        *,
        asset_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        List asset parts (the stable index spaces annotations bind to).

        Pass asset_id to keep only one asset's parts. element_kind is the
        USAP integer constant (see ELEMENT_KIND_*).
        """
        where = ""
        params: list[Any] = []

        if asset_id is not None:
            where = "WHERE asset_id = ?"
            params.append(asset_id)

        rows = self.conn.execute(
            f"""
            SELECT
                asset_part_id,
                asset_id,
                part_path,
                element_kind,
                element_count,
                index_origin,
                indexing_profile,
                minx, miny, minz,
                maxx, maxy, maxz
            FROM usap_asset_part
            {where}
            ORDER BY asset_id, asset_part_id
            """,
            params,
        ).fetchall()

        return [dict(row) for row in rows]

    # ---------------------------------------------------------------------
    # Semantic classes
    # ---------------------------------------------------------------------

    # Columns a re-seed may fill in on a concept that already exists. Identity
    # (scheme, class_uri, local_name) is deliberately not here: those decide
    # *which* concept this is, so a change is a different concept, not an
    # enrichment.
    _SEMANTIC_CLASS_BACKFILLABLE = (
        "scheme_version",
        "parent_class_id",
        "source_namespace",
        "concept_iri",
        "metadata_json",
    )

    def create_semantic_class(
        self,
        scheme: str,
        class_uri: str,
        local_name: str,
        scheme_version: str | None = None,
        parent_class_id: int | None = None,
        is_ade: bool = False,
        metadata_json: str | None = None,
        source_namespace: str | None = None,
        concept_iri: str | None = None,
    ) -> int:
        """
        Create or reuse a semantic class.

        Also updates usap_semantic_class_closure.

        Re-seeding an *enriched* vocabulary over a package that already has the
        concept fills in whatever is still NULL — provenance added to a
        registry later reaches existing packages by re-seeding, instead of
        needing them rebuilt. A field that already holds a different value is a
        contradiction, not an enrichment, and raises: seeding must never
        silently rewrite what a package already asserts.
        """
        existing = self.conn.execute(
            f"""
            SELECT
                semantic_class_id,
                {", ".join(self._SEMANTIC_CLASS_BACKFILLABLE)}
            FROM usap_semantic_class
            WHERE class_uri = ?
            """,
            (class_uri,),
        ).fetchone()

        if existing is not None:
            class_id = int(existing["semantic_class_id"])

            requested = {
                "scheme_version": scheme_version,
                "parent_class_id": parent_class_id,
                "source_namespace": source_namespace,
                "concept_iri": concept_iri,
                "metadata_json": metadata_json,
            }

            backfill: dict[str, Any] = {}

            for column, value in requested.items():
                # Requesting None makes no claim, so plain re-creates and
                # re-seeds of an unchanged file stay a no-op.
                if value is None or existing[column] == value:
                    continue

                if existing[column] is not None:
                    raise USAPError(
                        f"Semantic class already exists with a different "
                        f"{column}: {class_uri!r} has "
                        f"{existing[column]!r}, requested {value!r}. "
                        "Re-seeding fills in missing fields; it does not "
                        "overwrite what a package already asserts."
                    )

                backfill[column] = value

            if backfill:
                assignments = ", ".join(f"{c} = ?" for c in backfill)

                with self.transaction():
                    self.conn.execute(
                        f"""
                        UPDATE usap_semantic_class
                        SET {assignments}
                        WHERE semantic_class_id = ?
                        """,
                        (*backfill.values(), class_id),
                    )

                    # A parent arriving late has to reach the closure too,
                    # which is otherwise only written at insert.
                    if "parent_class_id" in backfill:
                        self._inherit_closure_from_parent(
                            class_id=class_id,
                            parent_class_id=int(backfill["parent_class_id"]),
                        )

                    self.log_edit(
                        "backfill_semantic_class",
                        "usap_semantic_class",
                        class_id,
                        details_json=json.dumps(sorted(backfill)),
                    )

            return class_id

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
                    metadata_json,
                    source_namespace,
                    concept_iri
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scheme,
                    scheme_version,
                    class_uri,
                    local_name,
                    parent_class_id,
                    1 if is_ade else 0,
                    metadata_json,
                    source_namespace,
                    concept_iri,
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

            if parent_class_id is not None:
                self._inherit_closure_from_parent(
                    class_id=class_id,
                    parent_class_id=parent_class_id,
                )

            self.log_edit(
                "create_semantic_class",
                "usap_semantic_class",
                class_id,
            )

        return class_id

    def _inherit_closure_from_parent(
        self,
        *,
        class_id: int,
        parent_class_id: int,
    ) -> None:
        """
        Give a class every ancestor of its parent, one level deeper.

        Shared by the insert path and by a parent arriving later through a
        re-seed backfill: the closure is written eagerly, so a parent that
        appears after the row exists has to reach it here or subclass queries
        would keep missing the new edge.
        """
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
        """
        Register a city object under `object_uid`; returns city_object_id.

        `object_uid` is supplied, never derived: uniqueness belongs to whoever
        owns the semantic model. In carrier-only use the caller passes the
        `gml:id`, so a repeat call names the same instance and returning the
        existing row is the whole point.

        Idempotent on object_uid, and — as in register_asset — only while
        "already registered" means "registered as the same thing". A repeat
        call that supplies a *different* value for a field already stored
        raises rather than discarding it. The case this exists for: an
        annotation's concept (EnergyRoof) is usually not the object's CityGML
        class (RoofSurface), so passing the annotation's concept here used to
        vanish without a word.

        Only fields the caller actually supplies are compared, so the bare
        `create_city_object(uid)` "give me the id" call stays valid against a
        fully populated row. `object_status` is excluded: its default is a real
        value, so a supplied status cannot be told from an omitted one.
        """
        if object_status not in CITY_OBJECT_STATUSES:
            raise USAPError(
                f"Unknown city object status {object_status!r}. "
                f"Use one of: {', '.join(CITY_OBJECT_STATUSES)}."
            )

        existing = self.conn.execute(
            """
            SELECT
                city_object_id,
                semantic_class_id,
                gml_id,
                source_asset_id,
                source_object_id,
                attributes_json
            FROM usap_city_object
            WHERE object_uid = ?
            """,
            (object_uid,),
        ).fetchone()

        if existing is not None:
            conflicts = _conflicting_fields(
                existing,
                {
                    column: value
                    for column, value in (
                        ("semantic_class_id", semantic_class_id),
                        ("gml_id", gml_id),
                        ("source_asset_id", source_asset_id),
                        ("source_object_id", source_object_id),
                        ("attributes_json", attributes_json),
                    )
                    if value is not None
                },
            )

            if conflicts:
                raise USAPError(
                    f"City object {object_uid!r} already exists with different "
                    f"values: {conflicts}. Creating it again cannot change it; "
                    "the semantic source owns these fields. If you meant to "
                    "record a different concept, that belongs on the "
                    "annotation, not on the city object."
                )

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

    # ---------------------------------------------------------------------
    # Relationship types (the link vocabulary)
    # ---------------------------------------------------------------------

    def register_relationship_type(
        self,
        local_name: str,
        *,
        code_space: str | None = None,
        category: str | None = None,
        metadata_json: str | None = None,
    ) -> int:
        """
        Create or reuse one link type, keyed on (local_name, code_space).

        Idempotent and enriching, like create_semantic_class: a category that
        is still NULL is filled in by a later call, so a package can be
        classified after the edges already exist. A *different* non-NULL
        category is a contradiction and raises rather than being overwritten.

        code_space is the namespace the property came from. Leaving it NULL is
        allowed but weakens the package: 'boundedBy' with no namespace cannot
        be resolved by a reader who did not build it.
        """
        if category is not None and category not in RELATIONSHIP_CATEGORIES:
            raise USAPError(
                f"Unknown relationship category {category!r}. "
                f"Use one of: {', '.join(RELATIONSHIP_CATEGORIES)}."
            )

        cache_key = (local_name, code_space or "")

        existing = self.conn.execute(
            """
            SELECT relationship_type_id, category
            FROM usap_relationship_type
            WHERE local_name = ?
              AND COALESCE(code_space, '') = ?
            """,
            cache_key,
        ).fetchone()

        if existing is not None:
            type_id = int(existing["relationship_type_id"])

            if category is not None and existing["category"] != category:
                if existing["category"] is not None:
                    raise USAPError(
                        f"Relationship type {local_name!r} is already "
                        f"{existing['category']!r}, cannot re-register it as "
                        f"{category!r}. Registering fills in a missing "
                        "category; it does not overwrite one."
                    )

                with self.transaction():
                    self.conn.execute(
                        """
                        UPDATE usap_relationship_type
                        SET category = ?
                        WHERE relationship_type_id = ?
                        """,
                        (category, type_id),
                    )

            self._relationship_type_cache[cache_key] = type_id

            return type_id

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_relationship_type (
                    local_name,
                    code_space,
                    category,
                    metadata_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (local_name, code_space, category, metadata_json),
            )
            type_id = require_lastrowid(cur)

            self.log_edit(
                "register_relationship_type",
                "usap_relationship_type",
                type_id,
            )

        self._relationship_type_cache[cache_key] = type_id

        return type_id

    def resolve_relationship_type(
        self,
        relationship_type: int | str | tuple[str, str | None],
        *,
        code_space: str | None = None,
    ) -> int:
        """
        Resolve a link type to its id.

        Accepts an id, a local name, or a (local_name, code_space) pair. The
        pair form exists because one query routinely spans modules — asking
        for CityGML's `boundary` and `fillingSurface` together means asking
        across the core and construction namespaces, which a single code_space
        argument cannot express.

        A name resolves only within its code space. There is deliberately no
        fallback across code spaces: silently matching 'boundedBy' from some
        other namespace is exactly the identity loss this table exists to stop.
        """
        if isinstance(relationship_type, tuple):
            relationship_type, code_space = relationship_type

        if isinstance(relationship_type, int):
            row = self.conn.execute(
                """
                SELECT relationship_type_id
                FROM usap_relationship_type
                WHERE relationship_type_id = ?
                """,
                (relationship_type,),
            ).fetchone()

            if row is None:
                raise USAPError(
                    f"No relationship type with id {relationship_type}."
                )

            return int(row["relationship_type_id"])

        cache_key = (relationship_type, code_space or "")
        cached = self._relationship_type_cache.get(cache_key)

        if cached is not None:
            return cached

        row = self.conn.execute(
            """
            SELECT relationship_type_id
            FROM usap_relationship_type
            WHERE local_name = ?
              AND COALESCE(code_space, '') = ?
            """,
            cache_key,
        ).fetchone()

        if row is None:
            raise USAPError(
                f"Relationship type {relationship_type!r} is not registered"
                + (f" in code space {code_space!r}" if code_space else "")
                + ". Register it, or load the ontology that defines it."
            )

        type_id = int(row["relationship_type_id"])
        self._relationship_type_cache[cache_key] = type_id

        return type_id

    def list_relationship_types(
        self,
        *,
        category: str | None = None,
        include_unused: bool = True,
    ) -> list[dict[str, Any]]:
        """
        The package's link vocabulary, with how many edges use each type.

        category=None lists everything, including unclassified types — which
        is how you find what an import registered but no ontology classified.
        """
        where = []
        params: list[Any] = []

        # Unqualified: this filters the wrapping SELECT, where the inner
        # alias is out of scope. edge_count is only addressable there too.
        if category is not None:
            where.append("category = ?")
            params.append(category)

        if not include_unused:
            where.append("edge_count > 0")

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        rows = self.conn.execute(
            f"""
            SELECT * FROM (
                SELECT
                    rt.relationship_type_id,
                    rt.local_name,
                    rt.code_space,
                    rt.category,
                    rt.metadata_json,
                    CAST((
                        SELECT COUNT(*)
                        FROM usap_city_object_relationship AS r
                        WHERE r.relationship_type_id = rt.relationship_type_id
                    ) AS INTEGER) AS edge_count
                FROM usap_relationship_type AS rt
            )
            {where_sql}
            ORDER BY local_name, COALESCE(code_space, '')
            """,
            params,
        ).fetchall()

        return [dict(row) for row in rows]

    def _resolve_edge_type_ids(
        self,
        *,
        relationship_types: Sequence[int | str | tuple[str, str | None]] | None,
        relationship_categories: Sequence[str] | None,
        code_space: str | None = None,
        default_categories: Sequence[str] | None = None,
    ) -> tuple[int, ...] | None:
        """
        Turn a type/category filter into the integer ids a query binds.

        Deliberately resolved in Python, before the statement is built: the
        recursive descendant CTE binds these as plain '?' placeholders, so its
        CROSS JOIN body stays byte-for-byte what the planner was tuned against
        (ACCELERATOR_ABLATION.md section 5 measures 400x on that join's plan).
        Putting a subquery or a join inside the recursive term to look the
        category up would undo that.

        Returns None for "no type filter at all", () for "match nothing".
        """
        if relationship_types is None and relationship_categories is None:
            if default_categories is None:
                return None

            relationship_categories = default_categories

        ids: list[int] = []

        for category in relationship_categories or ():
            if category not in RELATIONSHIP_CATEGORIES:
                raise USAPError(
                    f"Unknown relationship category {category!r}. "
                    f"Use one of: {', '.join(RELATIONSHIP_CATEGORIES)}."
                )

            ids.extend(
                int(row["relationship_type_id"])
                for row in self.conn.execute(
                    """
                    SELECT relationship_type_id
                    FROM usap_relationship_type
                    WHERE category = ?
                    """,
                    (category,),
                ).fetchall()
            )

        # An unregistered *name* raises rather than matching nothing: a typo
        # must not look like "this object has no parts".
        for relationship_type in relationship_types or ():
            ids.append(
                self.resolve_relationship_type(
                    relationship_type,
                    code_space=code_space,
                )
            )

        return tuple(dict.fromkeys(ids))

    def link_city_objects(
        self,
        from_city_object_id: int,
        to_city_object_id: int | None,
        relationship_type: int | str | tuple[str, str | None],
        *,
        to_external_uri: str | None = None,
        code_space: str | None = None,
        category: str | None = None,
        role: str | None = None,
        graph_name: str = DEFAULT_GRAPH_NAME,
        source_asset_id: int | None = None,
        source_relation_id: str | None = None,
        metadata_json: str | None = None,
    ) -> int:
        """
        Add one typed, directed edge between two city objects.

        The edge is directed but not hierarchical: from_ -> to_ records which
        way the source asserted it, and whether that makes the target a *part*
        of the source is a property of the type's category, not of the columns.

        Exactly one of to_city_object_id / to_external_uri must be given. The
        second is for an xlink that leaves the document: the link is real and
        typed even though its target is not in this package, and dropping it is
        how an xlink-serialized CityGML file used to import as unrelated roots.

        relationship_type accepts an id or a local name. An unregistered name
        is registered on the spot — with `category`, if the caller knows it —
        so no document is refused for using a link USAP has not met. Without a
        category the edge is stored and queryable by name, but stays outside
        the default traversal and is reported by validate_report().

        Idempotent: re-linking an identical edge (same graph, endpoints, type,
        role and source_relation_id) returns the existing relationship_id.
        """
        if (to_city_object_id is None) == (to_external_uri is None):
            raise USAPError(
                "Provide exactly one of to_city_object_id (a target in this "
                "package) or to_external_uri (an xlink that leaves the "
                "document); got "
                f"to_city_object_id={to_city_object_id!r}, "
                f"to_external_uri={to_external_uri!r}."
            )

        if isinstance(relationship_type, int):
            relationship_type_id = self.resolve_relationship_type(
                relationship_type
            )
        else:
            relationship_type_id = self.register_relationship_type(
                relationship_type,
                code_space=code_space,
                category=category,
            )

        existing = self.conn.execute(
            """
            SELECT relationship_id
            FROM usap_city_object_relationship
            WHERE graph_name = ?
              AND from_city_object_id = ?
              AND to_city_object_id IS ?
              AND to_external_uri IS ?
              AND relationship_type_id = ?
              AND role IS ?
              AND source_relation_id IS ?
            """,
            (
                graph_name,
                from_city_object_id,
                to_city_object_id,
                to_external_uri,
                relationship_type_id,
                role,
                source_relation_id,
            ),
        ).fetchone()

        if existing is not None:
            return int(existing["relationship_id"])

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_city_object_relationship (
                    graph_name,
                    from_city_object_id,
                    to_city_object_id,
                    to_external_uri,
                    relationship_type_id,
                    role,
                    source_asset_id,
                    source_relation_id,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_name,
                    from_city_object_id,
                    to_city_object_id,
                    to_external_uri,
                    relationship_type_id,
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

        return relationship_id

    def list_city_objects(
        self,
        *,
        object_status: str | None = None,
        semantic_class_id: int | None = None,
        search: str | None = None,
        related_to: int | str | None = None,
        descendants_of: int | str | None = None,
        direction: str = "out",
        graph_name: str = DEFAULT_GRAPH_NAME,
        relationship_types: Sequence[int | str | tuple[str, str | None]] | None = None,
        relationship_categories: Sequence[str] | None = None,
        code_space: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        List city objects (the semantic identities annotations link to).

        Filters are AND-combined:
        - object_status — e.g. 'temporary' to find carrier objects awaiting
          alignment with a real CityGML source.
        - semantic_class_id — objects of one class.
        - search — substring match on object_uid.
        - related_to — an object id or uid; returns the objects ONE hop away
          from it in graph_name (walk the graph: list all, then expand a node).
          Defaults to every link type, because a one-hop "what is related to
          this" that silently hid `relatedTo` would be a trap.
        - descendants_of — an object id or uid; returns that object AND
          everything reachable from it at any depth, which is the set
          elements_for_city_object gathers annotations over. Defaults to
          containment types only, so a neighbour is not reported as a part.

        Both traversals take `direction` ('out', 'in', 'both') and the same
        type filter: `relationship_categories` for a class of link,
        `relationship_types` to name them exactly. An unregistered type name
        raises — a typo must not look like "this object has no parts".

        Root objects are those that `related_to=..., direction='in'` never
        returns, i.e. nothing points at them.
        """
        where: list[str] = []
        params: list[Any] = []
        join = ""
        with_sql = ""

        if descendants_of is not None:
            root_id = self.resolve_city_object(descendants_of)
            edge_type_ids = self._resolve_edge_type_ids(
                relationship_types=relationship_types,
                relationship_categories=relationship_categories,
                code_space=code_space,
                default_categories=DEFAULT_TRAVERSAL_CATEGORIES,
            ) or ()

            with_sql = _descendants_cte(len(edge_type_ids), direction)

            params.append(root_id)

            if edge_type_ids:
                params.extend([graph_name, *edge_type_ids])

                if direction == "both":
                    params.extend([graph_name, *edge_type_ids])

            where.append("co.city_object_id IN (SELECT object_id FROM objects)")

        if related_to is not None:
            anchor_id = self.resolve_city_object(related_to)
            neighbour_sql, neighbour_params = self._one_hop_neighbour_sql(
                anchor_id=anchor_id,
                direction=direction,
                graph_name=graph_name,
                relationship_types=relationship_types,
                relationship_categories=relationship_categories,
                code_space=code_space,
            )

            where.append(f"co.city_object_id IN ({neighbour_sql})")
            params.extend(neighbour_params)

        if object_status is not None:
            where.append("co.object_status = ?")
            params.append(object_status)

        if semantic_class_id is not None:
            where.append("co.semantic_class_id = ?")
            params.append(semantic_class_id)

        if search is not None:
            where.append("co.object_uid LIKE ?")
            params.append(f"%{search}%")

        where_sql = ""

        if where:
            where_sql = "WHERE " + " AND ".join(where)

        rows = self.conn.execute(
            f"""
            {with_sql}
            SELECT DISTINCT
                co.city_object_id,
                co.object_uid,
                sc.local_name AS semantic_class,
                co.object_status,
                co.gml_id,
                src.uri AS source_asset_uri
            FROM usap_city_object AS co
            {join}
            LEFT JOIN usap_semantic_class AS sc
                ON sc.semantic_class_id = co.semantic_class_id
            LEFT JOIN usap_asset AS src
                ON src.asset_id = co.source_asset_id
            {where_sql}
            ORDER BY co.object_uid
            """,
            params,
        ).fetchall()

        return [dict(row) for row in rows]

    def _one_hop_neighbour_sql(
        self,
        *,
        anchor_id: int,
        direction: str,
        graph_name: str,
        relationship_types: Sequence[int | str | tuple[str, str | None]] | None,
        relationship_categories: Sequence[str] | None,
        code_space: str | None,
    ) -> tuple[str, list[Any]]:
        """
        A subquery yielding the city objects one edge away from anchor_id.

        A UNION ALL of one indexed branch per direction, rather than a JOIN:
        a JOIN cannot express direction='both' without either duplicating rows
        or losing one side. Each branch hits its own covering index
        (usap_rel_by_from_graph / usap_rel_by_to_graph).

        No default category here: a one-hop question is "what is related to
        this", and defaulting it to containment would silently hide exactly
        the peer and grouping links this method exists to surface.
        """
        if direction not in RELATIONSHIP_DIRECTIONS:
            raise USAPError(
                f"Unknown direction {direction!r}. "
                f"Use one of: {', '.join(RELATIONSHIP_DIRECTIONS)}."
            )

        edge_type_ids = self._resolve_edge_type_ids(
            relationship_types=relationship_types,
            relationship_categories=relationship_categories,
            code_space=code_space,
            default_categories=None,
        )

        type_clause = ""
        type_params: list[Any] = []

        if edge_type_ids is not None:
            if not edge_type_ids:
                # An explicit empty filter means "match nothing", and must not
                # collapse to "no filter".
                return "SELECT NULL WHERE 0", []

            placeholders = ",".join("?" for _ in edge_type_ids)
            type_clause = f"AND r.relationship_type_id IN ({placeholders})"
            type_params = list(edge_type_ids)

        branches: list[str] = []
        params: list[Any] = []

        def add_branch(anchor_column: str, target_column: str) -> None:
            branches.append(
                f"""
                SELECT r.{target_column} AS object_id
                FROM usap_city_object_relationship AS r
                WHERE r.graph_name = ?
                  AND r.{anchor_column} = ?
                  AND r.{target_column} IS NOT NULL
                  {type_clause}
                """
            )
            params.extend([graph_name, anchor_id, *type_params])

        if direction in ("out", "both"):
            add_branch("from_city_object_id", "to_city_object_id")

        if direction in ("in", "both"):
            add_branch("to_city_object_id", "from_city_object_id")

        return " UNION ALL ".join(branches), params

    def related_city_objects(
        self,
        city_object: int | str,
        *,
        relationship_types: Sequence[int | str | tuple[str, str | None]] | None = None,
        relationship_categories: Sequence[str] | None = None,
        code_space: str | None = None,
        direction: str = "out",
        graph_name: str = DEFAULT_GRAPH_NAME,
        include_external: bool = True,
    ) -> list[dict[str, Any]]:
        """
        The edges touching one city object, as edges rather than objects.

        This is the only way to see an edge whose target is outside the
        package: list_city_objects can return rows of usap_city_object and
        nothing else, so an unresolved xlink has no object row to be. Without
        this method to_external_uri would be write-only.

        Each row carries both endpoints, the resolved link type with its
        code space and category, the role, and the provenance columns.
        `direction` decides which way edges are followed and is reported per
        row, so 'both' stays readable.

        include_external=False drops the edges that leave the document, for
        callers that only want targets they can dereference locally.
        """
        if direction not in RELATIONSHIP_DIRECTIONS:
            raise USAPError(
                f"Unknown direction {direction!r}. "
                f"Use one of: {', '.join(RELATIONSHIP_DIRECTIONS)}."
            )

        anchor_id = self.resolve_city_object(city_object)

        edge_type_ids = self._resolve_edge_type_ids(
            relationship_types=relationship_types,
            relationship_categories=relationship_categories,
            code_space=code_space,
            default_categories=None,
        )

        type_clause = ""
        type_params: list[Any] = []

        if edge_type_ids is not None:
            if not edge_type_ids:
                return []

            placeholders = ",".join("?" for _ in edge_type_ids)
            type_clause = f"AND r.relationship_type_id IN ({placeholders})"
            type_params = list(edge_type_ids)

        external_clause = (
            "" if include_external else "AND r.to_city_object_id IS NOT NULL"
        )

        branches: list[str] = []
        params: list[Any] = []

        def add_branch(anchor_column: str, label: str) -> None:
            branches.append(
                f"""
                SELECT
                    r.relationship_id,
                    '{label}' AS direction,
                    r.graph_name,
                    r.from_city_object_id,
                    src.object_uid AS from_object_uid,
                    r.to_city_object_id,
                    dst.object_uid AS to_object_uid,
                    r.to_external_uri,
                    rt.local_name AS relationship_type,
                    rt.code_space,
                    rt.category,
                    r.role,
                    r.source_relation_id,
                    r.metadata_json
                FROM usap_city_object_relationship AS r
                JOIN usap_relationship_type AS rt
                    ON rt.relationship_type_id = r.relationship_type_id
                LEFT JOIN usap_city_object AS src
                    ON src.city_object_id = r.from_city_object_id
                LEFT JOIN usap_city_object AS dst
                    ON dst.city_object_id = r.to_city_object_id
                WHERE r.graph_name = ?
                  AND r.{anchor_column} = ?
                  {type_clause}
                  {external_clause}
                """
            )
            params.extend([graph_name, anchor_id, *type_params])

        if direction in ("out", "both"):
            add_branch("from_city_object_id", "out")

        if direction in ("in", "both"):
            add_branch("to_city_object_id", "in")

        rows = self.conn.execute(
            " UNION ALL ".join(branches) + " ORDER BY relationship_id",
            params,
        ).fetchall()

        return [dict(row) for row in rows]

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
                a.status,
                a.confidence,
                a.attributes_json,
                a.created_at,
                a.updated_at
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
            self._attach_annotation_summaries(result)

        return result


    def list_annotations(
        self,
        *,
        status: str | None = None,
        semantic_class_id: int | None = None,
        semantic_class_local_name: str | None = None,
        city_object_id: int | None = None,
        city_object_uid: str | None = None,
        asset_id: int | None = None,
        asset_part_id: int | None = None,
        include_membership_summary: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        List annotations using simple prototype filters.

        Filters are AND-combined.

        city_object_id / city_object_uid matches either:
        - annotation.primary_city_object_id
        - usap_annotation_object links

        asset_id / asset_part_id keep only annotations with membership in that
        asset (or one of its parts). This is how an application loads the
        annotations belonging to the 3D asset it has just opened: without it the
        only way to ask was to walk every annotation's membership.

        include_membership_summary=True also attaches value_field_summary
        (both are per-asset-part payload rollups).
        """
        where: list[str] = []
        params: list[Any] = []

        if asset_id is not None:
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM usap_membership_block AS mb
                    JOIN usap_asset_part AS ap
                        ON ap.asset_part_id = mb.asset_part_id
                    WHERE mb.annotation_id = a.annotation_id
                      AND ap.asset_id = ?
                )
                """
            )
            params.append(asset_id)

        if asset_part_id is not None:
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM usap_membership_block AS mb
                    WHERE mb.annotation_id = a.annotation_id
                      AND mb.asset_part_id = ?
                )
                """
            )
            params.append(asset_part_id)

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
                a.status,
                a.confidence,
                a.attributes_json,
                a.created_at,
                a.updated_at
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
                self._attach_annotation_summaries(item)

        return result


    def update_annotation(
        self,
        annotation_id: int,
        *,
        annotation_uid: object = _UNSET,
        semantic_class_id: object = _UNSET,
        primary_city_object_id: object = _UNSET,
        status: object = _UNSET,
        confidence: object = _UNSET,
        attributes_json: object = _UNSET,
    ) -> dict[str, Any]:
        """
        Update annotation metadata.

        Omitted fields are preserved.
        Passing None explicitly stores NULL.

        Changing primary_city_object_id also moves the annotation's
        'represents' link in usap_annotation_object, in the same transaction,
        so the two representations of the primary object cannot diverge (see
        elements_for_city_object). Only the *old primary object's* link row is
        removed, so intentional secondary links survive the move; a separately
        added 'represents' link to the old primary object is indistinguishable
        from the primary one and is removed with it.
        """
        _check_annotation_fields(
            status=status,
            confidence=confidence,
            attributes_json=attributes_json,
        )

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
        add_update("status", status)
        add_update("confidence", confidence)
        add_update("attributes_json", attributes_json)

        if not updates:
            existing = self.get_annotation(annotation_id)

            if existing is None:
                raise USAPError(f"Annotation not found: {annotation_id}")

            return existing

        # Keep updated_at meaningful: it is stamped on creation and must
        # advance on every real edit, otherwise it just mirrors created_at.
        # The expression must match the schema default exactly (UTC ISO-8601
        # with 'Z'), or an edited row would be spelled differently from a
        # fresh one. Inline SQL, so it needs no parameter.
        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")

        params.append(annotation_id)

        with self.transaction():
            # Read before the UPDATE: afterwards the old primary object is gone
            # and its stale link row could not be found any more.
            previous_primary_object_id: int | None = None

            if primary_city_object_id is not _UNSET:
                previous_primary_object_id = self._primary_city_object_id(
                    annotation_id
                )

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

            if primary_city_object_id is not _UNSET:
                new_primary_object_id = (
                    None
                    if primary_city_object_id is None
                    else int(primary_city_object_id)
                )

                self._move_primary_object_link(
                    annotation_id,
                    previous_primary_object_id,
                    new_primary_object_id,
                )

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


    def _attach_annotation_summaries(self, item: dict[str, Any]) -> None:
        annotation_id = int(item["annotation_id"])
        item["assessment_summary"] = self._assessment_summary(annotation_id)
        item["membership_summary"] = self._annotation_membership_summary(
            annotation_id
        )
        item["value_field_summary"] = self._annotation_value_field_summary(
            annotation_id
        )


    def create_annotation(
        self,
        annotation_uid: str,
        semantic_class_id: int,
        primary_city_object_id: int | None = None,
        status: str = "accepted",
        confidence: float | None = None,
        attributes_json: str | None = None,
        link_primary_object: bool = True,
    ) -> int:
        _check_annotation_fields(
            status=status,
            confidence=confidence,
            attributes_json=attributes_json,
        )

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
                    status,
                    confidence,
                    attributes_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    annotation_uid,
                    semantic_class_id,
                    primary_city_object_id,
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
    # Assessments
    # ---------------------------------------------------------------------
    #
    # An annotation is the logical claim; an assessment is one dated evaluation
    # of it against one asset. Every membership and value block belongs to an
    # assessment, so an annotation that has membership always has at least one.
    #
    # Callers that never mention assessments keep working: the write paths
    # resolve one through _default_assessment_for, creating an undated
    # assessment on first use. Assessments only become visible once a second
    # one exists on the same asset, which is exactly when they are meaningful.

    def resolve_asset(self, asset: int | str) -> int:
        """
        Resolve an asset reference to asset_id.

        Accepted forms:
        - asset_id as int
        - uri as str

        A uri is not unique on its own — usap_asset is keyed on
        (uri, content_hash), so the same path registered at two versions is two
        assets. That case raises rather than picking one: choosing silently is
        how a claim ends up attached to the wrong version of a file.
        """
        if isinstance(asset, int):
            row = self.conn.execute(
                """
                SELECT asset_id
                FROM usap_asset
                WHERE asset_id = ?
                """,
                (asset,),
            ).fetchone()

            if row is None:
                raise USAPError(f"Asset not found: {asset}")

            return int(row["asset_id"])

        rows = self.conn.execute(
            """
            SELECT asset_id, uri, content_hash
            FROM usap_asset
            WHERE uri = ?
            ORDER BY asset_id
            """,
            (asset,),
        ).fetchall()

        if not rows:
            raise USAPError(f"Asset not found: {asset!r}")

        if len(rows) > 1:
            options = [
                {
                    "asset_id": int(row["asset_id"]),
                    "uri": row["uri"],
                    "content_hash": row["content_hash"],
                }
                for row in rows
            ]

            raise USAPAmbiguityError(
                "Asset reference is ambiguous — the same uri is registered at "
                f"several content hashes. Use asset_id. Reference: {asset!r}. "
                f"Options: {options}"
            )

        return int(rows[0]["asset_id"])

    def resolve_assessment(self, assessment: int | str) -> int:
        """
        Resolve an assessment reference (assessment_id or assessment_uid).
        """
        column = "assessment_id" if isinstance(assessment, int) else "assessment_uid"

        row = self.conn.execute(
            f"""
            SELECT assessment_id
            FROM usap_assessment
            WHERE {column} = ?
            """,
            (assessment,),
        ).fetchone()

        if row is None:
            raise USAPError(f"Assessment not found: {assessment!r}")

        return int(row["assessment_id"])

    def create_assessment(
        self,
        annotation_id: int,
        asset: int | str,
        *,
        assessed_at: str | None = None,
        assessment_uid: str | None = None,
        status: str = "accepted",
        confidence: float | None = None,
        attributes: dict[str, Any] | None = None,
        attributes_json: str | None = None,
    ) -> dict[str, Any]:
        """
        Create (or reuse) one dated evaluation of an annotation on one asset.

        Idempotent on (annotation_id, asset_id, assessed_at): re-running an
        import does not fork a claim's history. Two evaluations of the same
        asset on the same date are the same assessment; to record a genuinely
        separate one, give it its own date.

        assessed_at is free-form text, stored as given. USAP does not parse it:
        the format is the caller's (US-ANN-08 only requires "a specific date"),
        and rejecting an unfamiliar spelling would be worse than storing it.
        Leaving it None means "undated", of which there can be at most one per
        (annotation, asset) — see the partial unique index in the schema.
        """
        _check_annotation_fields(
            status=status,
            confidence=confidence,
            attributes_json=attributes_json,
        )

        if attributes is not None and attributes_json is not None:
            raise USAPError("Provide attributes or attributes_json, not both.")

        if attributes is not None:
            attributes_json = json.dumps(attributes)

        asset_id = self.resolve_asset(asset)

        existing = self.conn.execute(
            """
            SELECT assessment_id
            FROM usap_assessment
            WHERE annotation_id = ?
              AND asset_id = ?
              AND assessed_at IS ?
            """,
            (annotation_id, asset_id, assessed_at),
        ).fetchone()

        if existing is not None:
            assessment = self.get_assessment(int(existing["assessment_id"]))

            if assessment is None:  # pragma: no cover - just read above
                raise USAPError("Assessment disappeared after lookup.")

            return assessment

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

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO usap_assessment (
                    assessment_uid,
                    annotation_id,
                    asset_id,
                    assessed_at,
                    status,
                    confidence,
                    attributes_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_uid or f"asm_{uuid.uuid4().hex}",
                    annotation_id,
                    asset_id,
                    assessed_at,
                    status,
                    confidence,
                    attributes_json,
                ),
            )
            assessment_id = require_lastrowid(cur)
            self.log_edit(
                "create_assessment",
                "usap_assessment",
                assessment_id,
            )

        assessment = self.get_assessment(assessment_id)

        if assessment is None:  # pragma: no cover - just inserted
            raise USAPError(
                f"Assessment disappeared after creation: {assessment_id}"
            )

        return assessment

    def get_assessment(
        self,
        assessment_id: int | None = None,
        *,
        assessment_uid: str | None = None,
        include_membership_summary: bool = True,
    ) -> dict[str, Any] | None:
        """
        Read one assessment by id or uid. Returns None if not found.
        """
        if (assessment_id is None) == (assessment_uid is None):
            raise USAPError(
                "Provide exactly one of assessment_id or assessment_uid."
            )

        if assessment_uid is not None:
            where_sql = "asm.assessment_uid = ?"
            params: list[Any] = [assessment_uid]
        else:
            where_sql = "asm.assessment_id = ?"
            params = [assessment_id]

        row = self.conn.execute(
            f"""
            SELECT
                asm.assessment_id,
                asm.assessment_uid,
                asm.annotation_id,
                a.annotation_uid,
                asm.asset_id,
                asset.uri AS asset_uri,
                asset.asset_kind,
                asset.content_hash AS asset_content_hash,
                asm.assessed_at,
                asm.status,
                asm.confidence,
                asm.attributes_json,
                asm.created_at,
                asm.updated_at
            FROM usap_assessment AS asm
            JOIN usap_annotation AS a
                ON a.annotation_id = asm.annotation_id
            JOIN usap_asset AS asset
                ON asset.asset_id = asm.asset_id
            WHERE {where_sql}
            """,
            params,
        ).fetchone()

        if row is None:
            return None

        result = dict(row)

        if include_membership_summary:
            result["membership_summary"] = self._assessment_membership_summary(
                int(result["assessment_id"])
            )

        return result

    def list_assessments(
        self,
        *,
        annotation_id: int | None = None,
        asset_id: int | None = None,
        include_membership_summary: bool = True,
    ) -> list[dict[str, Any]]:
        """
        List assessments, newest date last.

        This is US-ANN-08's "view the list of all assessments associated with
        the same annotation, distinguishing them at least by date and 3D
        asset": both are columns here, and the membership summary says how much
        of the asset each one covers.

        Undated assessments sort first: they are the implicitly created ones,
        which predate any deliberate dating of the claim.
        """
        where: list[str] = []
        params: list[Any] = []

        if annotation_id is not None:
            where.append("asm.annotation_id = ?")
            params.append(annotation_id)

        if asset_id is not None:
            where.append("asm.asset_id = ?")
            params.append(asset_id)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        rows = self.conn.execute(
            f"""
            SELECT
                asm.assessment_id,
                asm.assessment_uid,
                asm.annotation_id,
                a.annotation_uid,
                asm.asset_id,
                asset.uri AS asset_uri,
                asset.asset_kind,
                asm.assessed_at,
                asm.status,
                asm.confidence,
                asm.attributes_json,
                asm.created_at,
                asm.updated_at
            FROM usap_assessment AS asm
            JOIN usap_annotation AS a
                ON a.annotation_id = asm.annotation_id
            JOIN usap_asset AS asset
                ON asset.asset_id = asm.asset_id
            {where_sql}
            ORDER BY
                asm.annotation_id,
                asm.assessed_at IS NOT NULL,
                asm.assessed_at,
                asm.assessment_id
            """,
            params,
        ).fetchall()

        result = [dict(row) for row in rows]

        if include_membership_summary:
            for item in result:
                item["membership_summary"] = self._assessment_membership_summary(
                    int(item["assessment_id"])
                )

        return result

    def update_assessment(
        self,
        assessment_id: int,
        *,
        assessed_at: object = _UNSET,
        status: object = _UNSET,
        confidence: object = _UNSET,
        attributes_json: object = _UNSET,
    ) -> dict[str, Any]:
        """
        Update assessment metadata. Omitted fields are preserved; passing None
        explicitly stores NULL.

        The asset is deliberately not updatable: an assessment's membership is
        indexed against that asset's parts, so re-pointing it would silently
        make every stored index refer to different geometry. Record a new
        assessment on the other asset instead.
        """
        _check_annotation_fields(
            status=status,
            confidence=confidence,
            attributes_json=attributes_json,
        )

        updates: list[str] = []
        params: list[Any] = []

        for column, value in (
            ("assessed_at", assessed_at),
            ("status", status),
            ("confidence", confidence),
            ("attributes_json", attributes_json),
        ):
            if value is _UNSET:
                continue

            updates.append(f"{column} = ?")
            params.append(value)

        if not updates:
            existing = self.get_assessment(assessment_id)

            if existing is None:
                raise USAPError(f"Assessment not found: {assessment_id}")

            return existing

        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        params.append(assessment_id)

        with self.transaction():
            try:
                cur = self.conn.execute(
                    f"""
                    UPDATE usap_assessment
                    SET {", ".join(updates)}
                    WHERE assessment_id = ?
                    """,
                    params,
                )
            except sqlite3.IntegrityError as exc:
                raise USAPError(
                    f"Assessment update violates a constraint: {exc}. An "
                    "assessment is unique per (annotation, asset, date)."
                ) from exc

            if cur.rowcount != 1:
                raise USAPError(f"Assessment not found: {assessment_id}")

            self.log_edit(
                "update_assessment",
                "usap_assessment",
                assessment_id,
            )

        updated = self.get_assessment(assessment_id)

        if updated is None:  # pragma: no cover - just updated
            raise USAPError(
                f"Assessment disappeared after update: {assessment_id}"
            )

        return updated

    def delete_assessment(
        self,
        assessment_id: int,
        *,
        missing_ok: bool = False,
    ) -> bool:
        """
        Delete one assessment and its membership/value blocks (ON DELETE
        CASCADE). The annotation and every other assessment of it survive.

        Returns True if an assessment was deleted, False only when
        missing_ok=True and it did not exist.
        """
        with self.transaction():
            cur = self.conn.execute(
                """
                DELETE FROM usap_assessment
                WHERE assessment_id = ?
                """,
                (assessment_id,),
            )

            if cur.rowcount == 0:
                if missing_ok:
                    return False

                raise USAPError(f"Assessment not found: {assessment_id}")

            self.log_edit(
                "delete_assessment",
                "usap_assessment",
                assessment_id,
            )

        return True

    def _assessment_membership_summary(
        self,
        assessment_id: int,
    ) -> list[dict[str, Any]]:
        """
        Which asset parts one assessment touches, and how much of each.
        """
        rows = self.conn.execute(
            """
            SELECT
                mb.asset_part_id,
                ap.part_path,
                ap.element_kind,
                SUM(mb.element_count) AS selected_count,
                COUNT(*) AS block_count
            FROM usap_membership_block AS mb
            JOIN usap_asset_part AS ap
                ON ap.asset_part_id = mb.asset_part_id
            WHERE mb.assessment_id = ?
            GROUP BY mb.asset_part_id, ap.part_path, ap.element_kind
            ORDER BY mb.asset_part_id
            """,
            (assessment_id,),
        ).fetchall()

        return [dict(row) for row in rows]

    def _assessment_summary(self, annotation_id: int) -> list[dict[str, Any]]:
        """
        One compact row per assessment of an annotation, for get_annotation.
        """
        rows = self.conn.execute(
            """
            SELECT
                asm.assessment_id,
                asm.assessment_uid,
                asm.asset_id,
                asset.uri AS asset_uri,
                asm.assessed_at,
                asm.status,
                CAST(COALESCE((
                    SELECT SUM(mb.element_count)
                    FROM usap_membership_block AS mb
                    WHERE mb.assessment_id = asm.assessment_id
                ), 0) AS INTEGER) AS selected_count
            FROM usap_assessment AS asm
            JOIN usap_asset AS asset
                ON asset.asset_id = asm.asset_id
            WHERE asm.annotation_id = ?
            ORDER BY
                asm.assessed_at IS NOT NULL,
                asm.assessed_at,
                asm.assessment_id
            """,
            (annotation_id,),
        ).fetchall()

        return [dict(row) for row in rows]

    def _default_assessment_for(
        self,
        annotation_id: int,
        asset_part_id: int,
    ) -> int:
        """
        The assessment a write means when the caller named none.

        Resolves the part's asset, then: exactly one assessment of this
        annotation on that asset is the answer; none means create an undated
        one; several is ambiguous and raises, because picking the newest (or
        the oldest) would silently rewrite a historical evaluation.

        This is what keeps every pre-assessment call site working unchanged.
        """
        asset_row = self.conn.execute(
            """
            SELECT asset_id
            FROM usap_asset_part
            WHERE asset_part_id = ?
            """,
            (asset_part_id,),
        ).fetchone()

        if asset_row is None:
            raise USAPError(f"Unknown asset_part_id: {asset_part_id}")

        asset_id = int(asset_row["asset_id"])

        rows = self.conn.execute(
            """
            SELECT assessment_id, assessment_uid, assessed_at
            FROM usap_assessment
            WHERE annotation_id = ?
              AND asset_id = ?
            ORDER BY assessment_id
            """,
            (annotation_id, asset_id),
        ).fetchall()

        if len(rows) == 1:
            return int(rows[0]["assessment_id"])

        if not rows:
            created = self.create_assessment(annotation_id, asset_id)

            return int(created["assessment_id"])

        options = [
            {
                "assessment_id": int(row["assessment_id"]),
                "assessment_uid": row["assessment_uid"],
                "assessed_at": row["assessed_at"],
            }
            for row in rows
        ]

        raise USAPAmbiguityError(
            f"Annotation {annotation_id} has {len(rows)} assessments on this "
            "asset, so the target of this write is ambiguous. Pass "
            f"assessment=... to say which. Options: {options}"
        )

    def _assessment_for_new_claim(
        self,
        *,
        annotation_id: int,
        asset_part_id: int,
        assessed_at: str | None,
    ) -> int | None:
        """
        Turn an `assessed_at` on a create-and-attach call into an assessment.

        None means "let the write path pick the default", which is the
        single-evaluation case and stays exactly as it was before assessments
        existed. A date creates (or reuses) that dated evaluation here, so the
        caller does not have to create an assessment first just to name it.
        """
        if assessed_at is None:
            return None

        asset_row = self.conn.execute(
            """
            SELECT asset_id
            FROM usap_asset_part
            WHERE asset_part_id = ?
            """,
            (asset_part_id,),
        ).fetchone()

        if asset_row is None:
            raise USAPError(f"Unknown asset_part_id: {asset_part_id}")

        assessment = self.create_assessment(
            annotation_id,
            int(asset_row["asset_id"]),
            assessed_at=assessed_at,
        )

        return int(assessment["assessment_id"])

    def _resolve_write_assessment(
        self,
        *,
        annotation_id: int,
        asset_part_id: int,
        assessment: int | str | None,
    ) -> int:
        """
        Resolve an explicit assessment for a write, or fall back to the
        default, checking that it belongs to this annotation and to the asset
        the part lives in.

        Checked here rather than only in validate_report(): a block written
        under another annotation's assessment is not a report-later problem,
        it is a claim attributed to the wrong annotation.
        """
        if assessment is None:
            return self._default_assessment_for(annotation_id, asset_part_id)

        assessment_id = self.resolve_assessment(assessment)

        row = self.conn.execute(
            """
            SELECT
                asm.annotation_id,
                asm.asset_id,
                ap.asset_id AS part_asset_id
            FROM usap_assessment AS asm
            CROSS JOIN usap_asset_part AS ap
                ON ap.asset_part_id = ?
            WHERE asm.assessment_id = ?
            """,
            (asset_part_id, assessment_id),
        ).fetchone()

        if row is None:
            raise USAPError(f"Unknown asset_part_id: {asset_part_id}")

        if int(row["annotation_id"]) != annotation_id:
            raise USAPError(
                f"Assessment {assessment!r} belongs to annotation "
                f"{int(row['annotation_id'])}, not {annotation_id}."
            )

        if int(row["asset_id"]) != int(row["part_asset_id"]):
            raise USAPError(
                f"Assessment {assessment!r} evaluates asset "
                f"{int(row['asset_id'])}, but asset part {asset_part_id} "
                f"belongs to asset {int(row['part_asset_id'])}. An "
                "assessment's membership must stay within its own asset."
            )

        return assessment_id

    # ---------------------------------------------------------------------
    # Membership editing
    # ---------------------------------------------------------------------

    def _validate_membership_indices(
        self,
        asset_part_id: int,
        element_kind: int,
        element_indices: IndexArray,
    ) -> np.ndarray:
        """
        Normalize a selection and check it against the part's index space.

        Returns a sorted, duplicate-free uint32 array: a selection over a
        10 GB point cloud can be hundreds of millions of indices, which is
        the one place where holding them as Python ints does not fit.
        """
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

        # as_index_array sorts, de-duplicates, and rejects negatives; sorted
        # means the upper bound is the last element.
        unique_indices = as_index_array(element_indices)

        if unique_indices.size and int(unique_indices[-1]) >= element_count:
            raise USAPError(
                f"Element index {int(unique_indices[-1])} is out of range. "
                f"Asset part has {element_count} elements."
            )

        return unique_indices

    def replace_annotation_membership(
        self,
        annotation_id: int,
        asset_part_id: int,
        element_kind: int,
        element_indices: IndexArray,
        encoding: str = DEFAULT_ENCODING,
        *,
        assessment: int | str | None = None,
    ) -> None:
        """
        Replace all membership blocks for one assessment in one asset part.

        This is an edit operation, so we validate first and write in a transaction.

        `assessment` names which evaluation of the annotation is being edited.
        Omitting it means the annotation's only assessment on this part's asset,
        created (undated) if there is none — so a caller that never heard of
        assessments keeps the pre-0.4.0 behaviour. It raises rather than guessing
        once a second assessment exists on that asset.

        Blocks are always sized at the package default
        (usap_profile.default_block_size). The reverse query
        annotations_for_elements derives block boundaries from that single
        global size, so membership must not be written at any other size or the
        reverse lookup would silently miss it.
        """
        element_kind = normalize_element_kind(element_kind)
        if encoding != DEFAULT_ENCODING:
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
            # Inside the transaction: resolving may *create* the default
            # assessment, which must roll back with the blocks it was made for.
            assessment_id = self._resolve_write_assessment(
                annotation_id=annotation_id,
                asset_part_id=asset_part_id,
                assessment=assessment,
            )

            self.conn.execute(
                """
                DELETE FROM usap_membership_block
                WHERE assessment_id = ?
                AND asset_part_id = ?
                AND element_kind = ?
                """,
                (
                    assessment_id,
                    asset_part_id,
                    element_kind,
                ),
            )

            for block_start, offsets in blocks.items():
                payload = encode_roaring(offsets)

                # int(): sqlite3 has no adapter for numpy scalars and would
                # store them as BLOBs.
                min_element_index = block_start + int(offsets[0])
                max_element_index = block_start + int(offsets[-1])

                self.conn.execute(
                    """
                    INSERT INTO usap_membership_block (
                        assessment_id,
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assessment_id,
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
                f'{{"assessment_id": {assessment_id}, '
                f'"asset_part_id": {asset_part_id}, '
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

    def _primary_city_object_id(self, annotation_id: int) -> int | None:
        row = self.conn.execute(
            """
            SELECT primary_city_object_id
            FROM usap_annotation
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        if row is None or row["primary_city_object_id"] is None:
            return None

        return int(row["primary_city_object_id"])

    def _move_primary_object_link(
        self,
        annotation_id: int,
        previous_city_object_id: int | None,
        new_city_object_id: int | None,
    ) -> None:
        """
        Keep usap_annotation_object in step with primary_city_object_id.

        Caller must already be inside a transaction, so the column and the link
        row move together or not at all.

        Re-stating the same primary object is not a no-op: the insert repairs a
        missing link row (annotations created with link_primary_object=False).
        """
        if (
            previous_city_object_id is not None
            and previous_city_object_id != new_city_object_id
        ):
            self.conn.execute(
                """
                DELETE FROM usap_annotation_object
                WHERE annotation_id = ?
                  AND city_object_id = ?
                  AND relation_type = 'represents'
                """,
                (annotation_id, previous_city_object_id),
            )

        if new_city_object_id is not None:
            self._link_annotation_object(
                annotation_id,
                new_city_object_id,
                "represents",
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
            "assessment_id": int(row["assessment_id"]),
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
            offsets = decode_roaring_array(row["payload"])
            # Vectorized add, then one conversion: the per-element Python
            # loop was the expensive half of expanding a large selection.
            item["elements"] = (
                offsets.astype(np.int64) + int(row["block_start"])
            ).tolist()

        return item


    def annotations_for_elements(
        self,
        asset_part_id: int,
        element_kind: int,
        selected_indices: list[int],
        *,
        assessment: int | str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Core reverse query:

            selected faces/points -> annotations

        One entry per (annotation, assessment): an annotation evaluated twice on
        this asset answers twice, each tagged with assessment_id and
        assessed_at, because the two cover different elements and collapsing
        them would report an extent no single evaluation ever claimed. While an
        annotation has one assessment — the default — this is one entry per
        annotation, as before.

        Pass `assessment` to restrict the search to a single evaluation.

        Optimized version:
        - groups selected indices by block_start
        - queries all relevant block_start values in one SQL query
        - decodes only candidate membership blocks
        """
        element_kind = normalize_element_kind(element_kind)
        assessment_id = (
            None if assessment is None else self.resolve_assessment(assessment)
        )

        selected = as_index_array(selected_indices)

        if selected.size == 0:
            return []

        block_size = self.get_default_block_size()

        # Same block split as the write path, so a selection the size of the
        # asset costs one pass over an array rather than a Python loop.
        #
        # Built as BitMaps once here rather than per row: several annotations
        # share a block_start, and each would otherwise rebuild the same
        # selection bitmap before intersecting it.
        selected_by_block = {
            block_start: BitMap(offsets)
            for block_start, offsets in split_indices_into_blocks(
                selected, block_size
            ).items()
        }

        block_starts = sorted(selected_by_block)

        # Chunked so a huge selection cannot exceed the SQLite variable limit.
        # Chunks partition block_starts, so each candidate block row appears
        # exactly once and the merge below is unaffected.
        rows: list[sqlite3.Row] = []

        assessment_clause = ""
        assessment_params: tuple[Any, ...] = ()

        if assessment_id is not None:
            assessment_clause = "AND mb.assessment_id = ?"
            assessment_params = (assessment_id,)

        for chunk_index in range(0, len(block_starts), _MAX_SQL_IN_VARS):
            chunk = block_starts[chunk_index : chunk_index + _MAX_SQL_IN_VARS]
            placeholders = ",".join("?" for _ in chunk)

            rows.extend(
                self.conn.execute(
                    f"""
                    SELECT
                        mb.annotation_id,
                        mb.assessment_id,
                        mb.block_start,
                        mb.payload,

                        a.annotation_uid,
                        a.status,

                        asm.assessment_uid,
                        asm.assessed_at,

                        sc.local_name AS semantic_class,
                        sc.class_uri AS semantic_class_uri,

                        co.object_uid AS primary_city_object_uid,
                        co.gml_id AS primary_city_object_gml_id
                    FROM usap_membership_block AS mb
                    JOIN usap_annotation AS a
                        ON a.annotation_id = mb.annotation_id
                    JOIN usap_assessment AS asm
                        ON asm.assessment_id = mb.assessment_id
                    JOIN usap_semantic_class AS sc
                        ON sc.semantic_class_id = a.semantic_class_id
                    LEFT JOIN usap_city_object AS co
                        ON co.city_object_id = a.primary_city_object_id
                    WHERE mb.asset_part_id = ?
                    AND mb.element_kind = ?
                    AND mb.block_start IN ({placeholders})
                    {assessment_clause}
                    ORDER BY
                        mb.block_start,
                        mb.annotation_id
                    """,
                    (
                        asset_part_id,
                        element_kind,
                        *chunk,
                        *assessment_params,
                    ),
                ).fetchall()
            )

        # Keyed on (annotation, assessment): one annotation evaluated twice on
        # this asset is two answers, not one merged extent.
        matches: dict[tuple[int, int], dict[str, Any]] = {}

        for row in rows:
            block_start = int(row["block_start"])
            selected_offsets = selected_by_block[block_start]

            # Roaring intersects container by container, so a candidate block
            # that shares no container with the selection costs almost nothing.
            hit = selected_offsets & decode_roaring_bitmap(row["payload"])

            if not hit:
                continue

            hit_offsets = roaring_to_array(hit)

            key = (int(row["annotation_id"]), int(row["assessment_id"]))

            if key not in matches:
                matches[key] = {
                    "annotation_id": key[0],
                    "annotation_uid": row["annotation_uid"],
                    "assessment_id": key[1],
                    "assessment_uid": row["assessment_uid"],
                    "assessed_at": row["assessed_at"],
                    "status": row["status"],
                    "semantic_class": row["semantic_class"],
                    "semantic_class_uri": row["semantic_class_uri"],
                    "primary_city_object_uid": row["primary_city_object_uid"],
                    "primary_city_object_gml_id": row["primary_city_object_gml_id"],
                    "matched_elements": [],
                }

            matches[key]["matched_elements"].extend(
                (hit_offsets.astype(np.int64) + block_start).tolist()
            )

        results = list(matches.values())

        for result in results:
            result["matched_elements"] = sorted(set(result["matched_elements"]))

        return results

    def elements_for_annotation(
        self,
        annotation_id: int,
        expand: bool = True,
        *,
        assessment: int | str | None = None,
        asset_part_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Forward query:

            annotation -> membership blocks/elements

        With expand=False, this returns compact block metadata.
        With expand=True, this returns actual element indices.

        Every block carries its assessment_id. By default blocks from *all* of
        the annotation's assessments are returned, so nothing is hidden from a
        caller who does not know about them; pass `assessment` to get one
        evaluation alone, which is what US-ANN-08's "highlight only the elements
        of that assessment" needs.

        `asset_part_id` narrows to one index space — the app editing a
        membership only ever holds one part's indices, and filtering here saves
        it decoding the rest.
        """
        where = ["annotation_id = ?"]
        params: list[Any] = [annotation_id]

        if assessment is not None:
            where.append("assessment_id = ?")
            params.append(self.resolve_assessment(assessment))

        if asset_part_id is not None:
            where.append("asset_part_id = ?")
            params.append(asset_part_id)

        rows = self.conn.execute(
            f"""
            SELECT
                membership_block_id,
                annotation_id,
                assessment_id,
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
            WHERE {" AND ".join(where)}
            ORDER BY assessment_id, asset_part_id, element_kind, block_start
            """,
            params,
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
                mb.assessment_id,
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
        link_types: Sequence[str] = ("represents",),
        relationship_types: Sequence[int | str | tuple[str, str | None]] | None = None,
        relationship_categories: Sequence[str] = DEFAULT_TRAVERSAL_CATEGORIES,
        direction: str = "out",
        code_space: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query a city object and optionally its descendants, then return
        annotation membership blocks.

        An annotation counts as belonging to a city object if it is linked via
        usap_annotation_object OR names it as its primary_city_object_id. Those
        two are kept in agreement by the write paths (create_annotation and
        update_annotation both maintain the 'represents' link), but matching
        both means an annotation can never silently drop out of this query if
        they ever diverge.

        Two independent type filters, on two different tables:

        link_types selects which usap_annotation_object rows count as
        "this annotation belongs to that object". It defaults to
        ('represents',) because other link types (concerns, derivedFrom, ...)
        say something *about* an object without claiming its elements. Pass
        more types to follow them too, or an empty sequence to match on
        primary_city_object_id alone. This is the annotation->object axis and
        is unrelated to the object->object one below.

        relationship_categories / relationship_types select which
        usap_city_object_relationship edges count as "part of", i.e. which the
        descendant expansion follows. The default is the containment category:
        the graph is typed, so a peer edge (adjacentTo, predecessor, ...) must
        NOT make its target's annotations answer for the source object. Name
        types explicitly to override, or pass an empty sequence to walk none.

        **include_descendants defaults to True, and expands over the link graph
        stored in this package.** A package built without importing a semantic
        source has no such graph: city objects created on demand as identity
        carriers have no edges between them. The expansion then finds no
        descendants and this returns the object's own elements alone -- no
        error, no warning, because an object with no children is a normal
        thing. So "this Building and its surfaces" silently becomes "this
        Building", which is indistinguishable from a correct answer.

        An application that owns the hierarchy elsewhere -- a CityGML document
        it parsed itself -- should walk that hierarchy and pass the resulting
        set to elements_for_city_objects() instead of relying on this
        expansion. See docs/HANDOFF.md.
        """
        relation_types = tuple(link_types)

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

        edge_type_ids: tuple[int, ...] = ()

        if include_descendants:
            edge_type_ids = self._resolve_edge_type_ids(
                relationship_types=relationship_types,
                relationship_categories=relationship_categories,
                code_space=code_space,
                default_categories=DEFAULT_TRAVERSAL_CATEGORIES,
            ) or ()

        objects_cte = _descendants_cte(len(edge_type_ids), direction)
        params: list[Any] = [city_object_id]

        if edge_type_ids:
            params.extend([graph_name, *edge_type_ids])

            if direction == "both":
                params.extend([graph_name, *edge_type_ids])

        link_branch = ""

        if relation_types:
            link_placeholders = ",".join("?" for _ in relation_types)
            link_branch = f"""
                SELECT ao.annotation_id
                FROM usap_annotation_object AS ao
                WHERE ao.city_object_id IN (SELECT object_id FROM objects)
                  AND ao.relation_type IN ({link_placeholders})
                UNION
            """
            params.extend(relation_types)

        rows = self.conn.execute(
            f"""
            {objects_cte}
            SELECT
                mb.membership_block_id,
                mb.annotation_id,
                mb.assessment_id,
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
                {link_branch}
                SELECT a.annotation_id
                FROM usap_annotation AS a
                WHERE a.primary_city_object_id IN (
                    SELECT object_id FROM objects
                )
            )
            ORDER BY
                mb.asset_part_id,
                mb.element_kind,
                mb.block_start,
                mb.annotation_id
            """,
            params,
        ).fetchall()

        return [
            self._membership_block_row_to_dict(row, expand=expand)
            for row in rows
        ]

    def elements_for_city_objects(
        self,
        object_uids: Sequence[str],
        *,
        expand: bool = False,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """
        elements_for_city_object over a set of objects, de-duplicated.

        For the common integration where the CityGML hierarchy lives outside
        USAP: the application walks its own object tree, then asks for the whole
        subtree's elements in one call instead of one call per uid. Keyword
        arguments are passed through unchanged.

        Blocks are keyed by membership_block_id, so an annotation reached
        through two of the given objects is returned once.
        """
        blocks: dict[int, dict[str, Any]] = {}

        for object_uid in object_uids:
            for block in self.elements_for_city_object(
                object_uid,
                expand=expand,
                **kwargs,
            ):
                blocks[int(block["membership_block_id"])] = block

        return [blocks[key] for key in sorted(blocks)]

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
        *,
        assessment: int | str | None = None,
    ) -> None:
        """
        Replace the whole value field for one assessment on one asset part.

        Editing is whole-field rewrite by design (write-once, read-many).
        Dtype resolution: explicit value_dtype > the ndarray's own dtype when
        it is in VALUE_DTYPES > 'f4'.

        `assessment` behaves exactly as in replace_annotation_membership: a
        field measured again at a later date is that date's assessment, not a
        rewrite of the earlier measurement.
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
            assessment_id = self._resolve_write_assessment(
                annotation_id=annotation_id,
                asset_part_id=asset_part_id,
                assessment=assessment,
            )

            self.conn.execute(
                """
                DELETE FROM usap_value_block
                WHERE assessment_id = ?
                AND asset_part_id = ?
                AND element_kind = ?
                """,
                (assessment_id, asset_part_id, element_kind),
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
                        assessment_id,
                        annotation_id,
                        asset_part_id,
                        element_kind,
                        block_start,
                        element_count,
                        value_dtype,
                        encoding,
                        value_min,
                        value_max,
                        payload
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assessment_id,
                        annotation_id,
                        asset_part_id,
                        element_kind,
                        block_start,
                        len(chunk),
                        value_dtype,
                        VALUE_BLOCK_ENCODING,
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
        status: str = "draft",
        confidence: float | None = None,
        attributes: dict[str, Any] | None = None,
        attributes_json: str | None = None,
        scheme: str | None = None,
        assessed_at: str | None = None,
    ) -> dict[str, Any]:
        """
        Create an annotation for a concept and attach a per-element value
        field in one step:

            this asset part carries these values, meaning this concept.

        The concept must be registered (CityGML, ADE, or a minimal local
        vocabulary — any scheme). Field metadata (unit, validAt, method, ...)
        belongs in `attributes`. There are deliberately no city-object
        parameters: a value field is a property of the geometry asset.

        `assessed_at` dates the measurement. Omitting it stores the field in the
        annotation's undated assessment, which is what a single-measurement
        workflow wants.
        """
        element_kind = normalize_element_kind(element_kind)

        with self.transaction():
            annotation = self.create_concept_annotation(
                concept=concept,
                annotation_uid=annotation_uid,
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
                assessment=self._assessment_for_new_claim(
                    annotation_id=annotation_id,
                    asset_part_id=asset_part_id,
                    assessed_at=assessed_at,
                ),
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
        assessment: int | str | None = None,
    ) -> list[sqlite3.Row]:
        """
        Fetch one annotation's value blocks (optionally filtered) and check
        that they belong to exactly one (assessment, asset part, element kind)
        triple, share one dtype, and tile the whole asset part (v1 full
        coverage) — so every reader enforces the same contract.

        The assessment has to be part of that identity: the same field measured
        at two dates is two complete fields over the same part, which without
        this check would look like one field covering it twice.
        """
        where = ["vb.annotation_id = ?"]
        params: list[Any] = [annotation_id]

        if assessment is not None:
            where.append("vb.assessment_id = ?")
            params.append(self.resolve_assessment(assessment))

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
                vb.assessment_id,
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
            ORDER BY
                vb.assessment_id,
                vb.asset_part_id,
                vb.element_kind,
                vb.block_start
            """,
            params,
        ).fetchall()

        if not rows:
            raise USAPError(
                f"No value field found for annotation {annotation_id}."
            )

        assessments = {int(r["assessment_id"]) for r in rows}

        if len(assessments) > 1:
            raise USAPError(
                f"Annotation {annotation_id} has value fields in several "
                f"assessments: {sorted(assessments)}. Pass assessment=... to "
                "say which evaluation to read."
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
        assessment: int | str | None = None,
    ) -> np.ndarray:
        """
        Forward query: annotation -> its dense value array.

        Element i's value is result[i]; the array always spans the whole
        asset part (v1 contract), with NaN marking "no value" in float fields.

        `assessment` picks one evaluation when the annotation has been measured
        more than once; without it, several measurements are an error rather
        than an arbitrary choice.
        """
        if element_kind is not None:
            element_kind = normalize_element_kind(element_kind)

        rows = self._value_blocks_for_annotation(
            annotation_id, asset_part_id, element_kind, assessment
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
        assessment: int | str | None = None,
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
            annotation_id, asset_part_id, None, assessment
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
        assessment: int | str | None = None,
    ) -> dict[str, Any]:
        """
        Field stats from the stored per-block min/max — no payload decode.

        min/max ignore NaN; count is the total number of stored values
        (NaN included).
        """
        where = ["vb.annotation_id = ?"]
        params: list[Any] = [annotation_id]

        if assessment is not None:
            where.append("vb.assessment_id = ?")
            params.append(self.resolve_assessment(assessment))

        if asset_part_id is not None:
            where.append("vb.asset_part_id = ?")
            params.append(asset_part_id)

        rows = self.conn.execute(
            f"""
            SELECT
                vb.assessment_id,
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
            GROUP BY
                vb.assessment_id,
                vb.asset_part_id,
                vb.element_kind,
                vb.value_dtype
            """,
            params,
        ).fetchall()

        if not rows:
            raise USAPError(
                f"No value field found for annotation {annotation_id}."
            )

        if len(rows) > 1:
            assessments = {int(r["assessment_id"]) for r in rows}

            if len(assessments) > 1:
                raise USAPError(
                    f"Annotation {annotation_id} has value fields in several "
                    f"assessments: {sorted(assessments)}. Pass assessment=... "
                    "to say which evaluation to read."
                )

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
            "assessment_id": int(row["assessment_id"]),
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

    def validate_report(self, level: str = "deep"):
        """
        Return a structured validation report.

        level is 'basic' (SQL structure only, no payload decoding), 'deep'
        (the default: every payload checked, plus graph and domain rules), or
        'external' (also re-hashes every registered asset file). See
        validation.validate_connection.
        """
        return validate_connection(self.conn, level=level)

    # ---------------------------------------------------------------------
    # Concept-level API
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
        resolve_semantic_class(
            "http://www.opengis.net/citygml/construction/3.0#RoofSurface"
        )
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
                sc.source_namespace,
                sc.concept_iri,
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
        status: str = "draft",
        confidence: float | None = None,
        attributes: dict[str, Any] | None = None,
        attributes_json: str | None = None,
        scheme: str | None = None,
        assessed_at: str | None = None,
    ) -> dict[str, Any]:
        """
        Create an annotation for a concept and immediately attach selected elements.

        This is the main prototype API:

            annotate this asset part, these element indices, as this concept.

        The concept can be CityGML, ADE, or any registered semantic class.

        `assessed_at` dates this evaluation. Omitting it puts the selection in
        the annotation's undated assessment — the ordinary single-pass case. To
        record a *re-assessment* of an existing annotation, do not call this
        again (it would mint a second annotation): create_assessment on the
        existing annotation, then attach_annotation_elements to it.
        """
        element_kind = normalize_element_kind(element_kind)
        with self.transaction():
            annotation = self.create_concept_annotation(
                concept=concept,
                annotation_uid=annotation_uid,
                city_object_id=city_object_id,
                city_object_uid=city_object_uid,
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
                assessment=self._assessment_for_new_claim(
                    annotation_id=annotation_id,
                    asset_part_id=asset_part_id,
                    assessed_at=assessed_at,
                ),
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
        assessment: int | str | None = None,
    ) -> dict[str, Any]:
        """
        Attach or replace selected elements for one assessment on one asset part.

        This is a clearer prototype name for replace_annotation_membership.
        It preserves memberships on other asset parts, and on the annotation's
        other assessments.

        Pass `assessment` (from create_assessment or list_assessments) to attach
        a re-assessment's geometry; omit it for the single-evaluation case.
        """
        self.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=asset_part_id,
            element_kind=element_kind,
            element_indices=element_indices,
            assessment=assessment,
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

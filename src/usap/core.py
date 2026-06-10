from __future__ import annotations

import os
import sqlite3
from collections import defaultdict, deque
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any


from .errors import USAPError
from .constants import DEFAULT_BLOCK_SIZE, DEFAULT_ENCODING
from .encoding import encode_u32_zlib, decode_u32_zlib, block_start_for_index, split_indices_into_blocks
from .sqlite_utils import require_lastrowid
from .validation import validate_connection
from .geopackage import initialize_geopackage_metadata

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

        Example:

            with pkg.transaction():
                pkg.create_city_object(...)
                pkg.create_annotation(...)
                pkg.replace_annotation_membership(...)
        """
        if self._transaction_depth > 0 or self.conn.in_transaction:
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1
            return

        self._transaction_depth = 1

        try:
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
        schema_path: str | Path = "schema.sql",
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
    # Encoding / decoding membership payloads
    # ---------------------------------------------------------------------

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
        metadata_json: str | None = None,
    ) -> int:
        """
        Register a stable sub-location inside an asset.

        Returns asset_part_id.
        """
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
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    part_path,
                    element_kind,
                    element_count,
                    index_origin,
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
            SELECT semantic_class_id
            FROM usap_semantic_class
            WHERE class_uri = ?
            """,
            (class_uri,),
        ).fetchone()

        if existing is not None:
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
        graph_name: str = "usap_default",
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
        graph_name: str = "usap_default",
    ) -> None:
        """
        Rebuild graph-aware ancestor/descendant closure.

        For phase 1 we assume usap_default is a practical navigation graph.
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
            SELECT annotation_id
            FROM usap_annotation
            WHERE annotation_uid = ?
            """,
            (annotation_uid,),
        ).fetchone()

        if existing is not None:
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

        for index in unique_indices:
            if index < 0:
                raise USAPError(f"Negative element index: {index}")
            if index >= element_count:
                raise USAPError(
                    f"Element index {index} is out of range. "
                    f"Asset part has {element_count} elements."
                )

        return unique_indices

    def replace_annotation_membership(
        self,
        annotation_id: int,
        asset_part_id: int,
        element_kind: int,
        element_indices: list[int],
        block_size: int | None = None,
        encoding: str = DEFAULT_ENCODING,
    ) -> None:
        """
        Replace all membership blocks for one annotation in one asset part.

        This is an edit operation, so we validate first and write in a transaction.
        """
        if encoding != "u32-zlib":
            raise USAPError(f"Unsupported encoding in phase 1: {encoding}")

        if block_size is None:
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
                (annotation_id, asset_part_id, element_kind),
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

        placeholders = ",".join("?" for _ in block_starts)

        rows = self.conn.execute(
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
                *block_starts,
            ),
        ).fetchall()

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
        graph_name: str = "usap_default",
        expand: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Query a city object and optionally its descendants, then return
        annotation membership blocks.
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

        placeholders = ",".join("?" for _ in object_ids)

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
                SELECT DISTINCT ao.annotation_id
                FROM usap_annotation_object AS ao
                WHERE ao.city_object_id IN ({placeholders})
            )
            ORDER BY
                mb.asset_part_id,
                mb.element_kind,
                mb.block_start,
                mb.annotation_id
            """,
            object_ids,
        ).fetchall()

        return [self._membership_block_row_to_dict(row, expand=expand) for row in rows]

    # ---------------------------------------------------------------------
    # Basic validation
    # ---------------------------------------------------------------------

    def validate_report(self):
        """
        Return a structured validation report.

        This is the preferred validation API.
        """

        return validate_connection(self.conn)


    def validate_basic(self) -> list[str]:
        """
        Backward-compatible simple validation API.

        Returns formatted validation issues as strings.
        """
        report = self.validate_report()
        return [issue.format() for issue in report.issues]
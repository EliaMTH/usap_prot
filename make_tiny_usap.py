# from __future__ import annotations

import os
import sqlite3
import struct
import zlib
from collections import defaultdict


DB_PATH = "demo.usap.gpkg"
SCHEMA_PATH = "schema.sql"

ELEMENT_KIND_FACE = 1 # 1 = face, 2 = point, 3 = vertex, 4 = feature
BLOCK_SIZE = 4096
ENCODING = "u32-zlib" #might be another one, this is just good for now


def encode_u32_zlib(offsets: list[int]) -> bytes:
    """
    Encode sorted uint32 offsets using little-endian uint32 + zlib.

    Offsets are local to a block.
    Example:
        face 6000 in block_start 4096 becomes offset 1904.
    """
    offsets = sorted(set(offsets))

    for value in offsets:
        if value < 0:
            raise ValueError(f"Negative offset: {value}")
        if value > 2**32 - 1:
            raise ValueError(f"Offset too large for uint32: {value}")

    raw = struct.pack("<" + "I" * len(offsets), *offsets)
    return zlib.compress(raw)


def split_indices_into_blocks(indices: list[int], block_size: int) -> dict[int, list[int]]:
    """
    Convert absolute element indices into block-local offsets.

    Example:
        indices = [100, 101, 6000]
        block_size = 4096

    Returns:
        {
            0: [100, 101],
            4096: [1904]
        }
    """
    blocks: dict[int, list[int]] = defaultdict(list)

    for index in sorted(set(indices)):
        if index < 0:
            raise ValueError(f"Negative element index: {index}")

        block_start = (index // block_size) * block_size
        offset = index - block_start
        blocks[block_start].append(offset)

    return dict(blocks)


def main() -> None:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        conn.executescript(schema_sql)

        with conn:
            # -----------------------------------------------------------------
            # 1. Package profile
            # -----------------------------------------------------------------
            conn.execute(
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
                (1, "0.1.0", BLOCK_SIZE, ENCODING, None),
            )

            # -----------------------------------------------------------------
            # 2. Register one external mesh asset
            # -----------------------------------------------------------------
            conn.execute(
                """
                INSERT INTO usap_asset (
                    asset_id,
                    uri,
                    asset_kind,
                    media_type,
                    content_hash,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "city_mesh.glb",
                    "mesh",
                    "model/gltf-binary",
                    "fake_hash_for_phase_0",
                    None,
                ),
            )

            # -----------------------------------------------------------------
            # 3. Register one asset part
            #
            # Face indices are meaningful only inside this asset part.
            # -----------------------------------------------------------------
            conn.execute(
                """
                INSERT INTO usap_asset_part (
                    asset_part_id,
                    asset_id,
                    part_path,
                    element_kind,
                    element_count,
                    index_origin,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    1,
                    "node=0/mesh=0/primitive=0",
                    ELEMENT_KIND_FACE,
                    10000,
                    "zero_based",
                    None,
                ),
            )

            # -----------------------------------------------------------------
            # 4. Register semantic classes
            # -----------------------------------------------------------------
            conn.execute(
                """
                INSERT INTO usap_semantic_class (
                    semantic_class_id,
                    scheme,
                    scheme_version,
                    class_uri,
                    local_name,
                    parent_class_id,
                    is_ade,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "citygml",
                    "3.0",
                    "citygml-3.0:bldg:Building",
                    "Building",
                    None,
                    0,
                    None,
                ),
            )

            conn.execute(
                """
                INSERT INTO usap_semantic_class (
                    semantic_class_id,
                    scheme,
                    scheme_version,
                    class_uri,
                    local_name,
                    parent_class_id,
                    is_ade,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    2,
                    "citygml",
                    "3.0",
                    "citygml-3.0:bldg:RoofSurface",
                    "RoofSurface",
                    None,
                    0,
                    None,
                ),
            )

            # Minimal semantic closure.
            # In this tiny example, each class is only its own descendant.
            conn.executemany(
                """
                INSERT INTO usap_semantic_class_closure (
                    ancestor_class_id,
                    descendant_class_id,
                    depth
                )
                VALUES (?, ?, ?)
                """,
                [
                    (1, 1, 0),
                    (2, 2, 0),
                ],
            )

            # -----------------------------------------------------------------
            # 5. Create city objects
            # -----------------------------------------------------------------
            conn.execute(
                """
                INSERT INTO usap_city_object (
                    city_object_id,
                    object_uid,
                    semantic_class_id,
                    gml_id,
                    source_asset_id,
                    source_object_id,
                    object_status,
                    attributes_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "building_1",
                    1,
                    None,
                    None,
                    None,
                    "accepted",
                    None,
                ),
            )

            conn.execute(
                """
                INSERT INTO usap_city_object (
                    city_object_id,
                    object_uid,
                    semantic_class_id,
                    gml_id,
                    source_asset_id,
                    source_object_id,
                    object_status,
                    attributes_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    2,
                    "building_1_roof_1",
                    2,
                    None,
                    None,
                    None,
                    "accepted",
                    None,
                ),
            )

            # -----------------------------------------------------------------
            # 6. Link building -> roof in usap_default
            # -----------------------------------------------------------------
            conn.execute(
                """
                INSERT INTO usap_city_object_relationship (
                    relationship_id,
                    graph_name,
                    parent_city_object_id,
                    child_city_object_id,
                    relationship_type,
                    role,
                    source_asset_id,
                    source_relation_id,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "usap_default",
                    1,
                    2,
                    "boundedBy",
                    "roof",
                    None,
                    None,
                    None,
                ),
            )

            # Minimal city-object closure.
            # building_1 contains itself and its roof in usap_default.
            conn.executemany(
                """
                INSERT INTO usap_city_object_closure (
                    graph_name,
                    ancestor_city_object_id,
                    descendant_city_object_id,
                    depth
                )
                VALUES (?, ?, ?, ?)
                """,
                [
                    ("usap_default", 1, 1, 0),
                    ("usap_default", 2, 2, 0),
                    ("usap_default", 1, 2, 1),
                ],
            )

            # -----------------------------------------------------------------
            # 7. Create one roof annotation
            # -----------------------------------------------------------------
            conn.execute(
                """
                INSERT INTO usap_annotation (
                    annotation_id,
                    annotation_uid,
                    semantic_class_id,
                    primary_city_object_id,
                    label,
                    status,
                    confidence,
                    attributes_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "ann_building_1_roof_mesh",
                    2,
                    2,
                    "Roof of building_1 in mesh",
                    "accepted",
                    1.0,
                    None,
                ),
            )

            conn.execute(
                """
                INSERT INTO usap_annotation_object (
                    annotation_id,
                    city_object_id,
                    relation_type
                )
                VALUES (?, ?, ?)
                """,
                (1, 2, "represents"),
            )

            # -----------------------------------------------------------------
            # 8. Store exact face membership
            #
            # Faces:
            #   100, 101, 102 are in block_start 0
            #   6000, 6001 are in block_start 4096
            # -----------------------------------------------------------------
            face_indices = [100, 101, 102, 6000, 6001]
            blocks = split_indices_into_blocks(face_indices, BLOCK_SIZE)

            for block_start, offsets in blocks.items():
                payload = encode_u32_zlib(offsets)

                min_index = block_start + min(offsets)
                max_index = block_start + max(offsets)

                conn.execute(
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
                        1,
                        1,
                        ELEMENT_KIND_FACE,
                        block_start,
                        BLOCK_SIZE,
                        ENCODING,
                        len(offsets),
                        min_index,
                        max_index,
                        payload,
                    ),
                )

            conn.execute(
                """
                INSERT INTO usap_edit_log (
                    operation,
                    target_table,
                    target_id,
                    details_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    "create_tiny_demo",
                    "usap_annotation",
                    1,
                    '{"faces": [100, 101, 102, 6000, 6001]}',
                ),
            )

        print(f"Created {DB_PATH}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
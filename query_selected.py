from __future__ import annotations

import sqlite3
import struct
import zlib
from collections import defaultdict


DB_PATH = "demo.usap.gpkg"

ELEMENT_KIND_FACE = 1
BLOCK_SIZE = 4096
ENCODING = "u32-zlib"


def decode_u32_zlib(payload: bytes) -> list[int]:
    raw = zlib.decompress(payload)

    if len(raw) % 4 != 0:
        raise ValueError("Invalid u32 payload length")

    count = len(raw) // 4

    if count == 0:
        return []

    return list(struct.unpack("<" + "I" * count, raw))


def block_start_for_index(index: int, block_size: int) -> int:
    return (index // block_size) * block_size


def annotations_for_elements(
    conn: sqlite3.Connection,
    asset_part_id: int,
    element_kind: int,
    selected_indices: list[int],
) -> list[dict]:
    """
    Given selected element indices, return matching annotations.

    This version groups results by annotation, so the same annotation
    is returned once even if the selected faces touch multiple blocks.
    """
    selected_by_block: dict[int, set[int]] = defaultdict(set)

    for index in selected_indices:
        block_start = block_start_for_index(index, BLOCK_SIZE)
        offset = index - block_start
        selected_by_block[block_start].add(offset)

    matches_by_annotation: dict[int, dict] = {}

    for block_start, selected_offsets in selected_by_block.items():
        rows = conn.execute(
            """
            SELECT
                mb.annotation_id,
                mb.block_start,
                mb.payload,
                a.annotation_uid,
                a.label,
                co.object_uid AS city_object_uid,
                sc.local_name AS semantic_class
            FROM usap_membership_block AS mb
            JOIN usap_annotation AS a
                ON a.annotation_id = mb.annotation_id
            LEFT JOIN usap_city_object AS co
                ON co.city_object_id = a.primary_city_object_id
            JOIN usap_semantic_class AS sc
                ON sc.semantic_class_id = a.semantic_class_id
            WHERE mb.asset_part_id = ?
              AND mb.element_kind = ?
              AND mb.block_start = ?
            """,
            (asset_part_id, element_kind, block_start),
        ).fetchall()

        for row in rows:
            (
                annotation_id,
                row_block_start,
                payload,
                annotation_uid,
                label,
                city_object_uid,
                semantic_class,
            ) = row

            encoded_offsets = set(decode_u32_zlib(payload))
            intersection = selected_offsets.intersection(encoded_offsets)

            if not intersection:
                continue

            absolute_matches = [
                row_block_start + offset
                for offset in intersection
            ]

            if annotation_id not in matches_by_annotation:
                matches_by_annotation[annotation_id] = {
                    "annotation_id": annotation_id,
                    "annotation_uid": annotation_uid,
                    "label": label,
                    "city_object_uid": city_object_uid,
                    "semantic_class": semantic_class,
                    "matched_elements": [],
                }

            matches_by_annotation[annotation_id]["matched_elements"].extend(
                absolute_matches
            )

    results = list(matches_by_annotation.values())

    for result in results:
        result["matched_elements"] = sorted(set(result["matched_elements"]))

    return results


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        selected_faces = [100, 101, 6000]

        matches = annotations_for_elements(
            conn=conn,
            asset_part_id=1,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=selected_faces,
        )

        print("Selected faces:", selected_faces)
        print()

        if not matches:
            print("No annotations found.")
            return

        for match in matches:
            print("Annotation:", match["annotation_uid"])
            print("Label:", match["label"])
            print("Semantic class:", match["semantic_class"])
            print("City object:", match["city_object_uid"])
            print("Matched faces:", match["matched_elements"])
            print()

    finally:
        conn.close()


if __name__ == "__main__":
    main()
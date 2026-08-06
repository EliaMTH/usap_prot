"""
Membership encoding: the one place USAP handles data at asset scale.

A selection over a 10 GB point cloud is hundreds of millions of element
indices, so these tests pin the properties that make that survivable —
accepting arrays without a Python-list round trip, normalizing without a
per-element loop, and refusing a payload that would decompress unbounded.
"""

from __future__ import annotations

import zlib

import numpy as np
import pytest

from conftest import make_mesh_part, make_pkg
from usap import ELEMENT_KIND_FACE, USAPError
from usap.encoding import (
    MAX_DECOMPRESSED_BYTES,
    as_index_array,
    decode_u32_zlib,
    encode_u32_zlib,
    split_indices_into_blocks,
)


def test_index_normalization_sorts_and_deduplicates() -> None:
    values = as_index_array([5, 1, 5, 3, 1])

    assert values.tolist() == [1, 3, 5]
    assert values.dtype == np.uint32


def test_index_normalization_accepts_arrays_and_lists_alike() -> None:
    # A selection tool hands over an ndarray; a JSON batch hands over a list.
    # Neither should be privileged, and neither should change the result.
    array_form = as_index_array(np.array([9, 2, 2, 7], dtype=np.int64))
    list_form = as_index_array([9, 2, 2, 7])

    assert array_form.tolist() == list_form.tolist() == [2, 7, 9]


def test_non_integer_indices_are_refused() -> None:
    # Truncating 3.5 to 3 would annotate a different element than the caller
    # named, silently.
    with pytest.raises(USAPError, match="must be integers"):
        as_index_array([1.0, 3.5])

    # A float that is exactly integral is a legitimate spelling (JSON has no
    # integer type of its own).
    assert as_index_array([1.0, 3.0]).tolist() == [1, 3]


def test_negative_and_oversized_indices_are_refused() -> None:
    with pytest.raises(USAPError, match="Negative element index"):
        as_index_array([-1, 2])

    with pytest.raises(USAPError, match="too large for uint32"):
        as_index_array([2**32])


def test_blocks_split_on_block_boundaries() -> None:
    blocks = split_indices_into_blocks([0, 4095, 4096, 8192], block_size=4096)

    assert sorted(blocks) == [0, 4096, 8192]
    assert blocks[0].tolist() == [0, 4095]
    assert blocks[4096].tolist() == [0]
    assert blocks[8192].tolist() == [0]


def test_roundtrip_preserves_offsets() -> None:
    offsets = [0, 1, 2, 4095]

    assert decode_u32_zlib(encode_u32_zlib(offsets)) == offsets


def test_decoding_refuses_an_oversized_payload() -> None:
    # A membership payload is decompressed before anything has checked how
    # big it will get. Without a ceiling, a few hundred KB of crafted input
    # expands to gigabytes and takes the process with it — the package format
    # is meant to be exchangeable, so this cannot depend on trusting the file.
    compressor = zlib.compressobj()
    chunk = b"\0" * (1024 * 1024)
    parts = [
        compressor.compress(chunk)
        for _ in range(MAX_DECOMPRESSED_BYTES // len(chunk) + 8)
    ]
    parts.append(compressor.flush())
    bomb = b"".join(parts)

    assert len(bomb) < 1024 * 1024  # tiny on the wire, huge on decode

    with pytest.raises(USAPError, match="decompresses to more than"):
        decode_u32_zlib(bomb)


def test_membership_write_accepts_a_numpy_selection(tmp_path) -> None:
    # The end-to-end property: an ndarray selection never has to be converted
    # to a Python list to be stored, and stores identically to one that was.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg, element_count=20_000)
        class_id = pkg.create_semantic_class(
            scheme="s", class_uri="s:Roof", local_name="Roof"
        )

        selection = np.arange(0, 12_000, 3, dtype=np.int64)

        annotations = []

        for uid, indices in [
            ("ann_array", selection),
            ("ann_list", selection.tolist()),
        ]:
            annotation_id = pkg.create_annotation(
                annotation_uid=uid, semantic_class_id=class_id
            )
            pkg.replace_annotation_membership(
                annotation_id=annotation_id,
                asset_part_id=part,
                element_kind=ELEMENT_KIND_FACE,
                element_indices=indices,
            )
            annotations.append(annotation_id)

        expanded = [
            [
                element
                for block in pkg.elements_for_annotation(annotation_id, expand=True)
                for element in block["elements"]
            ]
            for annotation_id in annotations
        ]

        assert expanded[0] == expanded[1] == selection.tolist()

        # Stored values must be SQLite integers, not numpy scalars smuggled
        # in as BLOBs.
        kinds = {
            type(row[0]).__name__
            for row in pkg.conn.execute(
                "SELECT min_element_index FROM usap_membership_block"
            )
        }

        assert kinds == {"int"}

        assert pkg.validate_report().is_ok

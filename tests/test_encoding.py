"""
Membership encoding: the one place USAP handles data at asset scale.

A selection over a 10 GB point cloud is hundreds of millions of element
indices, so these tests pin the properties that make that survivable —
accepting arrays without a Python-list round trip, normalizing without a
per-element loop, and refusing a payload a hostile package could hand over.

They also pin the reason membership is stored as roaring rather than as a
codec of our own: the payload must be readable by any roaring implementation.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest
from pyroaring import BitMap

from conftest import make_mesh_part, make_pkg
from usap import ELEMENT_KIND_FACE, USAPError
from usap.encoding import (
    as_index_array,
    decode_roaring,
    encode_roaring,
    split_indices_into_blocks,
)

# CRoaring portable-format cookies (the values every implementation agrees on).
SERIAL_COOKIE_NO_RUNCONTAINER = 12346
SERIAL_COOKIE = 12347


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


@pytest.mark.parametrize(
    "dtype",
    [np.uint8, np.uint16, np.uint32, np.uint64, np.int16, np.int32, np.int64],
)
def test_index_normalization_does_not_depend_on_the_input_dtype(dtype) -> None:
    # A selection over a large asset is the case this module exists for, and a
    # caller holding one as uint32 to halve its memory is doing the obvious
    # thing. The monotonicity fast path used to be written as
    # np.diff(values) > 0, which subtracts in the input's dtype: on an unsigned
    # one a descending step wrapped to a huge positive, every unsigned array
    # was taken for sorted, and the indices came back in the order given.
    values = as_index_array(np.array([200, 5, 30, 5], dtype=dtype))

    assert values.tolist() == [5, 30, 200]
    assert values.dtype == np.uint32


def test_oversized_index_is_refused_wherever_it_sits() -> None:
    # The uint32 range check reads the last element, which is only the upper
    # bound once the array is sorted. An unsorted uint64 input hid 2**40 behind
    # a small final element and astype(uint32) then wrapped it to 0 — the
    # annotation silently moved to a different element.
    with pytest.raises(USAPError, match="too large for uint32"):
        as_index_array(np.array([2**40, 5], dtype=np.uint64))


def test_unsorted_selection_is_range_checked_against_the_asset_part(tmp_path) -> None:
    # The range check in _validate_membership_indices tests the last element
    # only, trusting as_index_array to have sorted. When that trust was
    # misplaced for unsigned input, an out-of-range index was accepted and
    # written, and the corruption surfaced only at validate_report() time.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg, element_count=100)
        semantic_class_id = pkg.create_semantic_class(
            scheme="local",
            class_uri="http://example.org/usap/Roof",
            local_name="Roof",
        )
        annotation_id = pkg.create_annotation(
            annotation_uid="ann-unsorted",
            semantic_class_id=semantic_class_id,
        )

        with pytest.raises(USAPError, match="out of range"):
            pkg.replace_annotation_membership(
                annotation_id,
                part,
                ELEMENT_KIND_FACE,
                np.array([500, 5], dtype=np.uint32),
            )


def test_blocks_split_on_block_boundaries() -> None:
    blocks = split_indices_into_blocks([0, 4095, 4096, 8192], block_size=4096)

    assert sorted(blocks) == [0, 4096, 8192]
    assert blocks[0].tolist() == [0, 4095]
    assert blocks[4096].tolist() == [0]
    assert blocks[8192].tolist() == [0]


def test_normalized_indices_split_into_blocks_without_loss() -> None:
    # split_indices_into_blocks reads the sorted order to find its boundaries,
    # so unsorted input does not merely reorder the result — it drops elements.
    # [5, 20000, 6, 20001] as uint32 used to survive normalization unsorted and
    # come back out of the split as two indices instead of four, no error
    # raised anywhere. Normalization is what that contract rests on.
    indices = as_index_array(np.array([5, 20000, 6, 20001], dtype=np.uint32))
    blocks = split_indices_into_blocks(indices, block_size=16384)

    recovered = sorted(
        int(block_start) + int(offset)
        for block_start, offsets in blocks.items()
        for offset in offsets
    )

    assert recovered == [5, 6, 20000, 20001]


def test_roundtrip_preserves_offsets() -> None:
    offsets = [0, 1, 2, 4095]

    assert decode_roaring(encode_roaring(offsets)) == offsets


def test_a_run_of_offsets_costs_a_fraction_of_the_indices_it_names() -> None:
    # The reason a roof or wall surface is cheap to store: it exports as a
    # contiguous face range, which roaring keeps as a run container instead of
    # as one integer per element. If this ever regresses to a per-element
    # encoding the package still round-trips, so only size catches it.
    payload = encode_roaring(range(4096))

    assert len(payload) < 64


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("empty", b""),
        ("truncated", encode_roaring([1, 2, 3])[:6]),
        ("not roaring at all", bytes(range(64))),
        ("bad cookie", struct.pack("<I", 999) + encode_roaring([1, 2, 3])[4:]),
    ],
)
def test_decoding_refuses_a_malformed_payload(name: str, payload: bytes) -> None:
    # The package format is meant to be exchangeable, so decoding cannot
    # depend on trusting the file. Roaring removes the decompression-bomb
    # shape of this threat outright — it is a structural format, so a small
    # payload cannot expand into a large allocation — but it decodes in C,
    # and a malformed payload must surface as a USAPError rather than taking
    # the process down or being read as if it were valid.
    with pytest.raises(USAPError, match="Corrupt roaring payload"):
        decode_roaring(payload)


def test_payload_is_readable_by_any_roaring_implementation(tmp_path) -> None:
    # This is *why* membership is roaring rather than a codec of our own: a
    # stored payload is CRoaring's portable serialization, so a Java, Go or
    # C++ reader can decode it without this SDK. Asserted here against bare
    # pyroaring, with no USAP code in the decode path.
    offsets = [0, 1, 2, 500, 4095]

    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg, element_count=4096)
        class_id = pkg.create_semantic_class(
            scheme="s", class_uri="s:Roof", local_name="Roof"
        )
        annotation_id = pkg.create_annotation(
            annotation_uid="ann_interop", semantic_class_id=class_id
        )
        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=part,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=offsets,
        )

        payload = pkg.conn.execute(
            "SELECT payload FROM usap_membership_block"
        ).fetchone()[0]

    assert list(BitMap.deserialize(payload)) == offsets

    cookie = struct.unpack("<I", payload[:4])[0]
    assert cookie & 0xFFFF in (SERIAL_COOKIE, SERIAL_COOKIE_NO_RUNCONTAINER)


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

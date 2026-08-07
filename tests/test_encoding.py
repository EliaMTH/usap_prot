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


def test_blocks_split_on_block_boundaries() -> None:
    blocks = split_indices_into_blocks([0, 4095, 4096, 8192], block_size=4096)

    assert sorted(blocks) == [0, 4096, 8192]
    assert blocks[0].tolist() == [0, 4095]
    assert blocks[4096].tolist() == [0]
    assert blocks[8192].tolist() == [0]


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

"""
The reverse query, annotations_for_elements: which blocks it decodes, and that
both ways of choosing them give today's answer.

A sparse selection goes in two steps (stored [min, max] prefilter, then the
payloads of the blocks left); a dense one reads its blocks by block_start in
one pass. The route is an implementation detail: every test here pins the
answer, in order, against a brute-force oracle and across both routes.
"""

from __future__ import annotations

import math

import pytest

import usap.core as core
from conftest import make_mesh_part
from usap import ELEMENT_KIND_FACE, USAPPackage
from usap.constants import DEFAULT_BLOCK_SIZE
from usap.encoding import decode_roaring_array

BLOCK = DEFAULT_BLOCK_SIZE
PART_SIZE = 4 * BLOCK


def _asset_of(pkg: USAPPackage, part: int) -> int:
    return pkg.conn.execute(
        "SELECT asset_id FROM usap_asset_part WHERE asset_part_id = ?", (part,)
    ).fetchone()[0]


def _oracle(
    pkg: USAPPackage,
    part: int,
    selection: list[int],
    assessment_id: int | None = None,
) -> list[tuple[int, int, list[int]]]:
    """
    The answer worked out by decoding every block of the part.

    (annotation_id, assessment_id, matched elements) per entry, in the order
    entries are first hit walking the blocks by (block_start, annotation_id,
    membership_block_id) -- the order annotations_for_elements reports.
    """
    selected = {int(index) for index in selection}
    found: dict[tuple[int, int], set[int]] = {}

    for row in pkg.conn.execute(
        """
        SELECT membership_block_id, annotation_id, assessment_id, block_start,
               payload
        FROM usap_membership_block
        WHERE asset_part_id = ? AND element_kind = ?
        ORDER BY block_start, annotation_id, membership_block_id
        """,
        (part, ELEMENT_KIND_FACE),
    ):
        if assessment_id is not None and row["assessment_id"] != assessment_id:
            continue

        elements = {
            int(offset) + row["block_start"]
            for offset in decode_roaring_array(row["payload"])
        }
        hit = elements & selected

        if hit:
            key = (row["annotation_id"], row["assessment_id"])
            found.setdefault(key, set()).update(hit)

    return [(a, s, sorted(e)) for (a, s), e in found.items()]


def _summary(hits: list[dict]) -> list[tuple[int, int, list[int]]]:
    return [(h["annotation_id"], h["assessment_id"], h["matched_elements"]) for h in hits]


def _route_spy(monkeypatch) -> list[int]:
    """Records each time the two-step route builds its prefilter query."""
    calls: list[int] = []
    real = core._candidate_blocks_sql

    def spy(row_count: int, **kwargs):
        calls.append(row_count)
        return real(row_count, **kwargs)

    monkeypatch.setattr(core, "_candidate_blocks_sql", spy)

    return calls


@pytest.fixture
def city(pkg: USAPPackage) -> dict[str, int]:
    """
    One mesh part of four blocks and claims shaped to stress the prefilter:
    a sparse one across every block, a contiguous strip, one interleaved with
    the sparse one over three blocks, and one annotation with two assessments
    in the same block -- written so that block id order and assessment id
    order disagree, which is what decides which of the two is reported first.
    """
    part = make_mesh_part(pkg, element_count=PART_SIZE)
    roof = pkg.create_semantic_class("test", "test:Roof", "Roof")

    def claim(uid: str, indices) -> int:
        return int(
            pkg.annotate_elements(
                concept=roof,
                annotation_uid=uid,
                asset_part_id=part,
                element_kind="face",
                element_indices=list(indices),
            )["annotation_id"]
        )

    ids = {
        "sparse": claim("sparse", range(0, PART_SIZE, 1000)),
        "strip": claim("strip", range(100, 600)),
        "interleaved": claim("interleaved", range(500, 3 * BLOCK, 7)),
    }

    twice = pkg.create_annotation(annotation_uid="twice", semantic_class_id=roof)
    asset = _asset_of(pkg, part)
    undated = pkg.create_assessment(twice, asset)["assessment_id"]
    dated = pkg.create_assessment(twice, asset, assessed_at="2027-01-01")[
        "assessment_id"
    ]

    # The later assessment's block is written first.
    pkg.replace_annotation_membership(
        twice, part, "face", list(range(55, 70)), assessment=dated
    )
    pkg.replace_annotation_membership(
        twice, part, "face", list(range(50, 60)), assessment=undated
    )

    return {**ids, "twice": twice, "undated": undated, "dated": dated, "part": part}


def test_one_element_decodes_only_the_blocks_whose_range_holds_it(
    pkg: USAPPackage, monkeypatch
) -> None:
    # Forty claims side by side in one block all start at block_start 0, so
    # choosing candidates by block_start alone decoded all forty to find the
    # one holding the picked face (on the Catania mesh: 6,993 decoded, 2 hit).
    # The stored [min, max] range rules the other 39 out without a decode.
    part = make_mesh_part(pkg, element_count=BLOCK)
    roof = pkg.create_semantic_class("test", "test:Roof", "Roof")

    for i in range(40):
        pkg.annotate_elements(
            concept=roof,
            annotation_uid=f"strip_{i}",
            asset_part_id=part,
            element_kind="face",
            element_indices=list(range(i * 100, i * 100 + 50)),
        )

    decoded: list[bytes] = []
    real_decode = core.decode_roaring_bitmap

    def counting_decode(payload: bytes):
        decoded.append(payload)
        return real_decode(payload)

    monkeypatch.setattr(core, "decode_roaring_bitmap", counting_decode)

    hits = pkg.annotations_for_elements(part, "face", [1025])

    assert [h["annotation_uid"] for h in hits] == ["strip_10"]
    assert hits[0]["matched_elements"] == [1025]
    assert len(decoded) == 1


SELECTIONS = {
    "one element, two assessments": [55],
    "across blocks": [55, 120, 1000, BLOCK + 3000, 2 * BLOCK + 7, 3 * BLOCK + 2000],
    "interleaved": list(range(490, 520)) + list(range(BLOCK, BLOCK + 40)),
    "nothing there": [3 * BLOCK + 1, 3 * BLOCK + 2],
    "empty": [],
    "dense block": list(range(BLOCK)),
    "whole part": list(range(PART_SIZE)),
}


@pytest.mark.parametrize("name", list(SELECTIONS))
def test_both_routes_give_the_same_answer_in_the_same_order(
    pkg: USAPPackage, city: dict[str, int], monkeypatch, name: str
) -> None:
    selection = SELECTIONS[name]
    part = city["part"]
    expected = _oracle(pkg, part, selection)

    by_default = pkg.annotations_for_elements(part, "face", selection)

    monkeypatch.setattr(core, "_DENSE_SELECTION_FRACTION", 2.0)  # never dense
    two_step = pkg.annotations_for_elements(part, "face", selection)

    monkeypatch.setattr(core, "_DENSE_SELECTION_FRACTION", 0.0)  # always dense
    by_block_start = pkg.annotations_for_elements(part, "face", selection)

    assert _summary(by_default) == expected
    assert two_step == by_default
    assert by_block_start == by_default


def test_two_assessments_in_one_block_keep_their_order(
    pkg: USAPPackage, city: dict[str, int]
) -> None:
    # Pairs like these are why the order includes membership_block_id: both
    # entries share block_start and annotation_id, and the block written
    # first is reported first -- here the later assessment's.
    hits = pkg.annotations_for_elements(city["part"], "face", [55])

    assert [(h["annotation_id"], h["assessment_id"]) for h in hits] == [
        (city["twice"], city["dated"]),
        (city["twice"], city["undated"]),
    ]


def test_an_assessment_filter_applies_on_both_routes(
    pkg: USAPPackage, city: dict[str, int], monkeypatch
) -> None:
    part = city["part"]
    selection = SELECTIONS["across blocks"] + [56]
    expected = _oracle(pkg, part, selection, assessment_id=city["dated"])

    assert expected == [(city["twice"], city["dated"], [55, 56])]

    for fraction in (2.0, 0.0):
        monkeypatch.setattr(core, "_DENSE_SELECTION_FRACTION", fraction)
        hits = pkg.annotations_for_elements(
            part, "face", selection, assessment=city["dated"]
        )

        assert _summary(hits) == expected


def test_the_threshold_picks_the_route_and_not_the_answer(
    pkg: USAPPackage, city: dict[str, int], monkeypatch
) -> None:
    # Density is the selection's share of the blocks it touches, at the
    # package's own block size. Just below the threshold the prefilter runs;
    # at it, the single block_start pass does. The answer is the same.
    part = city["part"]
    fraction = core._DENSE_SELECTION_FRACTION
    at_threshold = math.ceil(fraction * BLOCK)

    assert (at_threshold - 1) / BLOCK < fraction <= at_threshold / BLOCK

    for size, takes_two_steps in ((at_threshold - 1, True), (at_threshold, False)):
        selection = list(range(size))
        calls = _route_spy(monkeypatch)

        hits = pkg.annotations_for_elements(part, "face", selection)

        assert bool(calls) is takes_two_steps, size
        assert _summary(hits) == _oracle(pkg, part, selection)

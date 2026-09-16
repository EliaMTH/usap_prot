"""
Ordered element paths — the claim membership cannot express.

Covers the contract from docs/ORDERED_PATHS_DESIGN.md: a path is a sequence,
its membership is the index derived from it, and the two are written together
so they cannot drift.

Every test here is written against an input that sorting would change. That is
deliberate: membership goes through as_index_array, which sorts and
de-duplicates, and a round-trip assertion on an already-ascending path would
pass even if the path write had been routed through it — which is the one
mistake that silently removes the whole feature.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import assert_package_valid, make_pkg
from usap import ELEMENT_KIND_FACE, USAPError, USAPPackage


def _road_package(tmp_path: Path, *, parts: int = 1, element_count: int = 1000):
    """A package with one mesh asset, `parts` face parts, and one annotation."""
    pkg = make_pkg(tmp_path, name="road.usap.gpkg")

    asset_id = pkg.register_asset(uri="road.ply", asset_kind="mesh")
    part_ids = [
        pkg.register_asset_part(
            asset_id=asset_id,
            part_path=f"geometry/{index}",
            element_kind=ELEMENT_KIND_FACE,
            element_count=element_count,
            indexing_profile="usap:test-face-order-v1",
        )
        for index in range(parts)
    ]
    semantic_class_id = pkg.create_semantic_class(
        scheme="local",
        class_uri="local:RoadCentreline",
        local_name="RoadCentreline",
    )
    annotation_id = pkg.create_annotation(
        annotation_uid="ann-road",
        semantic_class_id=semantic_class_id,
    )

    return pkg, asset_id, part_ids, annotation_id


def _indices(runs) -> list[list[list[int]]]:
    return [[s["element_indices"] for s in run["segments"]] for run in runs]


def test_a_path_round_trips_in_order_with_repeats(tmp_path: Path) -> None:
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [40, 12, 12, 7]}],
        )

        runs = pkg.path_for_annotation(annotation_id)

        assert _indices(runs) == [[[40, 12, 12, 7]]]
        assert_package_valid(pkg)


def test_membership_is_derived_as_the_paths_sorted_unique_set(
    tmp_path: Path,
) -> None:
    # The derivation, stated as a test: the path is the source, the membership
    # is its index. Existing readers must see the set they always saw.
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [40, 12, 12, 7]}],
        )

        blocks = pkg.elements_for_annotation(annotation_id, expand=True)
        elements = [e for block in blocks for e in block["elements"]]

        assert elements == [7, 12, 40]
        assert_package_valid(pkg)


def test_a_run_may_cross_asset_parts(tmp_path: Path) -> None:
    # The case the interim usap:path key could not express at all, and the one
    # the whole table exists for: element indices restart at zero in every
    # part, so a route crossing a tile boundary needs the part per segment.
    pkg, _asset, (tile_a, tile_b), annotation_id = _road_package(
        tmp_path, parts=2
    )

    with pkg:
        result = pkg.set_annotation_path(
            annotation_id,
            [
                {"asset_part_id": tile_a, "element_indices": [998, 999]},
                {
                    "asset_part_id": tile_b,
                    "element_indices": [0, 1, 2],
                    "continues_previous": True,
                },
                {"asset_part_id": tile_b, "element_indices": [500, 501]},
            ],
        )

        runs = pkg.path_for_annotation(annotation_id)

        # Two runs, not three segments and not one run: the crossing is
        # continuous, the gap after it is not.
        assert result["path_run_count"] == 2
        assert _indices(runs) == [[[998, 999], [0, 1, 2]], [[500, 501]]]
        assert [s["asset_part_id"] for s in runs[0]["segments"]] == [
            tile_a,
            tile_b,
        ]
        assert_package_valid(pkg)


def test_each_parts_membership_is_derived_separately(tmp_path: Path) -> None:
    pkg, _asset, (tile_a, tile_b), annotation_id = _road_package(
        tmp_path, parts=2
    )

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [
                {"asset_part_id": tile_a, "element_indices": [999, 998]},
                {
                    "asset_part_id": tile_b,
                    "element_indices": [2, 1, 0],
                    "continues_previous": True,
                },
            ],
        )

        def elements(part: int) -> list[int]:
            return [
                e
                for block in pkg.elements_for_annotation(
                    annotation_id, expand=True, asset_part_id=part
                )
                for e in block["elements"]
            ]

        assert elements(tile_a) == [998, 999]
        assert elements(tile_b) == [0, 1, 2]
        assert_package_valid(pkg)


def test_a_membership_write_over_a_path_is_refused(tmp_path: Path) -> None:
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [9, 8, 7]}],
        )

        with pytest.raises(USAPError, match="carries an ordered path"):
            pkg.attach_annotation_elements(
                annotation_id=annotation_id,
                asset_part_id=part,
                element_kind="face",
                element_indices=[1, 2, 3],
            )

        # And the path is untouched by the refusal.
        assert _indices(pkg.path_for_annotation(annotation_id)) == [[[9, 8, 7]]]


def test_drop_path_replaces_the_membership_and_discards_the_order(
    tmp_path: Path,
) -> None:
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [9, 8, 7]}],
        )

        pkg.attach_annotation_elements(
            annotation_id=annotation_id,
            asset_part_id=part,
            element_kind="face",
            element_indices=[1, 2, 3],
            drop_path=True,
        )

        assert pkg.path_for_annotation(annotation_id) == []

        blocks = pkg.elements_for_annotation(annotation_id, expand=True)

        assert [e for b in blocks for e in b["elements"]] == [1, 2, 3]
        assert_package_valid(pkg)


def test_setting_a_path_to_none_keeps_the_membership(tmp_path: Path) -> None:
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [9, 8, 7]}],
        )
        pkg.set_annotation_path(annotation_id, None)

        assert pkg.path_for_annotation(annotation_id) == []

        blocks = pkg.elements_for_annotation(annotation_id, expand=True)

        assert [e for b in blocks for e in b["elements"]] == [7, 8, 9]
        assert_package_valid(pkg)


def test_a_reroute_onto_fewer_parts_leaves_no_stale_membership(
    tmp_path: Path,
) -> None:
    # Otherwise the dropped part keeps a membership nothing orders any more --
    # the drift this design exists to prevent, arriving through the front door.
    pkg, _asset, (tile_a, tile_b), annotation_id = _road_package(
        tmp_path, parts=2
    )

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [
                {"asset_part_id": tile_a, "element_indices": [10, 11]},
                {"asset_part_id": tile_b, "element_indices": [20, 21]},
            ],
        )
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": tile_a, "element_indices": [10, 11, 12]}],
        )

        remaining = {
            block["asset_part_id"]
            for block in pkg.elements_for_annotation(annotation_id)
        }

        assert remaining == {tile_a}
        assert_package_valid(pkg)


def test_a_path_may_not_span_two_assets(tmp_path: Path) -> None:
    # One ordinal sequence lives in one assessment, and an assessment evaluates
    # one asset.
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        other = pkg.register_asset(uri="other.ply", asset_kind="mesh")
        other_part = pkg.register_asset_part(
            asset_id=other,
            part_path="geometry/0",
            element_kind=ELEMENT_KIND_FACE,
            element_count=100,
        )

        with pytest.raises(USAPError, match="second assessment"):
            pkg.set_annotation_path(
                annotation_id,
                [
                    {"asset_part_id": part, "element_indices": [1, 2]},
                    {
                        "asset_part_id": other_part,
                        "element_indices": [3, 4],
                        "continues_previous": True,
                    },
                ],
            )

        # Rolled back whole: no path, and no half-written membership.
        assert pkg.path_for_annotation(annotation_id) == []
        assert pkg.elements_for_annotation(annotation_id) == []


def test_two_assessments_each_carry_their_own_path(tmp_path: Path) -> None:
    pkg, asset_id, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        first = pkg.create_assessment(
            annotation_id, asset_id, assessed_at="2026-03-01"
        )
        second = pkg.create_assessment(
            annotation_id, asset_id, assessed_at="2027-03-01"
        )

        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [3, 2, 1]}],
            assessment=first["assessment_id"],
        )
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [9, 8]}],
            assessment=second["assessment_id"],
        )

        assert _indices(
            pkg.path_for_annotation(
                annotation_id, assessment=first["assessment_id"]
            )
        ) == [[[3, 2, 1]]]
        assert _indices(
            pkg.path_for_annotation(
                annotation_id, assessment=second["assessment_id"]
            )
        ) == [[[9, 8]]]

        # Unqualified, both evaluations' runs come back, never merged.
        assert len(pkg.path_for_annotation(annotation_id)) == 2
        assert_package_valid(pkg)


def test_path_run_count_is_reported_without_being_asked_for(
    tmp_path: Path,
) -> None:
    # Discovery is unconditional: an application learns an annotation is
    # ordered from the call it already makes, with no argument and no second
    # query. A flag would require knowing the answer to ask the question.
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [
                {"asset_part_id": part, "element_indices": [9, 8]},
                {"asset_part_id": part, "element_indices": [3, 2]},
            ],
        )

        annotation = pkg.get_annotation(
            annotation_id, include_membership_summary=True
        )

        assert annotation["assessment_summary"][0]["path_run_count"] == 2
        assert annotation["path_summary"][0]["segment_count"] == 2

        assessments = pkg.list_assessments(annotation_id=annotation_id)

        assert assessments[0]["path_run_count"] == 2

        row = pkg.conn.execute(
            "SELECT path_run_count FROM usap_annotations_view WHERE OGC_FID = ?",
            (annotation_id,),
        ).fetchone()

        # The plain-SQL discovery path, which is what the C++ reader uses.
        assert int(row["path_run_count"]) == 2


def test_an_unordered_annotation_reports_no_runs(tmp_path: Path) -> None:
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.attach_annotation_elements(
            annotation_id=annotation_id,
            asset_part_id=part,
            element_kind="face",
            element_indices=[1, 2, 3],
        )

        annotation = pkg.get_annotation(
            annotation_id, include_membership_summary=True
        )

        assert annotation["assessment_summary"][0]["path_run_count"] == 0
        assert pkg.path_for_annotation(annotation_id) == []


def test_existing_membership_reads_are_unchanged_by_a_path(
    tmp_path: Path,
) -> None:
    # The compatibility promise: no shipped read call changes its output shape
    # or its ordering, so the consumer's reader keeps working across the bump.
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [40, 12, 7]}],
        )

        hits = pkg.annotations_for_elements(part, ELEMENT_KIND_FACE, [12])

        assert len(hits) == 1
        assert hits[0]["annotation_uid"] == "ann-road"
        assert hits[0]["matched_elements"] == [12]


def test_a_path_index_past_the_asset_part_is_refused(tmp_path: Path) -> None:
    pkg, _asset, (part,), annotation_id = _road_package(
        tmp_path, element_count=100
    )

    with pkg:
        with pytest.raises(USAPError, match="out of range"):
            pkg.set_annotation_path(
                annotation_id,
                [{"asset_part_id": part, "element_indices": [10, 5000]}],
            )


def test_malformed_segment_lists_are_refused(tmp_path: Path) -> None:
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        with pytest.raises(USAPError, match="non-empty list"):
            pkg.set_annotation_path(annotation_id, [])

        with pytest.raises(USAPError, match="no 'asset_part_id'"):
            pkg.set_annotation_path(annotation_id, [{"element_indices": [1]}])

        with pytest.raises(USAPError, match="no element indices"):
            pkg.set_annotation_path(
                annotation_id,
                [{"asset_part_id": part, "element_indices": []}],
            )

        with pytest.raises(USAPError, match="cannot continue a previous one"):
            pkg.set_annotation_path(
                annotation_id,
                [
                    {
                        "asset_part_id": part,
                        "element_indices": [1],
                        "continues_previous": True,
                    }
                ],
            )

        with pytest.raises(USAPError, match="Unrecognised key"):
            pkg.set_annotation_path(
                annotation_id,
                [
                    {
                        "asset_part_id": part,
                        "element_indices": [1],
                        "continues": True,
                    }
                ],
            )


def test_usap_path_in_attributes_is_refused_on_every_write(
    tmp_path: Path,
) -> None:
    pkg, _asset, (_part,), annotation_id = _road_package(tmp_path)

    with pkg:
        with pytest.raises(USAPError, match="must not carry 'usap:path'"):
            pkg.update_annotation(
                annotation_id, attributes={"usap:path": [[1, 2, 3]]}
            )

        # The replacing form too, not only the merging one.
        with pytest.raises(USAPError, match="must not carry 'usap:path'"):
            pkg.update_annotation(
                annotation_id, attributes_json='{"usap:path": [[1, 2]]}'
            )

        # And at creation.
        semantic_class_id = pkg.resolve_semantic_class("RoadCentreline")

        with pytest.raises(USAPError, match="must not carry 'usap:path'"):
            pkg.create_annotation(
                annotation_uid="ann-other",
                semantic_class_id=semantic_class_id,
                attributes_json='{"usap:path": [[1]]}',
            )


def test_deleting_the_assessment_removes_its_path(tmp_path: Path) -> None:
    pkg, asset_id, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [3, 2, 1]}],
        )

        assessment_id = pkg.list_assessments(annotation_id=annotation_id)[0][
            "assessment_id"
        ]
        pkg.delete_assessment(assessment_id)

        assert pkg.path_for_annotation(annotation_id) == []
        assert (
            pkg.conn.execute(
                "SELECT COUNT(*) AS n FROM usap_path_block"
            ).fetchone()["n"]
            == 0
        )


def test_deleting_the_annotation_removes_its_path(tmp_path: Path) -> None:
    pkg, _asset, (part,), annotation_id = _road_package(tmp_path)

    with pkg:
        pkg.set_annotation_path(
            annotation_id,
            [{"asset_part_id": part, "element_indices": [3, 2, 1]}],
        )
        pkg.delete_annotation(annotation_id)

        assert (
            pkg.conn.execute(
                "SELECT COUNT(*) AS n FROM usap_path_block"
            ).fetchone()["n"]
            == 0
        )

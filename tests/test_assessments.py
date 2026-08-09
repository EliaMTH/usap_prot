"""
Assessments: US-ANN-08, criterion by criterion.

The user story defines a two-level model — a logical annotation (concept +
CityObject) carrying N assessments, each with its own id, a date, one specific
3D asset, and its own membership. These tests are written against that story's
acceptance criteria rather than against the implementation, so they stay
meaningful if the internals move.

The other half of what is checked here is the compatibility claim the design
rests on: a caller that never mentions assessments must behave exactly as it did
before they existed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from usap import ELEMENT_KIND_FACE, USAPAmbiguityError, USAPError, USAPPackage

from conftest import (
    assert_package_valid,
    make_mesh_part,
    make_pkg,
    seed_citygml_concepts,
)


def _annotated(pkg: USAPPackage, part: int, indices: list[int]) -> dict:
    """One annotation with membership, made the way an app makes the first one."""
    seed_citygml_concepts(pkg)

    return pkg.annotate_elements(
        concept="RoofSurface",
        asset_part_id=part,
        element_kind="face",
        element_indices=indices,
    )


def _asset_of(pkg: USAPPackage, asset_part_id: int) -> int:
    return next(
        part["asset_id"]
        for part in pkg.list_asset_parts()
        if part["asset_part_id"] == asset_part_id
    )


# ---------------------------------------------------------------------------
# Compatibility: assessments must be invisible until they are wanted
# ---------------------------------------------------------------------------


def test_annotating_without_mentioning_assessments_still_works(pkg, mesh_part):
    """
    The pre-0.4.0 call shape keeps working, and quietly acquires one assessment.
    """
    annotation = _annotated(pkg, mesh_part, [1, 2, 3])

    summary = annotation["assessment_summary"]

    assert len(summary) == 1
    assert summary[0]["assessed_at"] is None
    assert summary[0]["selected_count"] == 3

    assert_package_valid(pkg)


def test_repeated_writes_reuse_the_one_assessment(pkg, mesh_part):
    """
    Editing membership must not accumulate assessments: an app that lets a user
    adjust a lasso ten times has made one evaluation, not ten.
    """
    annotation = _annotated(pkg, mesh_part, [1, 2])
    annotation_id = annotation["annotation_id"]

    for indices in ([1, 2, 3], [4], [5, 6]):
        pkg.attach_annotation_elements(
            annotation_id=annotation_id,
            asset_part_id=mesh_part,
            element_kind="face",
            element_indices=indices,
        )

    assert len(pkg.list_assessments(annotation_id=annotation_id)) == 1

    blocks = pkg.elements_for_annotation(annotation_id, expand=True)
    assert [e for block in blocks for e in block["elements"]] == [5, 6]


# ---------------------------------------------------------------------------
# US-ANN-08: creating multiple assessments for the same annotation
# ---------------------------------------------------------------------------


def test_second_assessment_keeps_the_same_annotation_and_city_object(pkg, mesh_part):
    """
    "the new assessment remains linked to the same logical annotation and to
    the same CityObject" — the whole point of the level existing.
    """
    seed_citygml_concepts(pkg)
    pkg.create_city_object(object_uid="building_1_roof_1")

    annotation = pkg.annotate_elements(
        concept="RoofSurface",
        city_object_uid="building_1_roof_1",
        asset_part_id=mesh_part,
        element_kind="face",
        element_indices=[1, 2],
        assessed_at="2026-01-01T00:00:00Z",
    )
    annotation_id = annotation["annotation_id"]

    second = pkg.create_assessment(
        annotation_id,
        _asset_of(pkg, mesh_part),
        assessed_at="2027-01-01T00:00:00Z",
    )

    assert second["annotation_id"] == annotation_id
    assert second["annotation_uid"] == annotation["annotation_uid"]

    # One annotation, one concept, one city object — still.
    assert len(pkg.list_annotations()) == 1
    reread = pkg.get_annotation(annotation_id)
    assert reread["semantic_class"] == "RoofSurface"
    assert reread["primary_city_object_uid"] == "building_1_roof_1"


def test_each_assessment_has_a_unique_identifier(pkg, mesh_part):
    annotation_id = _annotated(pkg, mesh_part, [1])["annotation_id"]
    asset_id = _asset_of(pkg, mesh_part)

    uids = {
        pkg.create_assessment(annotation_id, asset_id, assessed_at=date)[
            "assessment_uid"
        ]
        for date in ("2026-01-01", "2027-01-01", "2028-01-01")
    }

    assert len(uids) == 3


def test_new_assessment_does_not_modify_the_previous_one(pkg, mesh_part):
    """
    "the creation of the new assessment does not modify or overwrite the
    previous assessments" — the failure the one-annotation-per-assessment
    workaround could not prevent.
    """
    annotation = _annotated(pkg, mesh_part, [1, 2])
    annotation_id = annotation["annotation_id"]
    first_id = annotation["assessment_summary"][0]["assessment_id"]

    second = pkg.create_assessment(
        annotation_id,
        _asset_of(pkg, mesh_part),
        assessed_at="2027-01-01T00:00:00Z",
    )

    pkg.attach_annotation_elements(
        annotation_id=annotation_id,
        asset_part_id=mesh_part,
        element_kind="face",
        element_indices=[7, 8, 9],
        assessment=second["assessment_id"],
    )

    first_blocks = pkg.elements_for_annotation(
        annotation_id, expand=True, assessment=first_id
    )

    assert [e for b in first_blocks for e in b["elements"]] == [1, 2]

    second_blocks = pkg.elements_for_annotation(
        annotation_id, expand=True, assessment=second["assessment_id"]
    )

    assert [e for b in second_blocks for e in b["elements"]] == [7, 8, 9]

    assert_package_valid(pkg)


def test_assessments_are_listed_distinguished_by_date_and_asset(pkg, tmp_path):
    """
    "view the list of all assessments ... distinguishing them at least by date
    and 3D assets".
    """
    seed_citygml_concepts(pkg)

    mesh_a = make_mesh_part(pkg, element_count=50)
    asset_b = pkg.register_asset(uri="other.ply", asset_kind="mesh")
    mesh_b = pkg.register_asset_part(
        asset_id=asset_b,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=50,
    )

    annotation = pkg.annotate_elements(
        concept="RoofSurface",
        asset_part_id=mesh_a,
        element_kind="face",
        element_indices=[1],
        assessed_at="2026-01-01",
    )
    annotation_id = annotation["annotation_id"]

    pkg.create_assessment(annotation_id, _asset_of(pkg, mesh_a), assessed_at="2027-01-01")
    pkg.create_assessment(annotation_id, asset_b, assessed_at="2026-01-01")

    listed = pkg.list_assessments(annotation_id=annotation_id)

    assert [(a["assessed_at"], a["asset_uri"]) for a in listed] == [
        ("2026-01-01", "mesh.ply"),
        ("2026-01-01", "other.ply"),
        ("2027-01-01", "mesh.ply"),
    ]


def test_selecting_an_assessment_highlights_only_its_elements(pkg, mesh_part):
    """
    "by selecting an assessment, the system highlights only the geometric
    elements associated with that assessment".
    """
    annotation = _annotated(pkg, mesh_part, [1, 2])
    annotation_id = annotation["annotation_id"]

    second = pkg.create_assessment(
        annotation_id, _asset_of(pkg, mesh_part), assessed_at="2027-01-01"
    )
    pkg.attach_annotation_elements(
        annotation_id=annotation_id,
        asset_part_id=mesh_part,
        element_kind="face",
        element_indices=[3, 4],
        assessment=second["assessment_id"],
    )

    hits = pkg.annotations_for_elements(
        asset_part_id=mesh_part,
        element_kind="face",
        selected_indices=[1, 2, 3, 4],
        assessment=second["assessment_id"],
    )

    assert len(hits) == 1
    assert hits[0]["matched_elements"] == [3, 4]
    assert hits[0]["assessed_at"] == "2027-01-01"


def test_lasso_reports_each_assessment_separately(pkg, mesh_part):
    """
    A selection touching two evaluations answers twice, tagged — never merged
    into one extent that no single evaluation ever claimed.
    """
    annotation = _annotated(pkg, mesh_part, [1, 2])
    annotation_id = annotation["annotation_id"]

    second = pkg.create_assessment(
        annotation_id, _asset_of(pkg, mesh_part), assessed_at="2027-01-01"
    )
    pkg.attach_annotation_elements(
        annotation_id=annotation_id,
        asset_part_id=mesh_part,
        element_kind="face",
        element_indices=[3, 4],
        assessment=second["assessment_id"],
    )

    hits = pkg.annotations_for_elements(
        asset_part_id=mesh_part,
        element_kind="face",
        selected_indices=[2, 3],
    )

    assert len(hits) == 2
    assert {h["annotation_id"] for h in hits} == {annotation_id}
    assert {
        h["assessed_at"]: tuple(h["matched_elements"]) for h in hits
    } == {None: (2,), "2027-01-01": (3,)}


def test_deleting_one_assessment_leaves_the_others(pkg, mesh_part):
    annotation = _annotated(pkg, mesh_part, [1, 2])
    annotation_id = annotation["annotation_id"]
    first_id = annotation["assessment_summary"][0]["assessment_id"]

    second = pkg.create_assessment(
        annotation_id, _asset_of(pkg, mesh_part), assessed_at="2027-01-01"
    )
    pkg.attach_annotation_elements(
        annotation_id=annotation_id,
        asset_part_id=mesh_part,
        element_kind="face",
        element_indices=[3, 4],
        assessment=second["assessment_id"],
    )

    pkg.delete_assessment(second["assessment_id"])

    remaining = pkg.list_assessments(annotation_id=annotation_id)
    assert [a["assessment_id"] for a in remaining] == [first_id]

    # The annotation itself survives, with the surviving assessment's geometry.
    assert pkg.get_annotation(annotation_id) is not None
    blocks = pkg.elements_for_annotation(annotation_id, expand=True)
    assert [e for b in blocks for e in b["elements"]] == [1, 2]

    assert_package_valid(pkg)


def test_deleting_the_annotation_removes_every_assessment(pkg, mesh_part):
    annotation_id = _annotated(pkg, mesh_part, [1])["annotation_id"]
    pkg.create_assessment(
        annotation_id, _asset_of(pkg, mesh_part), assessed_at="2027-01-01"
    )

    pkg.delete_annotation(annotation_id)

    assert pkg.list_assessments() == []
    assert pkg.conn.execute(
        "SELECT COUNT(*) AS n FROM usap_membership_block"
    ).fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Integrity: an assessment's membership must stay inside its own asset
# ---------------------------------------------------------------------------


def test_membership_outside_the_assessments_asset_is_refused(pkg):
    """
    "if the 3D asset to which the assessment refers is not loaded or does not
    correspond to the expected version, the system ... does not apply the
    membership to another 3D asset."
    """
    seed_citygml_concepts(pkg)

    mesh_a = make_mesh_part(pkg, element_count=50)
    asset_b = pkg.register_asset(uri="other.ply", asset_kind="mesh")
    mesh_b = pkg.register_asset_part(
        asset_id=asset_b,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=50,
    )

    annotation_id = pkg.annotate_elements(
        concept="RoofSurface",
        asset_part_id=mesh_a,
        element_kind="face",
        element_indices=[1],
    )["annotation_id"]

    assessment_on_a = pkg.list_assessments(annotation_id=annotation_id)[0]

    with pytest.raises(USAPError, match="must stay within its own asset"):
        pkg.attach_annotation_elements(
            annotation_id=annotation_id,
            asset_part_id=mesh_b,
            element_kind="face",
            element_indices=[1],
            assessment=assessment_on_a["assessment_id"],
        )


def test_assessment_of_another_annotation_is_refused(pkg, mesh_part):
    seed_citygml_concepts(pkg)

    first = pkg.annotate_elements(
        concept="RoofSurface", asset_part_id=mesh_part,
        element_kind="face", element_indices=[1],
    )
    second = pkg.annotate_elements(
        concept="WallSurface", asset_part_id=mesh_part,
        element_kind="face", element_indices=[2],
    )

    foreign = pkg.list_assessments(
        annotation_id=first["annotation_id"]
    )[0]["assessment_id"]

    with pytest.raises(USAPError, match="belongs to annotation"):
        pkg.attach_annotation_elements(
            annotation_id=second["annotation_id"],
            asset_part_id=mesh_part,
            element_kind="face",
            element_indices=[3],
            assessment=foreign,
        )


def test_ambiguous_write_raises_rather_than_guessing(pkg, mesh_part):
    """
    Once a second evaluation exists on an asset, an unqualified write has no
    right answer — picking the newest would silently rewrite history.
    """
    annotation = _annotated(pkg, mesh_part, [1])
    annotation_id = annotation["annotation_id"]

    pkg.create_assessment(
        annotation_id, _asset_of(pkg, mesh_part), assessed_at="2027-01-01"
    )

    with pytest.raises(USAPAmbiguityError, match="Pass assessment"):
        pkg.attach_annotation_elements(
            annotation_id=annotation_id,
            asset_part_id=mesh_part,
            element_kind="face",
            element_indices=[9],
        )


def test_only_one_undated_assessment_per_annotation_and_asset(pkg, mesh_part):
    """
    The partial unique index, not just the write path's good manners: SQLite
    treats NULLs as distinct, so the plain UNIQUE would not have caught this.
    """
    annotation_id = _annotated(pkg, mesh_part, [1])["annotation_id"]
    asset_id = _asset_of(pkg, mesh_part)

    # Idempotent through the API...
    again = pkg.create_assessment(annotation_id, asset_id)
    assert len(pkg.list_assessments(annotation_id=annotation_id)) == 1

    # ...and refused underneath it.
    with pytest.raises(Exception):
        pkg.conn.execute(
            """
            INSERT INTO usap_assessment (assessment_uid, annotation_id, asset_id)
            VALUES ('asm_dupe', ?, ?)
            """,
            (annotation_id, asset_id),
        )


def test_create_assessment_is_idempotent_on_date(pkg, mesh_part):
    annotation_id = _annotated(pkg, mesh_part, [1])["annotation_id"]
    asset_id = _asset_of(pkg, mesh_part)

    first = pkg.create_assessment(annotation_id, asset_id, assessed_at="2027-01-01")
    second = pkg.create_assessment(annotation_id, asset_id, assessed_at="2027-01-01")

    assert first["assessment_id"] == second["assessment_id"]


# ---------------------------------------------------------------------------
# Metadata and validation
# ---------------------------------------------------------------------------


def test_assessment_carries_its_own_metadata(pkg, mesh_part):
    """
    "the user can consult each assessment and its attributes separately" —
    method/source/operator differ per evaluation, so they cannot live on the
    annotation.
    """
    annotation_id = _annotated(pkg, mesh_part, [1])["annotation_id"]

    created = pkg.create_assessment(
        annotation_id,
        _asset_of(pkg, mesh_part),
        assessed_at="2027-01-01",
        status="draft",
        confidence=0.4,
        attributes={"method": "detector_v3", "operator": "survey team B"},
    )

    reread = pkg.get_assessment(created["assessment_id"])

    assert reread["status"] == "draft"
    assert reread["confidence"] == 0.4
    assert json.loads(reread["attributes_json"])["method"] == "detector_v3"

    updated = pkg.update_assessment(
        created["assessment_id"], status="accepted", confidence=0.9
    )

    assert updated["status"] == "accepted"
    assert updated["confidence"] == 0.9
    # An update must not disturb the fields it was not given.
    assert json.loads(updated["attributes_json"])["operator"] == "survey team B"


def test_assessment_asset_cannot_be_repointed(pkg, mesh_part):
    """
    Membership is indexed against the assessment's asset, so moving the asset
    would silently make every stored index mean different geometry.
    """
    annotation_id = _annotated(pkg, mesh_part, [1])["annotation_id"]
    assessment = pkg.list_assessments(annotation_id=annotation_id)[0]

    with pytest.raises(TypeError):
        pkg.update_assessment(assessment["assessment_id"], asset_id=99)


def test_validation_catches_a_block_pointing_at_a_foreign_asset(pkg):
    """
    The integrity rule the asset-level binding buys, checked against a package
    corrupted underneath the API.
    """
    seed_citygml_concepts(pkg)

    mesh_a = make_mesh_part(pkg, element_count=50)
    asset_b = pkg.register_asset(uri="other.ply", asset_kind="mesh")
    mesh_b = pkg.register_asset_part(
        asset_id=asset_b,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=50,
    )

    annotation_id = pkg.annotate_elements(
        concept="RoofSurface", asset_part_id=mesh_a,
        element_kind="face", element_indices=[1],
    )["annotation_id"]

    pkg.conn.execute(
        "UPDATE usap_membership_block SET asset_part_id = ? WHERE annotation_id = ?",
        (mesh_b, annotation_id),
    )

    report = pkg.validate_report()

    assert "MEMBERSHIP_OUTSIDE_ASSESSMENT_ASSET" in {
        issue.code for issue in report.errors
    }


def test_validation_catches_a_block_naming_the_wrong_annotation(pkg, mesh_part):
    seed_citygml_concepts(pkg)

    first = pkg.annotate_elements(
        concept="RoofSurface", asset_part_id=mesh_part,
        element_kind="face", element_indices=[1],
    )
    second = pkg.annotate_elements(
        concept="WallSurface", asset_part_id=mesh_part,
        element_kind="face", element_indices=[2],
    )

    pkg.conn.execute(
        "UPDATE usap_membership_block SET annotation_id = ? WHERE annotation_id = ?",
        (second["annotation_id"], first["annotation_id"]),
    )

    report = pkg.validate_report()

    assert "ASSESSMENT_ANNOTATION_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_empty_assessment_is_a_warning_not_an_error(pkg, mesh_part):
    annotation_id = _annotated(pkg, mesh_part, [1])["annotation_id"]
    pkg.create_assessment(
        annotation_id, _asset_of(pkg, mesh_part), assessed_at="2027-01-01"
    )

    report = pkg.validate_report()

    assert report.is_ok
    assert "ASSESSMENT_WITHOUT_MEMBERSHIP" in {
        issue.code for issue in report.warnings
    }


# ---------------------------------------------------------------------------
# Value fields follow membership
# ---------------------------------------------------------------------------


def test_value_fields_are_scoped_to_their_assessment(pkg, mesh_part):
    """
    A field measured at two dates is two fields over the same part. Without
    assessment scoping the second reads as the first tiled twice.
    """
    seed_citygml_concepts(pkg)

    annotation = pkg.annotate_value_field(
        concept="RoofSurface",
        asset_part_id=mesh_part,
        element_kind="face",
        values=[0.5] * 100,
    )
    annotation_id = annotation["annotation_id"]

    later = pkg.create_assessment(
        annotation_id, _asset_of(pkg, mesh_part), assessed_at="2027-01-01"
    )
    pkg.replace_value_field(
        annotation_id=annotation_id,
        asset_part_id=mesh_part,
        element_kind="face",
        values=[0.9] * 100,
        assessment=later["assessment_id"],
    )

    # approx: the default value dtype is f4, so 0.9 does not round-trip exactly.
    assert pkg.values_for_annotation(
        annotation_id, assessment=later["assessment_id"]
    ).tolist() == pytest.approx([0.9] * 100)

    # Unqualified, the answer is genuinely ambiguous and must say so.
    with pytest.raises(USAPError, match="several assessments"):
        pkg.values_for_annotation(annotation_id)

    assert_package_valid(pkg)

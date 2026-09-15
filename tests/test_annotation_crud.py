from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import assert_package_valid, make_mesh_part, make_pkg, seed_citygml_concepts
from conftest import write_tiny_mesh as _write_tiny_mesh
from usap import (
    ELEMENT_KIND_FACE,
    ELEMENT_KIND_POINT,
    USAPError,
    USAPPackage,
    register_mesh_asset,
    seed_default_ade_vocabulary,
)


def test_get_and_update_annotation(tmp_path: Path) -> None:
    db_path = tmp_path / "crud.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        classes = seed_citygml_concepts(pkg)

        building_id = pkg.create_city_object(
            object_uid="building_1",
            semantic_class_id=classes.by_name["Building"],
            gml_id="building_1",
        )

        roof_id = pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=classes.by_name["RoofSurface"],
            gml_id="building_1_roof_1",
        )

        pkg.link_city_objects(
            building_id,
            roof_id,
            "boundedBy",
            category="containment",
            role="roof",
            graph_name="usap_default",
        )

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_crud_roof",
            semantic_class_id=classes.by_name["RoofSurface"],
            primary_city_object_id=roof_id,
            status="draft",
            confidence=0.25,
            attributes_json=json.dumps({"version": 1}),
        )

        annotation = pkg.get_annotation(annotation_id)

        assert annotation is not None
        assert annotation["annotation_uid"] == "ann_crud_roof"
        assert annotation["semantic_class"] == "RoofSurface"
        assert annotation["primary_city_object_uid"] == "building_1_roof_1"
        assert annotation["status"] == "draft"
        assert annotation["confidence"] == 0.25

        updated = pkg.update_annotation(
            annotation_id,
            status="accepted",
            confidence=None,
            attributes_json=json.dumps({"version": 2}),
        )

        assert updated["status"] == "accepted"
        assert updated["confidence"] is None
        assert json.loads(updated["attributes_json"]) == {"version": 2}

        # updated_at must track the last edit, not stay frozen at created_at.
        # Backdate the row so the assertion is deterministic despite the
        # one-second resolution of the timestamp, then confirm an update
        # advances updated_at while leaving created_at untouched. The backdated
        # value uses the stored format (UTC ISO-8601 with 'Z') so the string
        # comparison below stays lexicographic within one format.
        backdated = "2000-01-01T00:00:00Z"

        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_annotation
                SET created_at = ?, updated_at = ?
                WHERE annotation_id = ?
                """,
                (backdated, backdated, annotation_id),
            )

        pkg.update_annotation(annotation_id, status="accepted")

        timestamps = pkg.conn.execute(
            """
            SELECT created_at, updated_at
            FROM usap_annotation
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        assert timestamps["created_at"] == backdated
        assert timestamps["updated_at"] > backdated
        assert timestamps["updated_at"].endswith("Z")


def test_list_annotations_with_filters(tmp_path: Path) -> None:
    db_path = tmp_path / "list.usap.gpkg"

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        classes = seed_citygml_concepts(pkg)
        ade = seed_default_ade_vocabulary(pkg)

        roof_object_id = pkg.create_city_object(
            object_uid="roof_1",
            semantic_class_id=classes.by_name["RoofSurface"],
            gml_id="roof_1",
        )

        pkg.create_annotation(
            annotation_uid="ann_citygml_roof",
            semantic_class_id=classes.by_name["RoofSurface"],
            primary_city_object_id=roof_object_id,
            status="accepted",
        )

        pkg.create_annotation(
            annotation_uid="ann_energy_roof",
            semantic_class_id=ade.by_name["EnergyRoof"],
            primary_city_object_id=roof_object_id,
            status="draft",
        )

        accepted = pkg.list_annotations(status="accepted")
        drafts = pkg.list_annotations(status="draft")
        energy = pkg.list_annotations(semantic_class_local_name="EnergyRoof")
        by_city_object = pkg.list_annotations(city_object_uid="roof_1")

        assert [item["annotation_uid"] for item in accepted] == [
            "ann_citygml_roof"
        ]

        assert [item["annotation_uid"] for item in drafts] == [
            "ann_energy_roof"
        ]

        assert [item["annotation_uid"] for item in energy] == [
            "ann_energy_roof"
        ]

        assert {
            item["annotation_uid"]
            for item in by_city_object
        } == {
            "ann_citygml_roof",
            "ann_energy_roof",
        }


def test_list_annotations_asset_filter_separates_two_assets(tmp_path: Path) -> None:
    """
    US-DATA-01's load step: "the annotations belonging to the 3D asset just
    opened". With a single registered asset this filter is indistinguishable
    from a no-op — it returns everything either way — so a broken
    implementation looks exactly like a working one. It takes two assets to
    test at all, which is why this went uncovered.

    The pair mirrors the real case: a point cloud and a mesh of the same area,
    with one claim spanning both.
    """
    with make_pkg(tmp_path) as pkg:
        classes = seed_citygml_concepts(pkg)
        roof = classes.by_name["RoofSurface"]

        mesh_asset = pkg.register_asset(uri="area.ply", asset_kind="mesh")
        mesh_part = pkg.register_asset_part(
            asset_id=mesh_asset,
            part_path="geometry/0",
            element_kind=ELEMENT_KIND_FACE,
            element_count=100,
        )
        # A second part on the same asset: without it, asset_id and
        # asset_part_id are themselves indistinguishable.
        mesh_part_2 = pkg.register_asset_part(
            asset_id=mesh_asset,
            part_path="geometry/1",
            element_kind=ELEMENT_KIND_FACE,
            element_count=100,
        )

        cloud_asset = pkg.register_asset(uri="area.las", asset_kind="pointcloud")
        cloud_part = pkg.register_asset_part(
            asset_id=cloud_asset,
            part_path="points/0",
            element_kind=ELEMENT_KIND_POINT,
            element_count=100,
        )

        def annotate(uid: str, part: int, kind: int) -> int:
            return int(
                pkg.annotate_elements(
                    concept=roof,
                    annotation_uid=uid,
                    asset_part_id=part,
                    element_kind=kind,
                    element_indices=[1, 2, 3],
                )["annotation_id"]
            )

        annotate("ann_mesh_only", mesh_part, ELEMENT_KIND_FACE)
        annotate("ann_mesh_part_2", mesh_part_2, ELEMENT_KIND_FACE)
        annotate("ann_cloud_only", cloud_part, ELEMENT_KIND_POINT)

        # One claim covering both assets — it must appear under each, and is
        # the reason the filter cannot be a partition.
        both = annotate("ann_both_assets", mesh_part, ELEMENT_KIND_FACE)
        pkg.attach_annotation_elements(
            annotation_id=both,
            asset_part_id=cloud_part,
            element_kind=ELEMENT_KIND_POINT,
            element_indices=[10, 11],
        )

        # No membership anywhere: belongs to no asset, so no asset lists it.
        pkg.create_annotation(
            annotation_uid="ann_unattached",
            semantic_class_id=roof,
            status="draft",
        )

        def uids(**filters) -> set[str]:
            return {item["annotation_uid"] for item in pkg.list_annotations(**filters)}

        assert uids() == {
            "ann_mesh_only",
            "ann_mesh_part_2",
            "ann_cloud_only",
            "ann_both_assets",
            "ann_unattached",
        }

        assert uids(asset_id=mesh_asset) == {
            "ann_mesh_only",
            "ann_mesh_part_2",
            "ann_both_assets",
        }

        assert uids(asset_id=cloud_asset) == {
            "ann_cloud_only",
            "ann_both_assets",
        }

        # asset_part_id is narrower than asset_id: the second mesh part drops
        # out even though it is the same asset.
        assert uids(asset_part_id=mesh_part) == {"ann_mesh_only", "ann_both_assets"}
        assert uids(asset_part_id=mesh_part_2) == {"ann_mesh_part_2"}
        assert uids(asset_part_id=cloud_part) == {"ann_cloud_only", "ann_both_assets"}

        # Filters AND-combine, including across assets: a part of one asset
        # and the id of the other keeps only what spans both.
        assert uids(asset_id=cloud_asset, asset_part_id=mesh_part) == {
            "ann_both_assets"
        }


def test_reverse_query_reports_the_same_identifiers_as_the_detail_read(
    tmp_path: Path,
) -> None:
    # A lasso (annotations_for_elements) and a detail panel (get_annotation /
    # list_annotations) must name the same CityObject the same way. The reverse
    # query used to return object_uid only, so an app showing gml:id in the
    # detail panel had nothing to show beside the lasso hit.
    #
    # object_uid and gml_id are deliberately different here: with them equal a
    # column aliased to the wrong one would still pass.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        seed_citygml_concepts(pkg)

        roof_id = pkg.create_city_object(
            object_uid="carrier::roof_1",
            gml_id="GML_ROOF_1",
        )

        pkg.annotate_elements(
            concept="RoofSurface",
            annotation_uid="ann_reverse_identifiers",
            asset_part_id=part,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[3, 4],
            city_object_id=roof_id,
        )

        (match,) = pkg.annotations_for_elements(
            asset_part_id=part,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[4],
        )

        assert match["primary_city_object_uid"] == "carrier::roof_1"
        assert match["primary_city_object_gml_id"] == "GML_ROOF_1"

        detail = pkg.get_annotation(annotation_uid="ann_reverse_identifiers")
        (listed,) = pkg.list_annotations(city_object_uid="carrier::roof_1")

        assert detail is not None

        for field in ("primary_city_object_uid", "primary_city_object_gml_id"):
            assert match[field] == detail[field]
            assert match[field] == listed[field]

        # An annotation with no CityObject reports both as None rather than
        # omitting the key, so the caller can branch on one shape.
        pkg.annotate_elements(
            concept="RoofSurface",
            annotation_uid="ann_reverse_unlinked",
            asset_part_id=part,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[9],
        )

        (unlinked,) = pkg.annotations_for_elements(
            asset_part_id=part,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[9],
        )

        assert unlinked["primary_city_object_uid"] is None
        assert unlinked["primary_city_object_gml_id"] is None


def test_delete_annotation_cascades_membership(tmp_path: Path) -> None:
    mesh_path = tmp_path / "mesh.ply"
    db_path = tmp_path / "delete.usap.gpkg"

    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        classes = seed_citygml_concepts(pkg)

        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="tiny_mesh",
            representation_kind="triangulated_surface",
            lod=None,
        )

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_delete_me",
            semantic_class_id=classes.by_name["RoofSurface"],
            status="draft",
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=mesh.primary_asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[0, 1],
        )

        before = pkg.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM usap_membership_block
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        assert before["n"] > 0

        deleted = pkg.delete_annotation(annotation_id)

        assert deleted is True
        assert pkg.get_annotation(annotation_id) is None

        after = pkg.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM usap_membership_block
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        assert after["n"] == 0

        assert pkg.delete_annotation(annotation_id, missing_ok=True) is False

def test_create_annotation_rejects_conflicting_concept(tmp_path: Path) -> None:
    # Re-using an annotation_uid with a different concept must raise, not
    # silently replace the existing claim.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")
        pkg.create_semantic_class(scheme="s", class_uri="s:Wall", local_name="Wall")

        pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_x",
            asset_part_id=part,
            element_kind="face",
            element_indices=[1, 2],
        )

        with pytest.raises(USAPError, match="different semantic class"):
            pkg.annotate_elements(
                concept="Wall",
                annotation_uid="ann_x",
                asset_part_id=part,
                element_kind="face",
                element_indices=[50, 51],
            )

        # The rejected call must not have touched the annotation or its
        # membership (the old behavior silently replaced the indices).
        annotation = pkg.get_annotation(annotation_uid="ann_x")

        assert annotation is not None
        assert annotation["semantic_class"] == "Roof"

        blocks = pkg.elements_for_annotation(
            int(annotation["annotation_id"]),
            expand=True,
        )

        assert [block["elements"] for block in blocks] == [[1, 2]]


def test_integrity_violations_raise_usap_error(tmp_path: Path) -> None:
    # Constraint violations must surface as USAPError, not raw sqlite3 errors.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")

        annotation = pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_a",
            asset_part_id=part,
            element_kind="face",
            element_indices=[1],
        )

        with pytest.raises(USAPError, match="constraint"):
            pkg.update_annotation(
                int(annotation["annotation_id"]),
                semantic_class_id=None,
            )

        with pytest.raises(USAPError, match="Annotation not found"):
            pkg.attach_annotation_elements(
                annotation_id=99999,
                asset_part_id=part,
                element_kind="face",
                element_indices=[1],
            )


def _represents_links(pkg: USAPPackage, annotation_id: int) -> list[int]:
    """City objects the annotation carries a 'represents' link to."""
    rows = pkg.conn.execute(
        """
        SELECT city_object_id
        FROM usap_annotation_object
        WHERE annotation_id = ?
          AND relation_type = 'represents'
        ORDER BY city_object_id
        """,
        (annotation_id,),
    ).fetchall()

    return [int(row["city_object_id"]) for row in rows]


def _moveable_annotation(pkg: USAPPackage) -> tuple[int, int, int]:
    """An annotation on object_a, plus the ids of object_a and object_b."""
    part = make_mesh_part(pkg)
    pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")

    object_a = pkg.create_city_object(object_uid="object_a")
    object_b = pkg.create_city_object(object_uid="object_b")

    annotation = pkg.annotate_elements(
        concept="Roof",
        annotation_uid="ann_move",
        asset_part_id=part,
        element_kind="face",
        element_indices=[1, 2],
        city_object_id=object_a,
    )

    return int(annotation["annotation_id"]), object_a, object_b


def test_update_annotation_moves_primary_object_link(tmp_path: Path) -> None:
    # The primary city object is recorded twice: as a column on the annotation
    # and as a 'represents' link row. Moving it must move both, otherwise the
    # annotation stays visible under the object it no longer belongs to.
    with make_pkg(tmp_path) as pkg:
        annotation_id, object_a, object_b = _moveable_annotation(pkg)

        assert _represents_links(pkg, annotation_id) == [object_a]

        updated = pkg.update_annotation(
            annotation_id,
            primary_city_object_id=object_b,
        )

        assert updated["primary_city_object_id"] == object_b
        assert _represents_links(pkg, annotation_id) == [object_b]

        assert pkg.elements_for_city_object(
            "object_a",
            include_descendants=False,
        ) == []

        moved = pkg.elements_for_city_object(
            "object_b",
            include_descendants=False,
        )

        assert {block["annotation_id"] for block in moved} == {annotation_id}

        assert_package_valid(pkg)


def test_update_annotation_clearing_primary_object_removes_link(
    tmp_path: Path,
) -> None:
    # Detaching an annotation from its city object must not leave the link row
    # behind: the annotation would still answer queries for that object.
    with make_pkg(tmp_path) as pkg:
        annotation_id, _object_a, _object_b = _moveable_annotation(pkg)

        updated = pkg.update_annotation(
            annotation_id,
            primary_city_object_id=None,
        )

        assert updated["primary_city_object_id"] is None
        assert _represents_links(pkg, annotation_id) == []

        assert pkg.elements_for_city_object(
            "object_a",
            include_descendants=False,
        ) == []

        assert_package_valid(pkg)


def test_update_annotation_keeps_other_object_links(tmp_path: Path) -> None:
    # Only the *old primary* link is rewritten. Links of other kinds record
    # separate facts (here: which survey the claim came from) and must survive
    # a move of the primary object.
    with make_pkg(tmp_path) as pkg:
        annotation_id, _object_a, object_b = _moveable_annotation(pkg)

        survey_id = pkg.create_city_object(object_uid="survey_object_7")

        pkg.link_annotation_to_object(
            annotation_id=annotation_id,
            city_object_id=survey_id,
            relation_type="derivedFrom",
        )

        pkg.update_annotation(annotation_id, primary_city_object_id=object_b)

        assert _represents_links(pkg, annotation_id) == [object_b]

        links = pkg.conn.execute(
            """
            SELECT city_object_id, relation_type
            FROM usap_annotation_object
            WHERE annotation_id = ?
              AND relation_type = 'derivedFrom'
            """,
            (annotation_id,),
        ).fetchall()

        assert [int(row["city_object_id"]) for row in links] == [survey_id]

        assert_package_valid(pkg)


def test_update_annotation_link_move_rolls_back_with_its_transaction(
    tmp_path: Path,
) -> None:
    # The column and the link row must move together or not at all: a caller
    # transaction that fails afterwards must not leave the annotation pointing
    # at one object while the link table points at another.
    with make_pkg(tmp_path) as pkg:
        annotation_id, object_a, object_b = _moveable_annotation(pkg)

        with pytest.raises(RuntimeError, match="caller failed"):
            with pkg.transaction():
                pkg.update_annotation(
                    annotation_id,
                    primary_city_object_id=object_b,
                )

                raise RuntimeError("caller failed")

        annotation = pkg.get_annotation(annotation_id)

        assert annotation is not None
        assert annotation["primary_city_object_id"] == object_a
        assert _represents_links(pkg, annotation_id) == [object_a]

        assert_package_valid(pkg)


def test_update_annotation_repairs_missing_primary_object_link(
    tmp_path: Path,
) -> None:
    # Re-stating the current primary object is the repair path for an
    # annotation created with link_primary_object=False (or written by raw
    # SQL): validation flags it, and setting the same value fixes it.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        roof_class_id = pkg.create_semantic_class(
            scheme="s",
            class_uri="s:Roof",
            local_name="Roof",
        )

        object_a = pkg.create_city_object(object_uid="object_a")

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_unlinked",
            semantic_class_id=roof_class_id,
            primary_city_object_id=object_a,
            link_primary_object=False,
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=part,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[1, 2],
        )

        assert _represents_links(pkg, annotation_id) == []
        assert not pkg.validate_report().is_ok

        pkg.update_annotation(annotation_id, primary_city_object_id=object_a)

        assert _represents_links(pkg, annotation_id) == [object_a]
        assert_package_valid(pkg)


def _annotated(pkg, attributes_json: str | None = None) -> int:
    """One annotation, optionally carrying attributes already."""
    class_id = pkg.create_semantic_class(
        scheme="local",
        class_uri="local:Roof",
        local_name="Roof",
    )

    return pkg.create_annotation(
        annotation_uid="ann_merge",
        semantic_class_id=class_id,
        attributes_json=attributes_json,
    )


def _attributes(pkg, annotation_id: int) -> dict:
    stored = pkg.get_annotation(annotation_id)["attributes_json"]
    return {} if stored is None else json.loads(stored)


def test_update_annotation_attributes_merges_rather_than_replacing(
    tmp_path: Path,
) -> None:
    # The whole point of the kwarg. attributes_json is a multi-key field whose
    # contents are prescribed (method, source, the reserved usap: keys), so
    # setting one key by rewriting the column silently discards the rest --
    # which is what every caller had to do before this existed.
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(
            pkg, json.dumps({"method": "roof_detector_v2", "source": "survey"})
        )

        pkg.update_annotation(
            annotation_id,
            attributes={"usap:label": "Via Etnea, tratto 4"},
        )

        assert _attributes(pkg, annotation_id) == {
            "method": "roof_detector_v2",
            "source": "survey",
            "usap:label": "Via Etnea, tratto 4",
        }


def test_update_annotation_attributes_replaces_a_key_it_names(
    tmp_path: Path,
) -> None:
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(
            pkg, json.dumps({"method": "v2", "source": "survey"})
        )

        pkg.update_annotation(annotation_id, attributes={"method": "v3"})

        assert _attributes(pkg, annotation_id) == {
            "method": "v3",
            "source": "survey",
        }


def test_update_annotation_attributes_none_removes_a_key(tmp_path: Path) -> None:
    # None removes the key rather than storing a JSON null: a key whose value
    # is null still reads as present, which is a different claim.
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(
            pkg, json.dumps({"method": "v2", "source": "survey"})
        )

        pkg.update_annotation(annotation_id, attributes={"method": None})

        assert _attributes(pkg, annotation_id) == {"source": "survey"}


def test_update_annotation_attributes_merges_into_an_empty_field(
    tmp_path: Path,
) -> None:
    # A fresh annotation stores NULL, and merging into nothing is the first
    # thing an application that sets a label will ever do.
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(pkg)

        assert pkg.get_annotation(annotation_id)["attributes_json"] is None

        pkg.update_annotation(annotation_id, attributes={"usap:label": "L"})

        assert _attributes(pkg, annotation_id) == {"usap:label": "L"}


def test_update_annotation_attributes_emptied_stays_an_object(
    tmp_path: Path,
) -> None:
    # Removing the last key leaves '{}', not NULL. Blanking the field outright
    # is attributes_json=None's job, and the two mean different things.
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(pkg, json.dumps({"method": "v2"}))

        pkg.update_annotation(annotation_id, attributes={"method": None})

        assert pkg.get_annotation(annotation_id)["attributes_json"] == "{}"


def test_update_annotation_refuses_attributes_and_attributes_json_together(
    tmp_path: Path,
) -> None:
    # Merging and replacing are different intentions; picking a winner would
    # silently do one of them.
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(pkg, json.dumps({"method": "v2"}))

        with pytest.raises(USAPError, match="not both"):
            pkg.update_annotation(
                annotation_id,
                attributes={"usap:label": "L"},
                attributes_json='{"usap:label": "L"}',
            )

        # And the refused call wrote nothing.
        assert _attributes(pkg, annotation_id) == {"method": "v2"}


def test_update_annotation_attributes_refuses_a_non_object_stored_value(
    tmp_path: Path,
) -> None:
    # A stored JSON array is valid JSON and passes the column's own check, but
    # there is no sane way to merge keys into it -- and quietly replacing it
    # would lose whatever it held.
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(pkg, "[1, 2, 3]")

        with pytest.raises(USAPError, match="rather than an object"):
            pkg.update_annotation(annotation_id, attributes={"k": "v"})

        assert pkg.get_annotation(annotation_id)["attributes_json"] == "[1, 2, 3]"


def test_update_annotation_attributes_requires_a_dict(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(pkg)

        with pytest.raises(USAPError, match="must be a dict"):
            pkg.update_annotation(annotation_id, attributes="usap:label=L")


def test_label_round_trips_through_every_read_path(tmp_path: Path) -> None:
    # US.md wants the label in the detail view (line 182), the list (198) and
    # the selection result list (US-SELECT-02). All three are separate SELECTs,
    # so all three are asserted: a column added to one of them and forgotten in
    # another is exactly how the 0.4.2 gml_id/object_uid mismatch happened.
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        pkg.create_semantic_class(
            scheme="local", class_uri="local:Road", local_name="Road"
        )

        annotation = pkg.annotate_elements(
            concept="Road",
            asset_part_id=part,
            element_kind="face",
            element_indices=[41, 42, 43],
            label="Via Etnea, tratto 4",
        )
        annotation_id = annotation["annotation_id"]

        assert pkg.get_annotation(annotation_id)["label"] == "Via Etnea, tratto 4"
        assert pkg.list_annotations()[0]["label"] == "Via Etnea, tratto 4"

        hits = pkg.annotations_for_elements(part, ELEMENT_KIND_FACE, [41])
        assert hits[0]["label"] == "Via Etnea, tratto 4"

        assert_package_valid(pkg)


def test_label_is_editable_and_clearable(tmp_path: Path) -> None:
    # US-ANN-06 requires it editable. Clearing is a separate case from editing:
    # a caption someone typed by mistake has to be removable, not just
    # replaceable with other text.
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(pkg)

        pkg.update_annotation(annotation_id, label="first")
        assert pkg.get_annotation(annotation_id)["label"] == "first"

        pkg.update_annotation(annotation_id, label="second")
        assert pkg.get_annotation(annotation_id)["label"] == "second"

        pkg.update_annotation(annotation_id, label=None)
        assert pkg.get_annotation(annotation_id)["label"] is None


def test_omitting_the_label_leaves_it_alone(tmp_path: Path) -> None:
    # The _UNSET-by-omission rule, which is what lets the batch do a partial
    # update: editing a status must not blank a caption.
    with make_pkg(tmp_path) as pkg:
        annotation_id = _annotated(pkg)
        pkg.update_annotation(annotation_id, label="keep me")

        pkg.update_annotation(annotation_id, status="accepted")

        assert pkg.get_annotation(annotation_id)["label"] == "keep me"


def test_the_label_is_not_an_identifier(tmp_path: Path) -> None:
    # The whole reason a label column is safe to have. It carries no UNIQUE and
    # no lookup accepts it, so it cannot become a fourth name to reconcile
    # beside annotation_uid, object_uid and gml_id -- which is the objection
    # that removed the original column in 0.4.0.
    with make_pkg(tmp_path) as pkg:
        first = _annotated(pkg)
        pkg.update_annotation(first, label="not unique")

        # Two annotations may legitimately carry one caption.
        class_id = pkg.get_annotation(first)["semantic_class_id"]
        second = pkg.create_annotation(
            annotation_uid="ann_merge_2",
            semantic_class_id=class_id,
            label="not unique",
        )

        assert pkg.get_annotation(second)["label"] == "not unique"
        assert_package_valid(pkg)

        # And it is not a way to find anything.
        assert pkg.get_annotation(annotation_uid="not unique") is None

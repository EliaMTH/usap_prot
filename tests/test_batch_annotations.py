from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_mesh_part, make_pkg, seed_citygml_concepts
from conftest import write_tiny_las as _write_tiny_las, write_tiny_mesh as _write_tiny_mesh
from usap import (
    USAPError,
    USAPPackage,
    apply_annotation_batch,
    apply_annotation_batch_file,
    register_las_asset,
    register_mesh_asset,
    seed_default_ade_vocabulary,
)


def test_apply_annotation_batch_with_las_and_mesh(tmp_path: Path) -> None:
    db_path = tmp_path / "batch.usap.gpkg"
    las_path = tmp_path / "tiny.las"
    mesh_path = tmp_path / "tiny.ply"

    _write_tiny_las(las_path, point_count=10)
    _write_tiny_mesh(mesh_path)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        citygml_vocab = seed_citygml_concepts(pkg)
        seed_default_ade_vocabulary(pkg)

        roof_object_id = pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=citygml_vocab.by_name["RoofSurface"],
            gml_id="building_1_roof_1",
        )

        las = register_las_asset(pkg, las_path)
        mesh = register_mesh_asset(
            pkg,
            mesh_path,
            representation_name="tiny_mesh",
            representation_kind="triangulated_surface",
            lod=None,
        )

        batch = {
            "annotations": [
                {
                    "annotation_uid": "ann_batch_energy_roof",
                    "concept": "EnergyRoof",
                    "city_object_uid": "building_1_roof_1",
                    "status": "draft",
                    "attributes": {
                        "domain": "energy_emissions"
                    },
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [1, 2, 3]
                        },
                        {
                            "asset_part_id": mesh.primary_asset_part_id,
                            "element_kind": "face",
                            "element_indices": [0, 1]
                        }
                    ]
                }
            ]
        }

        result = apply_annotation_batch(pkg, batch)

        assert result.annotation_count == 1
        assert result.membership_count == 2

        annotation = pkg.get_annotation(
            annotation_uid="ann_batch_energy_roof",
            include_membership_summary=True,
        )

        assert annotation is not None
        assert annotation["semantic_class"] == "EnergyRoof"
        assert annotation["primary_city_object_id"] == roof_object_id
        assert len(annotation["membership_summary"]) == 2

        las_matches = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[2],
        )

        mesh_matches = pkg.annotations_for_elements(
            asset_part_id=mesh.primary_asset_part_id,
            element_kind="face",
            selected_indices=[1],
        )

        assert las_matches[0]["annotation_uid"] == "ann_batch_energy_roof"
        assert mesh_matches[0]["annotation_uid"] == "ann_batch_energy_roof"

        report = pkg.validate_report()
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_batch_rejects_unknown_concept(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown_batch.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        register_las_asset(pkg, las_path)

        batch = {
            "annotations": [
                {
                    "annotation_uid": "ann_unknown",
                    "concept": "DefinitelyNotRegistered",
                    "memberships": [
                        {
                            "asset_part_id": 1,
                            "element_kind": "point",
                            "element_indices": [1]
                        }
                    ]
                }
            ]
        }

        with pytest.raises(USAPError, match="concept not found"):
            apply_annotation_batch(pkg, batch)


def test_batch_rejects_out_of_range_indices(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_indices.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=5)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        seed_citygml_concepts(pkg)
        las = register_las_asset(pkg, las_path)

        batch = {
            "annotations": [
                {
                    "annotation_uid": "ann_bad_index",
                    "concept": "RoofSurface",
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [99]
                        }
                    ]
                }
            ]
        }

        with pytest.raises(USAPError, match="out of range"):
            apply_annotation_batch(pkg, batch)


def test_batch_replace_existing(tmp_path: Path) -> None:
    db_path = tmp_path / "replace.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        seed_citygml_concepts(pkg)
        las = register_las_asset(pkg, las_path)

        batch_1 = {
            "annotations": [
                {
                    "annotation_uid": "ann_replace",
                    "concept": "RoofSurface",
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [1, 2]
                        }
                    ]
                }
            ]
        }

        batch_2 = {
            "annotations": [
                {
                    "annotation_uid": "ann_replace",
                    "concept": "RoofSurface",
                    "status": "accepted",
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [3, 4]
                        }
                    ]
                }
            ]
        }

        apply_annotation_batch(pkg, batch_1)

        with pytest.raises(USAPError, match="already exists"):
            apply_annotation_batch(pkg, batch_2)

        apply_annotation_batch(
            pkg,
            batch_2,
            replace_existing=True,
        )

        annotation = pkg.get_annotation(
            annotation_uid="ann_replace",
            include_membership_summary=True,
        )

        assert annotation is not None
        assert annotation["status"] == "accepted"

        matches_old = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[1],
        )

        matches_new = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[3],
        )

        assert matches_old == []
        assert matches_new[0]["annotation_uid"] == "ann_replace"


def test_batch_replace_preserves_omitted_fields(tmp_path: Path) -> None:
    # Re-applying a batch with replace_existing is a *partial* update: fields
    # omitted from the entry must keep their existing values rather than being
    # wiped to NULL. This guards against silent data loss when a follow-up
    # batch only carries new memberships.
    db_path = tmp_path / "replace_preserve.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        citygml_vocab = seed_citygml_concepts(pkg)
        seed_default_ade_vocabulary(pkg)

        roof_object_id = pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=citygml_vocab.by_name["RoofSurface"],
            gml_id="building_1_roof_1",
        )

        las = register_las_asset(pkg, las_path)

        full = {
            "annotations": [
                {
                    "annotation_uid": "ann_preserve",
                    "concept": "EnergyRoof",
                    "city_object_uid": "building_1_roof_1",
                    "confidence": 0.75,
                    "attributes": {"domain": "energy_emissions"},
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [1, 2],
                        }
                    ],
                }
            ]
        }

        # Minimal replacement: only the required fields + new memberships.
        minimal = {
            "annotations": [
                {
                    "annotation_uid": "ann_preserve",
                    "concept": "EnergyRoof",
                    "memberships": [
                        {
                            "asset_part_id": las.asset_part_id,
                            "element_kind": "point",
                            "element_indices": [3, 4],
                        }
                    ],
                }
            ]
        }

        apply_annotation_batch(pkg, full)
        apply_annotation_batch(pkg, minimal, replace_existing=True)

        annotation = pkg.get_annotation(annotation_uid="ann_preserve")

        assert annotation is not None
        # Omitted fields are preserved, not cleared to NULL:
        assert annotation["confidence"] == 0.75
        assert annotation["attributes_json"] is not None
        assert json.loads(annotation["attributes_json"]) == {
            "domain": "energy_emissions"
        }
        assert annotation["primary_city_object_id"] == roof_object_id

        # The membership was still replaced by the new indices.
        matches_new = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[3],
        )
        matches_old = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind="point",
            selected_indices=[1],
        )

        assert matches_new[0]["annotation_uid"] == "ann_preserve"
        assert matches_old == []

def test_batch_replace_moves_primary_object_link(tmp_path: Path) -> None:
    # Re-applying a batch entry against a different city object moves the
    # annotation. The batch path used to add the new link without removing the
    # old one, leaving the annotation answering queries for both objects.
    db_path = tmp_path / "replace_move.usap.gpkg"
    las_path = tmp_path / "tiny.las"

    _write_tiny_las(las_path, point_count=10)

    with USAPPackage.create(
        db_path,
        overwrite=True,
    ) as pkg:
        citygml_vocab = seed_citygml_concepts(pkg)

        for uid in ("roof_a", "roof_b"):
            pkg.create_city_object(
                object_uid=uid,
                semantic_class_id=citygml_vocab.by_name["RoofSurface"],
            )

        las = register_las_asset(pkg, las_path)

        def batch_for(object_uid: str) -> dict:
            return {
                "annotations": [
                    {
                        "annotation_uid": "ann_moved",
                        "concept": "RoofSurface",
                        "city_object_uid": object_uid,
                        "memberships": [
                            {
                                "asset_part_id": las.asset_part_id,
                                "element_kind": "point",
                                "element_indices": [1, 2],
                            }
                        ],
                    }
                ]
            }

        apply_annotation_batch(pkg, batch_for("roof_a"))
        apply_annotation_batch(
            pkg,
            batch_for("roof_b"),
            replace_existing=True,
        )

        annotation = pkg.get_annotation(annotation_uid="ann_moved")

        assert annotation is not None
        assert annotation["primary_city_object_uid"] == "roof_b"

        links = pkg.conn.execute(
            """
            SELECT co.object_uid
            FROM usap_annotation_object AS ao
            JOIN usap_city_object AS co
                ON co.city_object_id = ao.city_object_id
            WHERE ao.annotation_id = ?
            """,
            (annotation["annotation_id"],),
        ).fetchall()

        assert [row["object_uid"] for row in links] == ["roof_b"]

        assert pkg.elements_for_city_object(
            "roof_a",
            include_descendants=False,
        ) == []

        moved = pkg.elements_for_city_object("roof_b", include_descendants=False)

        assert {block["annotation_id"] for block in moved} == {
            annotation["annotation_id"]
        }


def test_apply_annotation_batch_file(tmp_path: Path) -> None:
    # INGESTION.md procedure 3 relies on this file entry point for
    # standalone edits; it must behave exactly like the in-memory batch
    # and fail loudly on a missing path.
    with make_pkg(tmp_path) as pkg:
        make_mesh_part(pkg)
        pkg.create_semantic_class(
            scheme="local", class_uri="local:TempRoof", local_name="TempRoof"
        )

        batch_path = tmp_path / "batch.json"
        batch_path.write_text(
            json.dumps(
                {
                    "create_missing_city_objects": True,
                    "annotations": [
                        {
                            "city_object_uid": "tower_A_roof",
                            "concept": "TempRoof",
                            "memberships": [
                                {
                                    "asset_uri": "mesh.ply",
                                    "element_indices": [0, 1],
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = apply_annotation_batch_file(pkg, batch_path)

        assert result.annotation_count == 1

        blocks = pkg.elements_for_city_object("tower_A_roof", expand=True)
        assert [b["elements"] for b in blocks] == [[0, 1]]

        with pytest.raises(FileNotFoundError, match="Batch file not found"):
            apply_annotation_batch_file(pkg, tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# Carrier objects: what the batch writes, and what it deliberately does not
# ---------------------------------------------------------------------------

def _carrier_pkg(tmp_path: Path) -> USAPPackage:
    pkg = make_pkg(tmp_path)
    pkg.create_semantic_class(
        scheme="local", class_uri="local:TempRoof", local_name="TempRoof"
    )
    pkg.create_semantic_class(
        scheme="local", class_uri="local:TempWall", local_name="TempWall"
    )
    asset_id = pkg.register_asset(uri="mesh.ply", asset_kind="mesh")
    pkg.register_asset_part(asset_id, "g/0", "face", 100, indexing_profile="t:v1")
    return pkg


def _batch(**item):
    item.setdefault("memberships", [{"asset_uri": "mesh.ply", "element_indices": [0]}])
    return {"create_missing_city_objects": True, "annotations": [item]}


def test_batch_passes_gml_id_and_source_object_id_through(tmp_path: Path) -> None:
    # Without this the package is orphaned with respect to its CityGML: gml_id
    # is the only column carrying the object's identity on that side, and the
    # writer had to pre-create every object by hand just to set it.
    with _carrier_pkg(tmp_path) as pkg:
        apply_annotation_batch(pkg, _batch(
            city_object_uid="tower_A_roof",
            gml_id="tower_A_roof",
            source_object_id="tower_A_roof",
            concept="TempRoof",
        ))

        row = pkg.conn.execute(
            """
            SELECT gml_id, source_object_id, semantic_class_id, object_status
            FROM usap_city_object WHERE object_uid = 'tower_A_roof'
            """
        ).fetchone()

        assert row["gml_id"] == "tower_A_roof"
        assert row["source_object_id"] == "tower_A_roof"
        assert row["object_status"] == "temporary"

        # And the carrier is classless: the concept classed the annotation,
        # not the object. Its class belongs to the CityGML that aligns it.
        assert row["semantic_class_id"] is None


def _identity(pkg, object_uid: str):
    return pkg.conn.execute(
        """
        SELECT gml_id, source_object_id
        FROM usap_city_object WHERE object_uid = ?
        """,
        (object_uid,),
    ).fetchone()


def test_identity_reaches_an_object_that_already_existed(tmp_path: Path) -> None:
    # The shape that matters most, because it is the one every re-run takes and
    # the one a writer that pre-creates its objects takes on the *first* run.
    # The fields used to be passed only where the batch created the object, so
    # an entry naming an object something else had already made reported
    # success and wrote nothing.
    with _carrier_pkg(tmp_path) as pkg:
        pkg.create_city_object(object_uid="tower_A_roof")

        result = apply_annotation_batch(pkg, _batch(
            city_object_uid="tower_A_roof",
            gml_id="B1_roof",
            source_object_id="SRC-1",
            concept="TempRoof",
        ))

        assert result.annotation_count == 1

        row = _identity(pkg, "tower_A_roof")
        assert row["gml_id"] == "B1_roof"
        assert row["source_object_id"] == "SRC-1"


def test_identity_reaches_an_object_named_by_id(tmp_path: Path) -> None:
    # An entry may name its object either way, and create_city_object is keyed
    # on the uid -- so without the lookup the int form dropped the identity
    # every time, by construction rather than by accident.
    with _carrier_pkg(tmp_path) as pkg:
        city_object_id = pkg.create_city_object(object_uid="tower_A_roof")

        apply_annotation_batch(pkg, {"annotations": [{
            "annotation_uid": "a1",
            "city_object_id": city_object_id,
            "concept": "TempRoof",
            "gml_id": "B1_roof",
            "memberships": [{"asset_uri": "mesh.ply", "element_indices": [0]}],
        }]})

        assert _identity(pkg, "tower_A_roof")["gml_id"] == "B1_roof"


def test_identity_reaches_an_object_named_by_its_gml_id(tmp_path: Path) -> None:
    # resolve_city_object matches object_uid OR gml_id, while create_city_object
    # is keyed on object_uid alone and inserts when it finds nothing. So an
    # entry naming its object by gml_id has to write to the row resolve
    # matched, not to the string it was given: keying the write by the string
    # minted a second row instead -- a classless phantom nothing referenced,
    # two rows claiming one gml_id, and resolve_city_object raising
    # USAPAmbiguityError for that value from then on, while the batch reported
    # success with created_city_object_count 0.
    with _carrier_pkg(tmp_path) as pkg:
        # object_uid deliberately is *not* the gml_id. That is the shape
        # HANDOFF 2.1 asks writers to avoid and DUPLICATE_GML_ID exists to
        # report -- which is exactly the population this write must not break.
        city_object_id = pkg.create_city_object(
            object_uid="obj-A",
            gml_id="B1_roof",
        )

        result = apply_annotation_batch(pkg, _batch(
            city_object_uid="B1_roof",  # resolves via gml_id, not object_uid
            gml_id="B1_roof",
            source_object_id="SRC-1",
            concept="TempRoof",
        ))

        assert result.annotation_count == 1
        assert result.created_city_object_uids == []

        rows = pkg.conn.execute(
            "SELECT city_object_id FROM usap_city_object"
        ).fetchall()
        assert len(rows) == 1, "the identity write must not insert a second row"

        row = _identity(pkg, "obj-A")
        assert row["gml_id"] == "B1_roof"
        assert row["source_object_id"] == "SRC-1"

        # The two consequences a phantom row had, pinned so they cannot return.
        assert pkg.resolve_city_object("B1_roof") == city_object_id
        assert pkg.validate_report().is_ok


def test_re_running_the_same_identity_is_not_a_conflict(tmp_path: Path) -> None:
    # Re-running a batch is the normal way to import a re-survey, so the second
    # pass must not trip over what the first one wrote.
    with _carrier_pkg(tmp_path) as pkg:
        batch = _batch(
            city_object_uid="tower_A_roof",
            gml_id="B1_roof",
            concept="TempRoof",
        )

        apply_annotation_batch(pkg, batch)
        apply_annotation_batch(pkg, batch, replace_existing=True)

        assert _identity(pkg, "tower_A_roof")["gml_id"] == "B1_roof"


def test_a_changed_identity_raises_rather_than_being_ignored(tmp_path: Path) -> None:
    # The half of the fix that is about telling the truth rather than about
    # writing more: a generator whose gml_id was wrong and has been corrected
    # must hear that the package disagrees, not silently keep the old value.
    with _carrier_pkg(tmp_path) as pkg:
        apply_annotation_batch(pkg, _batch(
            city_object_uid="tower_A_roof",
            gml_id="B1_roof",
            concept="TempRoof",
        ))

        with pytest.raises(USAPError, match="already exists with a different"):
            apply_annotation_batch(pkg, _batch(
                city_object_uid="tower_A_roof",
                gml_id="SOMETHING_ELSE",
                concept="TempRoof",
            ), replace_existing=True)

        # And the refused call wrote nothing.
        assert _identity(pkg, "tower_A_roof")["gml_id"] == "B1_roof"


def test_an_item_referencing_a_carrier_must_carry_its_own_concept(tmp_path: Path) -> None:
    # The one shape classless carriers break, pinned deliberately. A later item
    # naming an earlier item's carrier used to inherit its class -- a value the
    # batch had itself invented one item before, then read back through a rule
    # commented "semantics from the CityGML side".
    with _carrier_pkg(tmp_path) as pkg:
        batch = {
            "create_missing_city_objects": True,
            "annotations": [
                {
                    "annotation_uid": "a1",
                    "city_object_uid": "tower_A_roof",
                    "concept": "TempRoof",
                    "memberships": [
                        {"asset_uri": "mesh.ply", "element_indices": [0]}
                    ],
                },
                {
                    "annotation_uid": "a2",
                    "city_object_uid": "tower_A_roof",
                    # no concept: nothing left to inherit
                    "memberships": [
                        {"asset_uri": "mesh.ply", "element_indices": [1]}
                    ],
                },
            ],
        }

        with pytest.raises(ValueError, match="deliberately classless"):
            apply_annotation_batch(pkg, batch)


def test_an_item_supplying_its_own_concept_is_unaffected(tmp_path: Path) -> None:
    with _carrier_pkg(tmp_path) as pkg:
        result = apply_annotation_batch(pkg, {
            "create_missing_city_objects": True,
            "annotations": [
                {
                    "annotation_uid": "a1",
                    "city_object_uid": "tower_A_roof",
                    "concept": "TempRoof",
                    "memberships": [
                        {"asset_uri": "mesh.ply", "element_indices": [0]}
                    ],
                },
                {
                    "annotation_uid": "a2",
                    "city_object_uid": "tower_A_roof",
                    "concept": "TempWall",
                    "memberships": [
                        {"asset_uri": "mesh.ply", "element_indices": [1]}
                    ],
                },
            ],
        })

        assert result.annotation_count == 2
        assert result.created_city_object_count == 1


# ---------------------------------------------------------------------------
# Key names: a value you never read is a value you cannot validate
# ---------------------------------------------------------------------------

def test_a_batch_entry_can_carry_a_label(tmp_path: Path) -> None:
    # `label` is a column again, so the batch has to both accept the key and
    # write it. Accepting it is not free: the key whitelist refuses anything it
    # does not name, so a column the batch forgot to list would make every
    # entry carrying it fail -- and batch files predating 0.4.0 do carry it.
    with _carrier_pkg(tmp_path) as pkg:
        apply_annotation_batch(pkg, _batch(
            annotation_uid="a1",
            city_object_uid="tower_A_roof",
            concept="TempRoof",
            label="Via Etnea, tratto 4",
        ))

        assert pkg.get_annotation(annotation_uid="a1")["label"] == (
            "Via Etnea, tratto 4"
        )


def test_a_re_run_without_a_label_leaves_the_stored_one(tmp_path: Path) -> None:
    # Replace is a partial update. A caption a user typed in the application
    # must survive the next run of a generator that knows nothing about it --
    # the failure mode the old attributes-based label had, since an entry's
    # `attributes` replaces the whole field.
    with _carrier_pkg(tmp_path) as pkg:
        apply_annotation_batch(pkg, _batch(
            annotation_uid="a1", city_object_uid="tower_A_roof",
            concept="TempRoof", label="typed by a user",
        ))

        apply_annotation_batch(pkg, _batch(
            annotation_uid="a1", city_object_uid="tower_A_roof",
            concept="TempRoof",
        ), replace_existing=True)

        assert pkg.get_annotation(annotation_uid="a1")["label"] == "typed by a user"

        # And an entry that names it explicitly still changes it, including to
        # nothing: "label": null is a value, not an omission.
        apply_annotation_batch(pkg, _batch(
            annotation_uid="a1", city_object_uid="tower_A_roof",
            concept="TempRoof", label=None,
        ), replace_existing=True)

        assert pkg.get_annotation(annotation_uid="a1")["label"] is None


def test_an_unknown_item_key_is_refused(tmp_path: Path) -> None:
    # A bad *value* already fails today. What escaped was the key name: a batch
    # generating 8,869 entries gets a misspelled field wrong in every entry, on
    # every run, and the package still validates clean.
    with _carrier_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="confidance"):
            apply_annotation_batch(pkg, _batch(
                city_object_uid="tower_A_roof",
                concept="TempRoof",
                confidance=0.9,          # typo for "confidence"
            ))


def test_unknown_keys_are_refused_before_anything_is_written(tmp_path: Path) -> None:
    # The check runs before the transaction opens, so a typo in the last entry
    # does not roll back every entry before it -- it prevents the write instead.
    with _carrier_pkg(tmp_path) as pkg:
        batch = {
            "create_missing_city_objects": True,
            "annotations": [
                {
                    "annotation_uid": "good",
                    "city_object_uid": "tower_A_roof",
                    "concept": "TempRoof",
                    "memberships": [
                        {"asset_uri": "mesh.ply", "element_indices": [0]}
                    ],
                },
                {
                    "annotation_uid": "bad",
                    "city_object_uid": "tower_A_wall",
                    "concept": "TempWall",
                    "totally_made_up_key": 1,
                    "memberships": [
                        {"asset_uri": "mesh.ply", "element_indices": [1]}
                    ],
                },
            ],
        }

        with pytest.raises(USAPError, match="totally_made_up_key"):
            apply_annotation_batch(pkg, batch)

        assert pkg.conn.execute(
            "SELECT COUNT(*) AS n FROM usap_annotation"
        ).fetchone()["n"] == 0


def test_unknown_keys_are_refused_in_nested_blocks(tmp_path: Path) -> None:
    with _carrier_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="element_indeces"):
            apply_annotation_batch(pkg, {
                "create_missing_city_objects": True,
                "annotations": [{
                    "city_object_uid": "tower_A_roof",
                    "concept": "TempRoof",
                    "memberships": [
                        {"asset_uri": "mesh.ply", "element_indeces": [0]}
                    ],
                }],
            })


def test_an_unknown_top_level_key_is_refused(tmp_path: Path) -> None:
    with _carrier_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="create_missing_cityobjects"):
            apply_annotation_batch(pkg, {
                "create_missing_cityobjects": True,      # missing underscore
                "annotations": [],
            })


def test_an_underscore_prefixed_key_is_a_comment(tmp_path: Path) -> None:
    # The same escape project_builder configs use, so a generated batch can
    # carry provenance without the importer treating it as a dropped intention.
    with _carrier_pkg(tmp_path) as pkg:
        result = apply_annotation_batch(pkg, {
            "_comment": "generated by the export bridge",
            "create_missing_city_objects": True,
            "annotations": [{
                "_source_row": 41,
                "city_object_uid": "tower_A_roof",
                "concept": "TempRoof",
                "memberships": [
                    {"_note": "from the lasso", "asset_uri": "mesh.ply",
                     "element_indices": [0]}
                ],
            }],
        })

        assert result.annotation_count == 1


# ---------------------------------------------------------------------------
# Ordered paths in a batch (docs/ORDERED_PATHS_DESIGN.md §4.6)
#
# "path" is a sibling of "memberships", not a key inside one: a memberships
# entry is per-part, and a path is cross-part by construction.
# ---------------------------------------------------------------------------


def _path_batch_package(tmp_path: Path):
    pkg = make_pkg(tmp_path, name="pathbatch.usap.gpkg")
    asset_id = pkg.register_asset(uri="road.ply", asset_kind="mesh")
    parts = [
        pkg.register_asset_part(
            asset_id=asset_id,
            part_path=f"geometry/{index}",
            element_kind=1,
            element_count=1000,
            indexing_profile="usap:test-face-order-v1",
        )
        for index in range(2)
    ]
    pkg.create_semantic_class(
        scheme="local",
        class_uri="local:RoadCentreline",
        local_name="RoadCentreline",
    )

    return pkg, parts


def test_a_path_only_entry_is_applied(tmp_path: Path) -> None:
    # No "memberships" key at all: the membership is derived from the path, so
    # requiring one would make the ordinary road entry illegal.
    pkg, (tile_a, tile_b) = _path_batch_package(tmp_path)

    with pkg:
        result = apply_annotation_batch(
            pkg,
            {
                "annotations": [
                    {
                        "annotation_uid": "ann_via_roma",
                        "concept": "RoadCentreline",
                        "label": "Via Roma, centreline",
                        "path": [
                            {
                                "asset_part_id": tile_a,
                                "element_indices": [998, 999],
                            },
                            {
                                "asset_part_id": tile_b,
                                "element_indices": [0, 1, 2],
                                "continues_previous": True,
                            },
                        ],
                    }
                ]
            },
        )

        assert result.path_segment_count == 2
        assert result.annotations[0].path_segment_count == 2

        annotation_id = result.annotations[0].annotation_id
        runs = pkg.path_for_annotation(annotation_id)

        assert len(runs) == 1
        assert [s["element_indices"] for s in runs[0]["segments"]] == [
            [998, 999],
            [0, 1, 2],
        ]

        report = pkg.validate_report()
        assert report.is_ok, [i.format() for i in report.issues]


def test_a_batch_path_preserves_order(tmp_path: Path) -> None:
    pkg, (tile_a, _tile_b) = _path_batch_package(tmp_path)

    with pkg:
        result = apply_annotation_batch(
            pkg,
            {
                "annotations": [
                    {
                        "annotation_uid": "ann_road",
                        "concept": "RoadCentreline",
                        "path": [
                            {
                                "asset_part_id": tile_a,
                                "element_indices": [40, 12, 12, 7],
                            }
                        ],
                    }
                ]
            },
        )

        annotation_id = result.annotations[0].annotation_id
        runs = pkg.path_for_annotation(annotation_id)

        assert runs[0]["segments"][0]["element_indices"] == [40, 12, 12, 7]

        blocks = pkg.elements_for_annotation(annotation_id, expand=True)
        assert [e for b in blocks for e in b["elements"]] == [7, 12, 40]


def test_path_and_membership_on_the_same_part_is_refused(
    tmp_path: Path,
) -> None:
    pkg, (tile_a, _tile_b) = _path_batch_package(tmp_path)

    with pkg:
        # ValueError, like every other entry-shape error in this importer.
        with pytest.raises(ValueError, match="covered by both"):
            apply_annotation_batch(
                pkg,
                {
                    "annotations": [
                        {
                            "annotation_uid": "ann_road",
                            "concept": "RoadCentreline",
                            "memberships": [
                                {
                                    "asset_part_id": tile_a,
                                    "element_indices": [1, 2],
                                }
                            ],
                            "path": [
                                {
                                    "asset_part_id": tile_a,
                                    "element_indices": [40, 12],
                                }
                            ],
                        }
                    ]
                },
            )


def test_path_and_membership_on_different_parts_is_fine(
    tmp_path: Path,
) -> None:
    pkg, (tile_a, tile_b) = _path_batch_package(tmp_path)

    with pkg:
        result = apply_annotation_batch(
            pkg,
            {
                "annotations": [
                    {
                        "annotation_uid": "ann_road",
                        "concept": "RoadCentreline",
                        "memberships": [
                            {"asset_part_id": tile_b, "element_indices": [1, 2]}
                        ],
                        "path": [
                            {
                                "asset_part_id": tile_a,
                                "element_indices": [40, 12],
                            }
                        ],
                    }
                ]
            },
        )

        assert result.membership_count == 1
        assert result.path_segment_count == 1
        assert pkg.validate_report().is_ok


def test_a_rerun_that_drops_the_path_key_is_refused(tmp_path: Path) -> None:
    # The --replace-existing case: an entry writing memberships onto an
    # assessment that carries a path inherits the drop_path rule rather than
    # silently stranding the order.
    pkg, (tile_a, _tile_b) = _path_batch_package(tmp_path)

    with pkg:
        payload = {
            "annotations": [
                {
                    "annotation_uid": "ann_road",
                    "concept": "RoadCentreline",
                    "path": [
                        {"asset_part_id": tile_a, "element_indices": [40, 12]}
                    ],
                }
            ]
        }
        apply_annotation_batch(pkg, payload)

        without_path = {
            "annotations": [
                {
                    "annotation_uid": "ann_road",
                    "concept": "RoadCentreline",
                    "memberships": [
                        {"asset_part_id": tile_a, "element_indices": [1, 2]}
                    ],
                }
            ]
        }

        with pytest.raises(USAPError, match="carries an ordered path"):
            apply_annotation_batch(
                pkg, without_path, replace_existing=True
            )


def test_a_rerun_with_a_path_rewrites_it(tmp_path: Path) -> None:
    pkg, (tile_a, _tile_b) = _path_batch_package(tmp_path)

    with pkg:
        def payload(indices):
            return {
                "annotations": [
                    {
                        "annotation_uid": "ann_road",
                        "concept": "RoadCentreline",
                        "path": [
                            {
                                "asset_part_id": tile_a,
                                "element_indices": indices,
                            }
                        ],
                    }
                ]
            }

        apply_annotation_batch(pkg, payload([40, 12]))
        result = apply_annotation_batch(
            pkg, payload([7, 8, 9]), replace_existing=True
        )

        annotation_id = result.annotations[0].annotation_id
        runs = pkg.path_for_annotation(annotation_id)

        assert runs[0]["segments"][0]["element_indices"] == [7, 8, 9]
        assert pkg.validate_report().is_ok


def test_an_unknown_key_in_a_path_segment_is_refused(tmp_path: Path) -> None:
    pkg, (tile_a, _tile_b) = _path_batch_package(tmp_path)

    with pkg:
        with pytest.raises(USAPError, match="Unrecognised key"):
            apply_annotation_batch(
                pkg,
                {
                    "annotations": [
                        {
                            "annotation_uid": "ann_road",
                            "concept": "RoadCentreline",
                            "path": [
                                {
                                    "asset_part_id": tile_a,
                                    "element_indices": [1],
                                    "continues": True,
                                }
                            ],
                        }
                    ]
                },
            )

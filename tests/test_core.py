from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import make_mesh_part, make_pkg, seed_citygml_concepts
from usap.constants import (
    CITYGML_3_0_BUILDING_NS,
    CITYGML_3_0_CONSTRUCTION_NS,
    concept_uri,
)
from usap import (
    ELEMENT_KIND_FACE,
    ELEMENT_KIND_POINT,
    USAPError,
    USAPPackage,
)
from usap.constants import DEFAULT_BLOCK_SIZE, normalize_element_kind

# The tiny fixture's membership deliberately straddles a block boundary, so the
# split-and-reassemble path stays exercised whatever block size the profile
# uses. Derived rather than hardcoded: pinning it to one block size is how a
# block-size change silently stops these tests from testing anything.
SECOND_BLOCK_FACE = DEFAULT_BLOCK_SIZE + 100
TINY_PART_ELEMENT_COUNT = DEFAULT_BLOCK_SIZE + 4000
TINY_FACES = [100, 101, 102, SECOND_BLOCK_FACE, SECOND_BLOCK_FACE + 1]


def build_tiny_package(db_path: Path) -> tuple[USAPPackage, int, int, int]:
    pkg = USAPPackage.create(
        db_path,
        overwrite=True,
    )

    asset_id = pkg.register_asset(
        uri="city_mesh.ply",
        asset_kind="mesh",
        media_type="application/ply",
        # No file backs this fixture, so the digest is invented — but it is
        # spelled canonically ('algorithm:digest'), or deep validation would
        # flag NON_CANONICAL_CONTENT_HASH in every test that checks an
        # exact issue list.
        content_hash="sha256:" + "11" * 32,
    )

    asset_part_id = pkg.register_asset_part(
        asset_id=asset_id,
        part_path="node=0/mesh=0/primitive=0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=TINY_PART_ELEMENT_COUNT,
    )

    building_class_id = pkg.create_semantic_class(
        scheme="citygml",
        scheme_version="3.0",
        class_uri=concept_uri(CITYGML_3_0_BUILDING_NS, "Building"),
        local_name="Building",
    )

    roof_class_id = pkg.create_semantic_class(
        scheme="citygml",
        scheme_version="3.0",
        class_uri=concept_uri(CITYGML_3_0_CONSTRUCTION_NS, "RoofSurface"),
        local_name="RoofSurface",
    )

    building_id = pkg.create_city_object(
        object_uid="building_1",
        semantic_class_id=building_class_id,
    )

    roof_id = pkg.create_city_object(
        object_uid="building_1_roof_1",
        semantic_class_id=roof_class_id,
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
        annotation_uid="ann_building_1_roof_mesh",
        semantic_class_id=roof_class_id,
        primary_city_object_id=roof_id,
        status="accepted",
        confidence=1.0,
    )

    pkg.replace_annotation_membership(
        annotation_id=annotation_id,
        asset_part_id=asset_part_id,
        element_kind=ELEMENT_KIND_FACE,
        element_indices=TINY_FACES,
    )

    return pkg, asset_part_id, roof_class_id, annotation_id


def test_selected_face_returns_roof_annotation(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    pkg, asset_part_id, _roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        matches = pkg.annotations_for_elements(
            asset_part_id=asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[SECOND_BLOCK_FACE],
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_building_1_roof_mesh"
        assert matches[0]["semantic_class"] == "RoofSurface"
        assert matches[0]["primary_city_object_uid"] == "building_1_roof_1"
        assert matches[0]["matched_elements"] == [SECOND_BLOCK_FACE]

    finally:
        pkg.close()


def test_annotation_membership_is_split_into_two_blocks(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    pkg, _asset_part_id, _roof_class_id, annotation_id = build_tiny_package(db_path)

    try:
        blocks = pkg.elements_for_annotation(
            annotation_id=annotation_id,
            expand=True,
        )

        assert len(blocks) == 2

        block_starts = [block["block_start"] for block in blocks]
        assert block_starts == [0, DEFAULT_BLOCK_SIZE]

        all_faces = []
        for block in blocks:
            all_faces.extend(block["elements"])

        assert all_faces == TINY_FACES

    finally:
        pkg.close()


def test_standalone_object_answers_for_itself(tmp_path: Path) -> None:
    # An object with no relationship edges at all is still its own only
    # "part". Descendant expansion used to read a stored closure, so such an
    # object was invisible under the default include_descendants=True and its
    # annotations silently vanished from object-level retrieval — while
    # validation reported the package as fine.
    db_path = tmp_path / "test.usap.gpkg"

    pkg, asset_part_id, roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        loose_id = pkg.create_city_object(
            object_uid="loose_object",
            semantic_class_id=roof_class_id,
        )

        loose_annotation_id = pkg.create_annotation(
            annotation_uid="ann_loose",
            semantic_class_id=roof_class_id,
            primary_city_object_id=loose_id,
        )

        pkg.replace_annotation_membership(
            annotation_id=loose_annotation_id,
            asset_part_id=asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[7, 8, 9],
        )

        with_descendants = pkg.elements_for_city_object("loose_object", expand=True)
        without_descendants = pkg.elements_for_city_object(
            "loose_object",
            include_descendants=False,
            expand=True,
        )

        assert [b["elements"] for b in with_descendants] == [[7, 8, 9]]
        assert with_descendants == without_descendants

    finally:
        pkg.close()


def test_descendants_follow_containment_edges_only(tmp_path: Path) -> None:
    # The object graph is typed. 'adjacentTo' relates two objects without
    # making one part of the other, so the neighbour's elements must not be
    # reported as elements of building_1 — a type-blind traversal turns every
    # edge into containment and answers "this building's roof faces" with a
    # different building's.
    db_path = tmp_path / "test.usap.gpkg"

    pkg, asset_part_id, roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        neighbour_id = pkg.create_city_object(
            object_uid="building_2",
            semantic_class_id=roof_class_id,
        )

        neighbour_annotation_id = pkg.create_annotation(
            annotation_uid="ann_building_2",
            semantic_class_id=roof_class_id,
            primary_city_object_id=neighbour_id,
        )

        pkg.replace_annotation_membership(
            annotation_id=neighbour_annotation_id,
            asset_part_id=asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[500, 501],
        )

        pkg.link_city_objects(
            pkg.resolve_city_object("building_1"),
            neighbour_id,
            "adjacentTo",
            category="peer",
        )

        blocks = pkg.elements_for_city_object("building_1", expand=True)
        returned = {index for block in blocks for index in block["elements"]}

        # The roof (a boundedBy child) is in; the neighbour is not.
        assert 100 in returned
        assert 500 not in returned

        # Opting into the edge type reaches it, which is what makes the
        # default a policy rather than a limitation.
        followed = pkg.elements_for_city_object(
            "building_1",
            expand=True,
            relationship_types=("boundedBy", "adjacentTo"),
        )

        assert 500 in {index for block in followed for index in block["elements"]}

    finally:
        pkg.close()


def test_city_object_query_uses_usap_default_descendants(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    pkg, _asset_part_id, _roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        blocks = pkg.elements_for_city_object(
            object_uid="building_1",
            include_descendants=True,
            graph_name="usap_default",
            expand=True,
        )

        assert len(blocks) == 2

        all_faces = []
        for block in blocks:
            all_faces.extend(block["elements"])

        assert all_faces == TINY_FACES

    finally:
        pkg.close()


def test_city_object_query_finds_annotation_without_object_link(tmp_path: Path) -> None:
    # Hardening regression: an annotation that names a primary city object but
    # has no usap_annotation_object link row must still be returned. The query
    # used to match only via the link table, so such an annotation silently
    # vanished from elements_for_city_object.
    db_path = tmp_path / "test.usap.gpkg"

    pkg, asset_part_id, roof_class_id, _annotation_id = build_tiny_package(db_path)

    try:
        roof_id = pkg.resolve_city_object("building_1_roof_1")

        unlinked_id = pkg.create_annotation(
            annotation_uid="ann_unlinked_roof",
            semantic_class_id=roof_class_id,
            primary_city_object_id=roof_id,
            status="accepted",
            link_primary_object=False,
        )

        pkg.replace_annotation_membership(
            annotation_id=unlinked_id,
            asset_part_id=asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[7000, 7001],
        )

        blocks = pkg.elements_for_city_object(
            object_uid="building_1",
            include_descendants=True,
            graph_name="usap_default",
            expand=True,
        )

        annotation_ids = {block["annotation_id"] for block in blocks}
        assert unlinked_id in annotation_ids

        # The query tolerates the divergence, but the package is not healthy:
        # link_primary_object=False breaks the primary-object invariant, and
        # validation must say so rather than let it pass silently.
        report = pkg.validate_report()

        # Errors only: this fixture registers its asset part by hand without an
        # indexing_profile, which validation warns about separately and which
        # has nothing to do with the invariant under test.
        assert [issue.code for issue in report.errors] == [
            "ANNOTATION_PRIMARY_OBJECT_LINK_MISSING"
        ], [i.format() for i in report.issues]
        assert report.errors[0].details["annotation_id"] == unlinked_id

    finally:
        pkg.close()


def test_city_object_query_follows_only_representing_links(tmp_path: Path) -> None:
    # 'derivedFrom' says the claim was informed by an object, not that it
    # covers that object's geometry. Following every link type would report the
    # roof faces as elements of the survey object the roof claim came from.
    db_path = tmp_path / "test.usap.gpkg"

    pkg, _asset_part_id, _roof_class_id, annotation_id = build_tiny_package(db_path)

    try:
        survey_id = pkg.create_city_object(object_uid="survey_object_7")

        pkg.link_annotation_to_object(
            annotation_id=annotation_id,
            city_object_id=survey_id,
            relation_type="derivedFrom",
        )

        # include_descendants=False keeps this about link types alone.
        assert pkg.elements_for_city_object(
            "survey_object_7",
            include_descendants=False,
        ) == []

        followed = pkg.elements_for_city_object(
            "survey_object_7",
            include_descendants=False,
            link_types=("represents", "derivedFrom"),
        )

        assert {block["annotation_id"] for block in followed} == {annotation_id}

        # The object it does represent is unaffected, and still reachable with
        # link following switched off entirely (the primary-column path).
        for link_types in [("represents",), ()]:
            roof_blocks = pkg.elements_for_city_object(
                "building_1_roof_1",
                include_descendants=False,
                link_types=link_types,
            )

            assert {block["annotation_id"] for block in roof_blocks} == {annotation_id}

    finally:
        pkg.close()


def test_link_city_objects_is_idempotent(pkg: USAPPackage) -> None:
    # Re-linking an identical edge must return the existing relationship_id
    # instead of inserting a duplicate — CityGML re-imports in update mode
    # rely on this to keep the relationship graph stable.
    parent = pkg.create_city_object(object_uid="b1")
    child = pkg.create_city_object(object_uid="b1_roof")

    first = pkg.link_city_objects(
        parent,
        child,
        "boundedBy",
        category="containment",
        role="roof",
    )
    second = pkg.link_city_objects(
        parent,
        child,
        "boundedBy",
        category="containment",
        role="roof",
    )

    assert second == first

    count = pkg.conn.execute(
        "SELECT COUNT(*) AS n FROM usap_city_object_relationship"
    ).fetchone()["n"]

    assert count == 1

    # A variant edge (different role) is a different claim and must insert.
    third = pkg.link_city_objects(
        parent,
        child,
        "boundedBy",
        category="containment",
        role="wall",
    )

    assert third != first


def test_log_edit_writes_row(pkg: USAPPackage) -> None:
    # The edit log is the package's provenance trail; a custom operation
    # recorded through the public API must land in usap_edit_log.
    pkg.log_edit("custom_op", "usap_asset", 7, details_json='{"why": "test"}')

    row = pkg.conn.execute(
        "SELECT operation, target_table, target_id, details_json, created_at "
        "FROM usap_edit_log WHERE operation = 'custom_op'"
    ).fetchone()

    assert row is not None
    assert row["target_table"] == "usap_asset"
    assert row["target_id"] == 7
    assert row["details_json"] == '{"why": "test"}'
    assert row["created_at"] is not None


def test_create_failure_leaves_no_artifacts(tmp_path: Path) -> None:
    # A failed create must not leave a half-initialized package file (or an
    # open connection) behind, or a retry hits "Database already exists".
    bad_schema = tmp_path / "bad_schema.sql"
    bad_schema.write_text("CREATE TABLE broken (;", encoding="utf-8")

    db_path = tmp_path / "broken.usap.gpkg"

    with pytest.raises(sqlite3.OperationalError):
        USAPPackage.create(db_path, schema_path=bad_schema, overwrite=True)

    assert not db_path.exists()


def test_default_paths_work_from_any_cwd(tmp_path: Path, monkeypatch) -> None:
    # Default schema/vocabulary paths are repo-anchored: creating a package
    # and seeding the default vocabulary must not depend on the process CWD.
    monkeypatch.chdir(tmp_path)

    with USAPPackage.create(tmp_path / "cwd.usap.gpkg", overwrite=True) as pkg:
        vocab = seed_citygml_concepts(pkg)

        assert "Building" in vocab.by_name


def test_annotations_for_elements_survives_huge_selection(tmp_path: Path) -> None:
    # Must be the profile's real block size: hardcoding a smaller one would
    # spread the selection over fewer blocks than intended and quietly stop
    # exercising the chunking this test exists for.
    block_size = DEFAULT_BLOCK_SIZE

    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg, element_count=block_size * 2600)
        pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")

        hit_low = 0
        hit_high = block_size * 2000

        pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_big",
            asset_part_id=part,
            element_kind="face",
            element_indices=[hit_low, hit_high],
        )

        # One selected index in each of 2500 distinct blocks, so the IN clause
        # would need 2500 placeholders and the query must be chunked.
        selected = list(range(0, block_size * 2500, block_size))

        # Pin the connection's variable limit to the 999 of older SQLite
        # builds. Without this the test cannot fail: this build allows 250_000
        # variables, so an unchunked 2500-placeholder query succeeds and
        # deleting the chunking loop goes unnoticed.
        pkg.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)

        matches = pkg.annotations_for_elements(
            asset_part_id=part,
            element_kind="face",
            selected_indices=selected,
        )

        assert len(matches) == 1
        assert matches[0]["annotation_uid"] == "ann_big"
        # Hits come from different chunks and must be merged.
        assert matches[0]["matched_elements"] == [hit_low, hit_high]


def test_elements_for_city_object_survives_many_descendants(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        part = make_mesh_part(pkg)
        pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")

        with pkg.transaction():
            root_id = pkg.create_city_object(object_uid="root")

            child_ids = []

            for i in range(1000):
                child_id = pkg.create_city_object(object_uid=f"child_{i:04d}")

                pkg.link_city_objects(
                    root_id,
                    child_id,
                    "contains",
                    category="containment",
                )

                child_ids.append(child_id)

        annotation = pkg.annotate_elements(
            concept="Roof",
            annotation_uid="ann_multi",
            asset_part_id=part,
            element_kind="face",
            element_indices=[1, 2, 3],
            city_object_uid="child_0010",
        )

        # Link the same annotation to a second descendant: it is reachable
        # by two paths at once and its block must still be returned once.
        pkg.link_annotation_to_object(
            annotation_id=int(annotation["annotation_id"]),
            city_object_id=child_ids[990],
        )

        blocks = pkg.elements_for_city_object("root", expand=True)

        assert len(blocks) == 1
        assert blocks[0]["elements"] == [1, 2, 3]


def test_raw_write_then_sdk_write_both_persist(tmp_path: Path) -> None:
    db_path = tmp_path / "raw.usap.gpkg"

    pkg = USAPPackage.create(db_path, overwrite=True)

    # A raw write opens an implicit sqlite3 transaction. The next SDK write
    # must adopt and commit it instead of silently never committing.
    pkg.conn.execute(
        "INSERT INTO usap_asset (uri, asset_kind) VALUES ('raw.las', 'pointcloud')"
    )

    assert pkg.conn.in_transaction

    pkg.register_asset(uri="sdk.las", asset_kind="pointcloud")
    pkg.close()

    conn = sqlite3.connect(db_path)

    try:
        count = conn.execute("SELECT COUNT(*) FROM usap_asset").fetchone()[0]
    finally:
        conn.close()

    assert count == 2


def test_normalize_element_kind_is_strict() -> None:
    assert normalize_element_kind("vertex") == 3
    assert normalize_element_kind("features") == 4

    with pytest.raises(ValueError):
        normalize_element_kind(99)

    with pytest.raises(ValueError):
        normalize_element_kind("polygon")


def test_list_assets_and_parts(pkg: USAPPackage) -> None:
    # A UI must be able to enumerate what was registered, with part/element
    # counts, and drill into one asset's parts.
    mesh_id = pkg.register_asset(uri="mesh.ply", asset_kind="mesh")
    pkg.register_asset_part(mesh_id, "geometry/0", ELEMENT_KIND_FACE, 10)
    pkg.register_asset_part(mesh_id, "geometry/1", ELEMENT_KIND_FACE, 5)

    cloud_id = pkg.register_asset(uri="area.las", asset_kind="pointcloud")
    pkg.register_asset_part(cloud_id, "points/all", ELEMENT_KIND_POINT, 100)

    assets = pkg.list_assets()

    assert [a["uri"] for a in assets] == ["mesh.ply", "area.las"]
    by_uri = {a["uri"]: a for a in assets}
    assert by_uri["mesh.ply"]["part_count"] == 2
    assert by_uri["mesh.ply"]["element_count"] == 15
    assert by_uri["area.las"]["part_count"] == 1
    assert by_uri["area.las"]["element_count"] == 100

    # asset_kind filter
    assert [a["uri"] for a in pkg.list_assets(asset_kind="pointcloud")] == [
        "area.las"
    ]

    # parts of one asset only
    mesh_parts = pkg.list_asset_parts(asset_id=mesh_id)
    assert [p["part_path"] for p in mesh_parts] == ["geometry/0", "geometry/1"]
    assert all(p["element_kind"] == ELEMENT_KIND_FACE for p in mesh_parts)
    assert {p["asset_id"] for p in mesh_parts} == {mesh_id}

    # unfiltered lists every part across assets
    assert len(pkg.list_asset_parts()) == 3


def test_list_city_objects_and_children(pkg: USAPPackage) -> None:
    # A UI populates an object tree: list all objects, filter carriers, and
    # expand a node to its direct children.
    pkg.create_semantic_class(scheme="s", class_uri="s:Building", local_name="Building")
    pkg.create_semantic_class(scheme="s", class_uri="s:Roof", local_name="Roof")

    building = pkg.create_city_object(object_uid="building_1")
    roof = pkg.create_city_object(
        object_uid="building_1_roof_1", object_status="temporary"
    )
    wall = pkg.create_city_object(object_uid="building_1_wall_1")

    for child in (roof, wall):
        pkg.link_city_objects(
            building,
            child,
            "boundedBy",
            category="containment",
        )

    # all objects
    assert {o["object_uid"] for o in pkg.list_city_objects()} == {
        "building_1",
        "building_1_roof_1",
        "building_1_wall_1",
    }

    # carrier filter — the alignment hook
    carriers = pkg.list_city_objects(object_status="temporary")
    assert [o["object_uid"] for o in carriers] == ["building_1_roof_1"]

    # expand a node: direct children only (by id or by uid)
    children = pkg.list_city_objects(related_to="building_1")
    assert {o["object_uid"] for o in children} == {
        "building_1_roof_1",
        "building_1_wall_1",
    }
    assert pkg.list_city_objects(related_to=building) == children

    # a leaf has no children
    assert pkg.list_city_objects(related_to="building_1_roof_1") == []


def test_reregistering_an_asset_with_different_values_raises(pkg: USAPPackage) -> None:
    # Registration is idempotent on (uri, content_hash) so re-runs are cheap.
    # That is only sound while "already registered" means "as the same
    # thing": returning the existing id for a conflicting record hands the
    # caller a row describing something they did not register — the probe
    # that registered a mesh, then the same key as a point cloud, got a mesh
    # back and no error.
    first = pkg.register_asset(
        uri="area.ply",
        asset_kind="mesh",
        content_hash="sha256:" + "a1" * 32,
    )

    assert pkg.register_asset(
        uri="area.ply",
        asset_kind="mesh",
        content_hash="sha256:" + "a1" * 32,
    ) == first

    with pytest.raises(USAPError, match="already registered with different"):
        pkg.register_asset(
            uri="area.ply",
            asset_kind="pointcloud",
            content_hash="sha256:" + "a1" * 32,
        )

    # A genuinely new version of the file is a different content hash, and
    # that is a separate asset rather than a conflict.
    assert pkg.register_asset(
        uri="area.ply",
        asset_kind="mesh",
        content_hash="sha256:" + "b2" * 32,
    ) != first


def test_reregistering_a_part_with_a_different_count_raises(pkg: USAPPackage) -> None:
    # element_count is the index space every membership on the part is
    # validated against. Silently keeping the old count while the caller
    # believes the new one leaves annotations mis-scoped with no error
    # anywhere.
    asset_id = pkg.register_asset(uri="area.ply", asset_kind="mesh")

    part_id = pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=10,
    )

    assert pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=10,
    ) == part_id

    with pytest.raises(USAPError, match="already registered with different"):
        pkg.register_asset_part(
            asset_id=asset_id,
            part_path="geometry/0",
            element_kind=ELEMENT_KIND_FACE,
            element_count=999,
        )


def test_recreating_a_city_object_with_a_different_class_raises(
    pkg: USAPPackage,
) -> None:
    # create_city_object is idempotent on object_uid, which is what makes a
    # gml:id usable as the key: the CityGML owns uniqueness, USAP just anchors
    # to it. Same rule as register_asset, though: "already there" has to mean
    # "there as the same thing".
    #
    # The way this goes wrong in practice is a class mismatch, and it arises
    # naturally — the annotation's concept (EnergyRoof) is usually not the
    # object's CityGML class (RoofSurface). Passing the former here used to
    # return the existing row and drop it with no error.
    roof = pkg.create_semantic_class(
        scheme="citygml", class_uri="c:RoofSurface", local_name="RoofSurface"
    )
    energy_roof = pkg.create_semantic_class(
        scheme="ade", class_uri="a:EnergyRoof", local_name="EnergyRoof"
    )

    first = pkg.create_city_object(
        object_uid="building_1_roof_1",
        semantic_class_id=roof,
        gml_id="building_1_roof_1",
    )

    # Same values: still idempotent.
    assert pkg.create_city_object(
        object_uid="building_1_roof_1",
        semantic_class_id=roof,
        gml_id="building_1_roof_1",
    ) == first

    with pytest.raises(USAPError, match="already exists with different"):
        pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=energy_roof,
        )

    with pytest.raises(USAPError, match="already exists with different"):
        pkg.create_city_object(
            object_uid="building_1_roof_1",
            gml_id="some_other_gml_id",
        )

    # Only supplied fields are compared, so the bare "give me the id" call
    # still works against a fully populated row — that idiom is how carrier
    # objects get looked up.
    assert pkg.create_city_object(object_uid="building_1_roof_1") == first

    # And nothing was written by the rejected calls.
    stored = pkg.conn.execute(
        "SELECT semantic_class_id, gml_id FROM usap_city_object "
        "WHERE object_uid = ?",
        ("building_1_roof_1",),
    ).fetchone()

    assert stored["semantic_class_id"] == roof
    assert stored["gml_id"] == "building_1_roof_1"


def test_annotation_domain_values_are_refused(pkg: USAPPackage, mesh_part: int) -> None:
    # These three all used to be stored and to validate clean. Each breaks a
    # reader: an unknown status drops out of every status filter, a
    # confidence of 7.5 cannot be compared with any other, and attributes
    # that are not JSON cannot be read back at all.
    class_id = pkg.create_semantic_class(
        scheme="s", class_uri="s:Roof", local_name="Roof"
    )

    with pytest.raises(USAPError, match="Unknown annotation status"):
        pkg.create_annotation(
            annotation_uid="ann_bad_status",
            semantic_class_id=class_id,
            status="probably",
        )

    with pytest.raises(USAPError, match="outside"):
        pkg.create_annotation(
            annotation_uid="ann_bad_confidence",
            semantic_class_id=class_id,
            confidence=7.5,
        )

    with pytest.raises(USAPError, match="not valid JSON"):
        pkg.create_annotation(
            annotation_uid="ann_bad_json",
            semantic_class_id=class_id,
            attributes_json="{not json",
        )

    # The same guards apply to edits, not just creation.
    annotation_id = pkg.create_annotation(
        annotation_uid="ann_ok",
        semantic_class_id=class_id,
    )

    with pytest.raises(USAPError, match="Unknown annotation status"):
        pkg.update_annotation(annotation_id, status="probably")

    assert pkg.get_annotation(annotation_id)["status"] == "accepted"


def test_open_refuses_a_foreign_sqlite_file(tmp_path: Path) -> None:
    # An arbitrary SQLite file used to open cleanly and fail later with
    # "no such table: usap_asset" from whichever call ran first — the failure
    # named a symptom, not the cause.
    foreign = tmp_path / "not_usap.gpkg"

    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE something (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(USAPError, match="Not a USAP package"):
        USAPPackage.open(foreign)


def test_open_refuses_an_unsupported_profile_version(tmp_path: Path) -> None:
    # There is no migration path yet, so a package written by a future
    # profile must be refused rather than read with this build's assumptions.
    db_path = tmp_path / "future.usap.gpkg"

    with make_pkg(tmp_path, name="future.usap.gpkg") as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                "UPDATE usap_profile SET profile_version = '9.9.9'"
            )

    with pytest.raises(USAPError, match="Unsupported USAP profile version"):
        USAPPackage.open(db_path)

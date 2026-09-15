from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import make_pkg
from usap import (
    ELEMENT_KIND_FACE,
    SyntheticConfig,
    USAPError,
    USAPPackage,
    create_synthetic_package,
    validate_connection,
)
from usap._util import canonical_hash, sha256_file
from usap.validation import verify_assets


def _create_small_valid_package(db_path: Path):
    return create_synthetic_package(
        db_path,
        config=SyntheticConfig(
            building_count=5,
            roof_faces_per_building=20,
            wall_faces_per_building=30,
            ground_faces_per_building=10,
        ),
        overwrite=True,
    )


def _codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def test_validation_report_is_ok_for_valid_synthetic_package(tmp_path: Path) -> None:
    db_path = tmp_path / "valid.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        report = pkg.validate_report()

        assert report.is_ok, [issue.format() for issue in report.issues]
        assert report.issues == []


def test_validation_catches_corrupt_membership_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "corrupt_payload.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_membership_block
                SET payload = X'000102'
                WHERE membership_block_id = 1
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "CORRUPT_MEMBERSHIP_PAYLOAD" in _codes(report)


def test_validation_catches_primary_object_without_represents_link(
    tmp_path: Path,
) -> None:
    # The primary city object is stored both as a column and as a 'represents'
    # link. A package where those disagree answers city-object queries with
    # annotations that no longer belong to the object, so it is not valid.
    db_path = tmp_path / "stale_link.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        annotation = pkg.conn.execute(
            """
            SELECT annotation_id, primary_city_object_id
            FROM usap_annotation
            WHERE primary_city_object_id IS NOT NULL
            LIMIT 1
            """
        ).fetchone()

        assert annotation is not None

        with pkg.transaction():
            pkg.conn.execute(
                """
                DELETE FROM usap_annotation_object
                WHERE annotation_id = ?
                  AND city_object_id = ?
                  AND relation_type = 'represents'
                """,
                (
                    annotation["annotation_id"],
                    annotation["primary_city_object_id"],
                ),
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "ANNOTATION_PRIMARY_OBJECT_LINK_MISSING" in _codes(report)


def test_validation_catches_membership_count_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_count.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_membership_block
                SET element_count = element_count + 1
                WHERE membership_block_id = 1
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "MEMBERSHIP_COUNT_MISMATCH" in _codes(report)


def test_validation_catches_containment_cycle(tmp_path: Path) -> None:
    # "An object and its parts" only means something if containment is
    # acyclic: on a cycle every object is its own part, so a package that
    # states one is stating something it cannot mean. Descendant queries do
    # not hang (the recursive CTE deduplicates), which is exactly why this
    # has to be reported rather than left to fail loudly at query time.
    db_path = tmp_path / "cycle.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        parent = pkg.resolve_city_object("building_000000")
        child = pkg.resolve_city_object("building_000000_roof")

        pkg.link_city_objects(
            child,
            parent,
            "contains",
            category="containment",
        )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "CITY_OBJECT_GRAPH_CYCLE" in _codes(report)


def test_non_containment_cycle_is_not_an_error(tmp_path: Path) -> None:
    # The graph is typed: two objects can perfectly well be adjacent to each
    # other. Only containment edges are checked for cycles.
    db_path = tmp_path / "adjacency_cycle.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        first = pkg.resolve_city_object("building_000000")
        second = pkg.resolve_city_object("building_000000_roof")

        for parent, child in [(first, second), (second, first)]:
            pkg.link_city_objects(
                parent,
                child,
                "adjacentTo",
                category="peer",
            )

        report = pkg.validate_report()

        assert "CITY_OBJECT_GRAPH_CYCLE" not in _codes(report)
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_validation_catches_missing_semantic_class_closure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing_class_closure.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                DELETE FROM usap_semantic_class_closure
                WHERE ancestor_class_id = 1
                  AND descendant_class_id = 1
                  AND depth = 0
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "MISSING_SEMANTIC_CLASS_SELF_CLOSURE" in _codes(report)


def test_validation_warns_on_duplicate_relationship_edges(pkg: USAPPackage) -> None:
    # link_city_objects dedups identical edges, but packages written before
    # that guard (or via raw SQL) may hold duplicates. Validation must
    # surface them — as a warning, not an error, because such packages must
    # keep opening and updating.
    parent = pkg.create_city_object(object_uid="b1")
    child = pkg.create_city_object(object_uid="b1_roof")

    type_id = pkg.register_relationship_type(
        "boundary",
        code_space="http://www.opengis.net/citygml/3.0",
        category="containment",
    )

    pkg.link_city_objects(parent, child, type_id, role="roof")

    # Simulate a duplicate written behind the API's back.
    with pkg.transaction():
        pkg.conn.execute(
            """
            INSERT INTO usap_city_object_relationship (
                graph_name, from_city_object_id, to_city_object_id,
                relationship_type_id, role
            )
            VALUES ('usap_default', ?, ?, ?, 'roof')
            """,
            (parent, child, type_id),
        )

    report = pkg.validate_report()
    duplicates = [
        issue for issue in report.issues
        if issue.code == "DUPLICATE_RELATIONSHIP_EDGE"
    ]

    assert len(duplicates) == 1
    assert duplicates[0].severity == "warning"
    assert report.is_ok  # a warning must not make the package invalid

    # The report joins the type back to a readable name: the stored column is
    # an id, and a report printing that would be useless to a human.
    assert duplicates[0].details["relationship_type"] == "boundary"
    assert duplicates[0].details["from_city_object_id"] == parent


def test_validation_catches_unsupported_encoding(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_encoding.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_membership_block
                SET encoding = 'unknown-encoding'
                WHERE membership_block_id = 1
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "UNSUPPORTED_MEMBERSHIP_ENCODING" in _codes(report)

def test_validate_connection_accepts_plain_connection(tmp_path: Path) -> None:
    # Validation must work on a bare sqlite3.Connection and restore the
    # caller's row factory, not hijack it.
    db_path = tmp_path / "plain.usap.gpkg"

    with make_pkg(tmp_path, "plain.usap.gpkg"):
        pass

    conn = sqlite3.connect(db_path)

    try:
        report = validate_connection(conn)

        assert report.is_ok, [issue.format() for issue in report.issues]
        assert conn.row_factory is None
    finally:
        conn.close()


def test_basic_level_skips_payload_decoding(tmp_path: Path) -> None:
    # 'basic' exists so a package with millions of membership blocks can be
    # checked without reading every blob off disk. The proof that it really
    # skips them: a corrupt payload that 'deep' reports must go unnoticed,
    # while the structural checks still run.
    db_path = tmp_path / "levels.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_membership_block
                SET payload = X'000102'
                WHERE membership_block_id = 1
                """
            )

        assert "CORRUPT_MEMBERSHIP_PAYLOAD" in _codes(pkg.validate_report())
        assert pkg.validate_report(level="basic").is_ok


def test_unknown_validation_level_is_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "level_name.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pytest.raises(USAPError, match="Unknown validation level"):
            pkg.validate_report(level="thorough")


def test_external_level_detects_changed_asset(tmp_path: Path) -> None:
    # An annotation is bound to one immutable version of a file by element
    # index. If the file changed, those indices may now name different
    # elements and nothing inside the package can tell — only re-hashing can,
    # which is why it is a level of its own rather than always-on.
    asset_path = tmp_path / "cloud.las"
    asset_path.write_bytes(b"original bytes")

    with make_pkg(tmp_path) as pkg:
        pkg.register_asset(
            uri=str(asset_path),
            asset_kind="pointcloud",
            content_hash=sha256_file(asset_path),
        )

        assert pkg.validate_report(level="external").is_ok

        asset_path.write_bytes(b"different bytes entirely")

        report = pkg.validate_report(level="external")

        assert not report.is_ok
        assert "ASSET_FILE_CHANGED" in _codes(report)

        # Nothing inside the database changed, so the cheaper levels cannot
        # and must not claim to notice.
        assert pkg.validate_report().is_ok

        asset_path.unlink()

        assert "ASSET_FILE_MISSING" in _codes(pkg.validate_report(level="external"))


def test_relative_asset_uri_resolves_against_the_package(tmp_path: Path) -> None:
    # A relative uri is relative to the package that makes the reference, not
    # to whatever directory the process happens to be in -- the rule glTF uses
    # for external buffers and 3D Tiles for tileset content. Without it a
    # package and its assets cannot be moved, and "does this file still match"
    # depends on the caller's cwd.
    import os
    import shutil

    project = tmp_path / "project"
    project.mkdir()
    (project / "area.las").write_bytes(b"payload")

    with USAPPackage.create(project / "p.usap.gpkg", overwrite=True) as pkg:
        pkg.register_asset(
            uri="area.las",                       # bare filename, beside the package
            asset_kind="pointcloud",
            content_hash=canonical_hash(project / "area.las"),
        )
        assert [r["status"] for r in verify_assets(pkg.conn)] == ["ok"]

    # Moved as a unit, from an unrelated working directory: still ok.
    moved = tmp_path / "somewhere" / "else"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(project), str(moved))

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with USAPPackage.open(moved / "p.usap.gpkg") as pkg:
            assert [r["status"] for r in verify_assets(pkg.conn)] == ["ok"]

            # And a real change is still detected through the same path.
            (moved / "area.las").write_bytes(b"different payload")
            assert [r["status"] for r in verify_assets(pkg.conn)] == ["changed"]
    finally:
        os.chdir(cwd)


def test_verify_assets_reports_unhashed_assets(tmp_path: Path) -> None:
    # compute_hash=False is a legitimate choice for a 10 GB file, but it
    # trades away change detection; that has to be visible, not implied.
    asset_path = tmp_path / "big.las"
    asset_path.write_bytes(b"payload")

    with make_pkg(tmp_path) as pkg:
        pkg.register_asset(uri=str(asset_path), asset_kind="pointcloud")

        assert [item["status"] for item in verify_assets(pkg.conn)] == ["unhashed"]

        report = pkg.validate_report(level="external")

        assert report.is_ok
        assert "ASSET_NOT_HASHED" in _codes(report)


# ---------------------------------------------------------------------------
# file:// asset uris
# ---------------------------------------------------------------------------

def test_verify_assets_understands_the_file_scheme(tmp_path: Path) -> None:
    # A stored uri is a path, not a URL -- but writers reach for file:// anyway,
    # and Path("file://area.las") is Path("file:/area.las"): a *relative* path
    # that resolves under the package directory and reports missing forever.
    # Accepting the spelling repairs every package already written that way.
    project = tmp_path / "project"
    project.mkdir()
    (project / "area.las").write_bytes(b"payload")

    with USAPPackage.create(project / "p.usap.gpkg", overwrite=True) as pkg:
        pkg.register_asset(
            uri="file://area.las",
            asset_kind="pointcloud",
            content_hash=canonical_hash(project / "area.las"),
        )

        # Stored exactly as given: the row's identity is (uri, content_hash),
        # so normalising on the way in would make a re-registration by the same
        # writer miss this row and insert a duplicate asset.
        assert pkg.conn.execute(
            "SELECT uri FROM usap_asset"
        ).fetchone()["uri"] == "file://area.las"

        assert [r["status"] for r in verify_assets(pkg.conn)] == ["ok"]


def test_verify_assets_understands_absolute_file_uris(tmp_path: Path) -> None:
    asset_path = tmp_path / "area.las"
    asset_path.write_bytes(b"payload")

    with make_pkg(tmp_path) as pkg:
        pkg.register_asset(
            uri=asset_path.as_uri(),          # file:///... with a real path
            asset_kind="pointcloud",
            content_hash=canonical_hash(asset_path),
        )

        assert [r["status"] for r in verify_assets(pkg.conn)] == ["ok"]


def test_register_asset_refuses_a_scheme_usap_cannot_resolve(tmp_path: Path) -> None:
    # http://, s3://, ... are not paths, and nothing in USAP fetches them: such
    # a package would verify as "missing" for the life of the file. Refuse at
    # registration, while the writer is still there to fix it.
    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="'https' scheme"):
            pkg.register_asset(
                uri="https://example.com/area.las", asset_kind="pointcloud"
            )

        # A Windows path has no "//" and is unaffected.
        pkg.register_asset(uri=r"C:\data\area.las", asset_kind="pointcloud")


def test_the_file_scheme_is_recognised_whatever_its_case(tmp_path: Path) -> None:
    # The registration guard lowercases the scheme before comparing, so FILE://
    # is accepted as the file scheme. Resolution has to agree, or such a uri is
    # stored happily and then reports missing for the life of the package --
    # the exact failure understanding file:// exists to end.
    project = tmp_path / "project"
    project.mkdir()
    (project / "area.las").write_bytes(b"payload")

    with USAPPackage.create(project / "p.usap.gpkg", overwrite=True) as pkg:
        pkg.register_asset(
            uri="FILE://area.las",
            asset_kind="pointcloud",
            content_hash=canonical_hash(project / "area.las"),
        )

        assert [r["status"] for r in verify_assets(pkg.conn)] == ["ok"]


def test_update_asset_refuses_a_scheme_usap_cannot_resolve(tmp_path: Path) -> None:
    # The same guard as registration, on the other door into the column.
    # update_asset is what the asset-identity guideline tells integrators to
    # call once a hash match tells them where the file really is, so a uri
    # refused at registration must not walk back in through it.
    with make_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="area.las", asset_kind="pointcloud")

        with pytest.raises(USAPError, match="'s3' scheme"):
            pkg.update_asset(asset_id, uri="s3://bucket/area.las")

        assert pkg.conn.execute(
            "SELECT uri FROM usap_asset WHERE asset_id = ?", (asset_id,)
        ).fetchone()["uri"] == "area.las"

        # A repair to a real path is still a repair.
        pkg.update_asset(asset_id, uri="moved/area.las")


# ---------------------------------------------------------------------------
# City object identity: a gml_id names one object
# ---------------------------------------------------------------------------

def _city_object(pkg, object_uid: str, gml_id: str | None) -> int:
    return pkg.create_city_object(object_uid=object_uid, gml_id=gml_id)


def test_two_objects_sharing_a_gml_id_is_an_error(tmp_path: Path) -> None:
    # resolve_city_object matches on object_uid OR gml_id, so a second row
    # claiming the same gml_id makes every reference to that value raise
    # USAPAmbiguityError -- and nothing in the schema prevents it, because a
    # row is allowed not to have a gml_id at all.
    #
    # The way in is a carrier minted under a name of the writer's own carrying
    # someone else's gml_id: the CityGML import matches carriers by object_uid,
    # so it creates a sibling rather than aligning the carrier.
    with make_pkg(tmp_path) as pkg:
        _city_object(pkg, "matera_roof_7", "w1")
        _city_object(pkg, "w1", "w1")

        report = pkg.validate_report()
        duplicates = [i for i in report.issues if i.code == "DUPLICATE_GML_ID"]

        assert report.is_ok is False
        assert len(duplicates) == 1
        assert duplicates[0].severity == "error"

        # The message has to name both, since choosing between them is the
        # writer's job and they are what the writer has to look at.
        assert duplicates[0].details["object_uids"] == ["matera_roof_7", "w1"]
        assert duplicates[0].details["gml_id"] == "w1"

        # Named at basic level too: this is pure SQL, and a caller who asked
        # for the cheap check still wants to know its identities are broken.
        assert any(
            i.code == "DUPLICATE_GML_ID"
            for i in pkg.validate_report(level="basic").issues
        )


def test_distinct_gml_ids_and_absent_ones_are_fine(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        _city_object(pkg, "a", "gml_a")
        _city_object(pkg, "b", "gml_b")

        # Any number of objects may have no gml_id: "unknown" is not a shared
        # identity, and "" is normalised to NULL on the way in.
        _city_object(pkg, "c", None)
        _city_object(pkg, "d", None)
        _city_object(pkg, "e", "")

        report = pkg.validate_report()
        assert [i for i in report.issues if i.code == "DUPLICATE_GML_ID"] == []


def test_empty_gml_ids_written_by_an_older_build_are_not_duplicates(
    tmp_path: Path,
) -> None:
    # create_city_object normalises "" to NULL, but rows written before it did
    # may still hold one. Two of those mean "neither knows its gml:id", not
    # "both are the same object", so they must not be reported as sharing one.
    with make_pkg(tmp_path) as pkg:
        _city_object(pkg, "a", None)
        _city_object(pkg, "b", None)

        with pkg.transaction():
            pkg.conn.execute("UPDATE usap_city_object SET gml_id = ''")

        report = pkg.validate_report()
        assert [i for i in report.issues if i.code == "DUPLICATE_GML_ID"] == []


# ---------------------------------------------------------------------------
# Asset identity: one uri, one row -- unless the rows are two versions
# ---------------------------------------------------------------------------

def _unhashed_sibling(pkg, uri: str, asset_kind: str = "mesh") -> int:
    """
    Write the shape register_asset used to produce, which it no longer can.

    Raw SQL on purpose: the fixed registrar fills the hash or returns the
    existing row, so the only way to build a package holding this is to write
    it the way an older build did.
    """
    with pkg.transaction():
        cur = pkg.conn.execute(
            "INSERT INTO usap_asset (uri, asset_kind, content_hash) "
            "VALUES (?, ?, NULL)",
            (uri, asset_kind),
        )

    return int(cur.lastrowid)


def test_a_uri_split_across_a_hashed_and_an_unhashed_row_is_a_warning(
    tmp_path: Path,
) -> None:
    # Two rows differing only by a missing hash are one file recorded twice.
    # resolve_asset raises USAPAmbiguityError for the uri from then on, and
    # each row takes its own parts -- so annotations on one are invisible to a
    # reverse query against the other, with every other check still passing.
    with make_pkg(tmp_path) as pkg:
        hashed = pkg.register_asset(
            uri="city.obj",
            asset_kind="mesh",
            content_hash="sha256:" + "a1" * 32,
        )
        orphan = _unhashed_sibling(pkg, "city.obj")

        report = pkg.validate_report()
        duplicates = [
            i for i in report.issues if i.code == "DUPLICATE_ASSET_URI"
        ]

        assert len(duplicates) == 1
        assert duplicates[0].severity == "warning"

        # A warning, so the package still reads as ok overall -- it is
        # recoverable by re-registering with the hash.
        assert report.is_ok is True

        assert duplicates[0].details["uri"] == "city.obj"
        assert duplicates[0].details["asset_ids"] == sorted([hashed, orphan])
        assert duplicates[0].details["unhashed_asset_ids"] == [orphan]

        # Pure SQL, so it is named at basic level too.
        assert any(
            i.code == "DUPLICATE_ASSET_URI"
            for i in pkg.validate_report(level="basic").issues
        )


def test_one_uri_at_two_hashes_is_two_versions_and_is_not_reported(
    tmp_path: Path,
) -> None:
    # The legal shape, and the reason this check cannot simply count rows per
    # uri: usap_asset is unique on (uri, content_hash) precisely so the old
    # file and the new one stay apart, each keeping the annotations indexed
    # against its own geometry.
    with make_pkg(tmp_path) as pkg:
        pkg.register_asset(
            uri="city.obj",
            asset_kind="mesh",
            content_hash="sha256:" + "a1" * 32,
        )
        pkg.register_asset(
            uri="city.obj",
            asset_kind="mesh",
            content_hash="sha256:" + "b2" * 32,
        )

        report = pkg.validate_report()
        assert [
            i for i in report.issues if i.code == "DUPLICATE_ASSET_URI"
        ] == []


def test_a_single_unhashed_asset_is_not_a_duplicate(tmp_path: Path) -> None:
    # One row with no hash is the ordinary compute_hash=False registration.
    # verify_assets reports it as `unhashed`; it is not an identity problem.
    with make_pkg(tmp_path) as pkg:
        pkg.register_asset(uri="city.obj", asset_kind="mesh")
        pkg.register_asset(uri="other.obj", asset_kind="mesh")

        report = pkg.validate_report()
        assert [
            i for i in report.issues if i.code == "DUPLICATE_ASSET_URI"
        ] == []


# ---------------------------------------------------------------------------
# Re-registration: omitting a field is not a claim that it should be NULL
# ---------------------------------------------------------------------------

def test_re_registering_an_asset_may_omit_fields(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        first = pkg.register_asset(
            uri="area.las",
            asset_kind="pointcloud",
            media_type="application/vnd.las",
            metadata_json='{"survey": 3}',
        )

        # The "give me the id" re-registration: omitted fields are not compared.
        assert pkg.register_asset(uri="area.las", asset_kind="pointcloud") == first

        # A field that is supplied and differs is still a conflict.
        with pytest.raises(USAPError, match="different"):
            pkg.register_asset(
                uri="area.las", asset_kind="pointcloud", media_type="text/plain"
            )


def test_re_registering_an_asset_part_may_omit_fields(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        asset_id = pkg.register_asset(uri="mesh.ply", asset_kind="mesh")

        first = pkg.register_asset_part(
            asset_id, "g/0", "face", 100,
            minx=0.0, maxx=10.0,
            indexing_profile="t:v1",
        )

        assert pkg.register_asset_part(asset_id, "g/0", "face", 100) == first

        # element_count is always compared: it is the index space every
        # existing membership on this part was validated against.
        with pytest.raises(USAPError, match="different"):
            pkg.register_asset_part(asset_id, "g/0", "face", 200)

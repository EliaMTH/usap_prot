"""
The API against real files.

Every other module in this suite builds its own input: a 2-triangle mesh, a
10-point LAS, a 7-file XSD subset. That is deliberate -- the suite stays
hermetic and fast -- but it means no test reaches the paths only real data
reaches: block splitting past DEFAULT_BLOCK_SIZE, a 22-XSD vocabulary with a
real substitutionGroup hierarchy, a reverse query over a million selected
indices, two assets covering the same buildings.

    pytest                                  # these all skip
    pytest --realdata-dir=data              # these run
    pytest --realdata-dir=data -s -k timing # N3's numbers

Several checks below are unreachable with a single asset: the filter, the
mismatch rule and the visibility rule all return the same answer for a working
and a broken implementation when there is only one thing to confuse. That is
how the list_annotations asset filter (G13) went untested for so long.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from usap import (
    USAPAmbiguityError,
    USAPPackage,
    load_ontology,
    load_vocabulary_folder,
    register_mesh_asset,
)
from usap.constants import DEFAULT_BLOCK_SIZE
from usap.validation import verify_assets

from conftest import REAL_PACKAGE_NAME

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parts_by_id(pkg: USAPPackage) -> dict[int, dict]:
    return {p["asset_part_id"]: p for p in pkg.list_asset_parts()}


def _mesh_part(pkg: USAPPackage) -> dict:
    return next(p for p in pkg.list_asset_parts() if p["element_kind"] == 1)


def _point_part(pkg: USAPPackage) -> dict:
    return next(p for p in pkg.list_asset_parts() if p["element_kind"] == 2)


def _cross_asset_annotation(pkg: USAPPackage) -> dict:
    """
    An annotation carrying membership in both assets.

    The point of the fixture: USAP's headline claim is that one statement can
    be evidenced in a point cloud and a mesh at once. Nothing below can be
    tested without one.
    """
    parts = {p["asset_part_id"]: p["asset_id"] for p in pkg.list_asset_parts()}

    for annotation in pkg.list_annotations(limit=4000):
        assets = {
            parts[b["asset_part_id"]]
            for b in pkg.elements_for_annotation(
                annotation["annotation_id"], expand=False
            )
        }

        if len(assets) > 1:
            return annotation

    pytest.skip("no annotation spans two assets in this data")


def _read_obj_independently(path: Path) -> tuple[int, tuple[float, ...]]:
    """
    A second opinion on the same file, deliberately not trimesh.

    Counts `f` records in file order and takes bounds over *referenced*
    vertices only. The second part matters: trimesh drops unreferenced
    vertices and remaps, so min/max over every `v` line would disagree with it
    for a reason that says nothing about face numbering.
    """
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    referenced: set[int] = set()
    faces = 0

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("v "):
                _, x, y, z, *_ = line.split()
                xs.append(float(x))
                ys.append(float(y))
                zs.append(float(z))
            elif line.startswith("f "):
                faces += 1
                for token in line.split()[1:]:
                    raw = int(token.split("/")[0])
                    referenced.add(raw - 1 if raw > 0 else len(xs) + raw)

    used = sorted(referenced)

    bounds = (
        min(xs[i] for i in used),
        min(ys[i] for i in used),
        min(zs[i] for i in used),
        max(xs[i] for i in used),
        max(ys[i] for i in used),
        max(zs[i] for i in used),
    )

    return faces, bounds


# ---------------------------------------------------------------------------
# N1 -- loader agreement (G9), the one mistake nothing detects afterwards
# ---------------------------------------------------------------------------


def test_n1_two_independent_readers_agree_on_the_face_count(
    pkg: USAPPackage, real_mesh_path: Path
) -> None:
    """
    G9 in a single assertion.

    In the rest of the suite the code that writes the mesh also counts its
    faces, so the two agree by construction and this can never fail. Here one
    count comes from trimesh (via register_mesh_asset) and the other from a
    reader that only knows the file format -- which is the actual integration
    risk: if the application's loader numbers faces differently from whatever
    counted them at registration, every membership points at different
    geometry and no validation level notices.
    """
    result = register_mesh_asset(
        pkg,
        real_mesh_path,
        representation_name="real",
        compute_hash=False,
    )

    stored = next(
        p
        for p in pkg.list_asset_parts()
        if p["asset_part_id"] == result.primary_asset_part_id
    )

    independent_faces, independent_bounds = _read_obj_independently(real_mesh_path)

    assert stored["element_count"] == independent_faces

    stored_bounds = (
        stored["minx"],
        stored["miny"],
        stored["minz"],
        stored["maxx"],
        stored["maxy"],
        stored["maxz"],
    )

    assert stored_bounds == pytest.approx(independent_bounds, abs=1e-4)


def test_n1_both_registration_paths_record_the_same_indexing_profile(
    pkg: USAPPackage, real_mesh_path: Path
) -> None:
    """
    The generic path is what an application must use (HANDOFF.md forbids
    register_mesh_asset there), so the two must not disagree about the
    convention -- and re-registering under a different one must raise rather
    than silently repointing memberships.
    """
    adapter = register_mesh_asset(
        pkg, real_mesh_path, representation_name="real", compute_hash=False
    )
    stored = next(
        p
        for p in pkg.list_asset_parts()
        if p["asset_part_id"] == adapter.primary_asset_part_id
    )

    assert stored["indexing_profile"] == "usap:obj-face-record-order-v1"

    with pytest.raises(Exception) as excinfo:
        pkg.register_asset_part(
            asset_id=stored["asset_id"],
            part_path=stored["part_path"],
            element_kind=stored["element_kind"],
            element_count=stored["element_count"],
            indexing_profile="theirapp:mesh-face-order-v1",
        )

    assert "indexing_profile" in str(excinfo.value)


# ---------------------------------------------------------------------------
# N2 -- the real vocabulary, the startup path an application takes
# ---------------------------------------------------------------------------


def test_n2_vocabulary_folder_loads_the_real_schemas(
    pkg: USAPPackage, real_vocabulary_dir: Path
) -> None:
    started = time.perf_counter()
    load_vocabulary_folder(pkg, real_vocabulary_dir, scheme_version="3.0")
    elapsed = time.perf_counter() - started

    concepts = pkg.list_accepted_concepts()

    print(
        f"\n  vocabulary folder: {len(concepts):,} concepts "
        f"from {len(list(real_vocabulary_dir.iterdir()))} files in {elapsed:.2f}s"
    )

    assert len(concepts) > 200
    assert any(c["is_ade"] for c in concepts), "no ADE concepts were seeded"


def test_n2_the_substitution_group_hierarchy_resolves(
    pkg: USAPPackage, real_vocabulary_dir: Path
) -> None:
    """
    The XSDs are the only artifact carrying substitutionGroup, so this chain
    exists in no other source -- not the OWL rendering, not the data
    dictionary. Without it `include_subclasses` has nothing to walk.
    """
    load_vocabulary_folder(pkg, real_vocabulary_dir, scheme_version="3.0")

    chain: list[str] = []
    row = pkg.conn.execute(
        "SELECT semantic_class_id, local_name, parent_class_id "
        "FROM usap_semantic_class WHERE local_name = 'RoofSurface'"
    ).fetchone()

    while row is not None:
        chain.append(row["local_name"])

        if row["parent_class_id"] is None:
            break

        row = pkg.conn.execute(
            "SELECT semantic_class_id, local_name, parent_class_id "
            "FROM usap_semantic_class WHERE semantic_class_id = ?",
            (row["parent_class_id"],),
        ).fetchone()

    print(f"\n  RoofSurface: {' -> '.join(chain)}")

    assert "AbstractThematicSurface" in chain
    assert "AbstractCityObject" in chain
    assert len(chain) >= 5


@pytest.mark.parametrize("name", ["uish_ontology_building.owl", "uo-full.owl"])
def test_n2_real_rdfxml_ontologies_load(
    pkg: USAPPackage, realdata_dir: Path, name: str
) -> None:
    """
    The .owl half of a vocabulary folder, against files that were not written
    for this test. uo-full.owl is the OBO Units of Measurement ontology -- not
    an ADE, so it is a size check on the RDF/XML reader rather than a semantic
    one.

    These live at the repo root rather than in --realdata-dir, but they are
    still real-data checks, so they stay behind the same flag: `pytest` alone
    must produce exactly the count it did before this module existed.
    """
    path = next(
        (p for p in (realdata_dir / name,
                     Path(__file__).resolve().parents[1] / name) if p.exists()),
        None,
    )

    if path is None:
        pytest.skip(f"{name} is not in the checkout")

    started = time.perf_counter()
    result = load_ontology(pkg, path, scheme=name.split(".")[0])
    elapsed = time.perf_counter() - started

    print(
        f"\n  {name}: {len(result.concepts)} concepts, "
        f"{len(result.relationship_types)} relationship types, "
        f"{path.stat().st_size / 1024:.0f} KB in {elapsed:.2f}s"
    )

    assert len(result.concepts) > 0


# ---------------------------------------------------------------------------
# N3 -- timings. Numbers, never assertions.
# ---------------------------------------------------------------------------


def test_n3_timings(real_package: USAPPackage, real_package_dir: Path) -> None:
    pkg = real_package
    point_part = _point_part(pkg)
    mesh = _mesh_part(pkg)

    print("\n  --- N3 timings ---")

    for n in (10_000, 100_000, 1_000_000):
        if n > point_part["element_count"]:
            continue

        step = max(1, point_part["element_count"] // n)
        selected = list(range(0, point_part["element_count"], step))[:n]

        started = time.perf_counter()
        hits = pkg.annotations_for_elements(
            asset_part_id=point_part["asset_part_id"],
            element_kind="point",
            selected_indices=selected,
        )
        elapsed = time.perf_counter() - started

        print(
            f"  reverse query {n:>9,} indices -> {len(hits):>6,} annotations"
            f"  {elapsed:7.3f}s"
        )

    started = time.perf_counter()
    pkg.validate_report(level="deep")
    print(f"  validate_report(deep)              {time.perf_counter() - started:7.3f}s")

    size = (real_package_dir / REAL_PACKAGE_NAME).stat().st_size

    print(
        f"  package size {size / (1 << 20):.1f} MB for "
        f"{len(pkg.list_annotations()):,} annotations, "
        f"{mesh['element_count']:,} faces + {point_part['element_count']:,} points"
    )


# ---------------------------------------------------------------------------
# Behaviours that a single asset cannot reach
# ---------------------------------------------------------------------------


def test_list_annotations_asset_filter_partitions_the_two_assets(
    real_package: USAPPackage,
) -> None:
    """
    G13, on data where it means something. With one registered asset the
    filter returns everything either way, so a broken implementation is
    indistinguishable from a working one.
    """
    pkg = real_package
    assets = pkg.list_assets()
    annotated = [a for a in assets if a["element_count"]]

    assert len(annotated) >= 2, "needs two element-bearing assets"

    per_asset = {
        a["uri"]: {
            row["annotation_uid"]
            for row in pkg.list_annotations(asset_id=a["asset_id"], limit=100_000)
        }
        for a in annotated
    }

    for uri, uids in per_asset.items():
        assert uids, f"{uri} has no annotations"

    everything = {
        row["annotation_uid"] for row in pkg.list_annotations(limit=100_000)
    }

    for uids in per_asset.values():
        assert uids < everything, "an asset filter returned the unfiltered set"


def test_assessment_is_bound_to_an_asset_not_to_a_part(
    real_package: USAPPackage,
) -> None:
    """
    US-ANN-08: an assessment records the 3D asset it refers to. Once a second
    evaluation exists on that asset, an unqualified write has no right answer,
    and guessing would silently rewrite history.
    """
    pkg = real_package
    annotation = _cross_asset_annotation(pkg)
    annotation_id = annotation["annotation_id"]
    mesh = _mesh_part(pkg)

    pkg.create_assessment(annotation_id, mesh["asset_id"], assessed_at="2027-01-01")

    with pytest.raises(USAPAmbiguityError):
        pkg.attach_annotation_elements(
            annotation_id=annotation_id,
            asset_part_id=mesh["asset_part_id"],
            element_kind="face",
            element_indices=[0, 1, 2],
        )


def test_us_ann_08_an_assessment_never_resolves_onto_the_other_asset(
    real_package: USAPPackage,
) -> None:
    """
    US.md:285 -- "does not apply the membership to another 3D asset".

    Two unrelated files would pass this trivially: the indices obviously do
    not belong. A mesh and a point cloud of the same buildings make the wrong
    answer plausible, which is the only way the check has teeth.
    """
    pkg = real_package
    annotation = _cross_asset_annotation(pkg)
    annotation_id = annotation["annotation_id"]
    parts = _parts_by_id(pkg)

    assessments = pkg.list_assessments(annotation_id=annotation_id)

    assert len(assessments) >= 2, (
        "a cross-asset annotation should carry one assessment per asset, "
        f"got {len(assessments)}"
    )

    for assessment in assessments:
        blocks = pkg.elements_for_annotation(
            annotation_id, expand=False, assessment=assessment["assessment_id"]
        )
        reached = {parts[b["asset_part_id"]]["asset_id"] for b in blocks}

        assert len(reached) <= 1, (
            f"assessment {assessment['assessment_uid']} resolved onto "
            f"{len(reached)} assets"
        )


def test_us_asset_02_a_city_object_reports_which_assets_hold_its_geometry(
    real_package: USAPPackage,
) -> None:
    """
    US.md:80 -- selecting a CityObject associated exclusively with a hidden
    asset must say the geometry is not visible. The app can only do that if
    the elements it gets back name their asset, and if an object whose
    geometry lives on one asset does not appear to have geometry on the other.
    """
    pkg = real_package
    parts = _parts_by_id(pkg)

    # Candidate selection in SQL rather than by scanning list_annotations:
    # the objects exclusive to each asset are not evenly distributed through
    # the annotation order, so a bounded scan finds only whichever asset
    # happens to sort first. The assertions below still go through the API.
    rows = pkg.conn.execute(
        """
        SELECT only_asset, MIN(object_uid) AS object_uid
        FROM (
            SELECT co.object_uid                     AS object_uid,
                   COUNT(DISTINCT ap.asset_id)       AS assets,
                   MIN(ap.asset_id)                  AS only_asset
            FROM usap_annotation AS a
            JOIN usap_city_object AS co
              ON co.city_object_id = a.primary_city_object_id
            JOIN usap_membership_block AS mb
              ON mb.annotation_id = a.annotation_id
            JOIN usap_asset_part AS ap
              ON ap.asset_part_id = mb.asset_part_id
            GROUP BY co.object_uid
            HAVING assets = 1
        )
        GROUP BY only_asset
        """
    ).fetchall()

    exclusive = {int(row["only_asset"]): row["object_uid"] for row in rows}

    assert len(exclusive) >= 2, (
        "expected city objects exclusive to each asset; found "
        f"{len(exclusive)}"
    )

    for asset_id, uid in exclusive.items():
        # include_descendants=False: the question is which assets hold *this*
        # object's geometry. Left at its default of True the call also answers
        # for the object's boundary surfaces, which live on the other asset --
        # see test_the_plural_subtree_call_expands_descendants_by_default.
        blocks = pkg.elements_for_city_objects(
            [uid], expand=True, include_descendants=False
        )

        assert blocks, f"{uid} returned no blocks"
        assert all(
            parts[b["asset_part_id"]]["asset_id"] == asset_id for b in blocks
        ), f"{uid} reached an asset it has no geometry on"
        assert all("asset_part_id" in b for b in blocks), (
            "blocks must name their part, or the app cannot route highlights"
        )


def test_the_plural_subtree_call_expands_descendants_by_default(
    real_package: USAPPackage,
) -> None:
    """
    A finding, pinned so it cannot change silently.

    elements_for_city_object takes include_descendants=True by default
    (core.py:3899) and elements_for_city_objects forwards **kwargs unchanged,
    so the plural call the handoff tells the application to use expands the
    link graph whether or not the caller wanted it.

    That makes the same call mean two different things depending on something
    the caller cannot see from the call site: whether the CityGML was ever
    imported. With a graph it answers for the whole subtree; carrier-only, with
    no edges, it answers for the named objects alone. G3 documents the second
    half of that; this is the first.
    """
    pkg = real_package
    kids = [k["object_uid"] for k in pkg.list_city_objects(related_to="building1000")]

    assert kids, "needs an imported link graph"

    subtree = pkg.elements_for_city_objects(["building1000"])
    explicit = pkg.elements_for_city_objects(["building1000"] + kids)
    own_only = pkg.elements_for_city_objects(
        ["building1000"], include_descendants=False
    )

    block_ids = lambda blocks: {b["membership_block_id"] for b in blocks}  # noqa: E731

    # Passing one uid and passing the whole subtree give the same answer.
    assert block_ids(subtree) == block_ids(explicit)
    assert len(own_only) < len(subtree)

    print(
        f"\n  building1000: {len(own_only)} own block(s), "
        f"{len(subtree)} with descendants (from 1 uid), "
        f"{len(explicit)} from {len(kids) + 1} uids"
    )

    # Now the carrier-only shape: same call, same arguments, empty graph.
    pkg.conn.execute("DELETE FROM usap_city_object_relationship")

    assert block_ids(
        pkg.elements_for_city_objects(["building1000"])
    ) == block_ids(own_only), (
        "with no link graph the same call must fall back to own elements"
    )


def test_verify_assets_attributes_a_change_to_the_file_that_changed(
    real_package_dir: Path, tmp_path: Path
) -> None:
    """
    Needs two assets by construction: the point is that the *other* one still
    reports ok. With one asset "something changed" and "this changed" are the
    same sentence.
    """
    staged = tmp_path / "staged"
    staged.mkdir()

    for item in real_package_dir.iterdir():
        if item.is_file() and not item.name.startswith("copy_"):
            shutil.copy2(item, staged / item.name)

    package = staged / REAL_PACKAGE_NAME

    with USAPPackage.open(package) as pkg:
        before = {r["uri"]: r["status"] for r in verify_assets(pkg.conn)}

    hashed = [uri for uri, status in before.items() if status == "ok"]

    assert len(hashed) >= 2, f"needs two hashed assets, got {before}"

    victim, bystander = hashed[0], hashed[1]

    with (staged / victim).open("ab") as handle:
        handle.write(b"\n# appended by a test\n")

    with USAPPackage.open(package) as pkg:
        after = {r["uri"]: r["status"] for r in verify_assets(pkg.conn)}

    assert after[victim] == "changed"
    assert after[bystander] == "ok"


# ---------------------------------------------------------------------------
# Existing behaviours, on inputs large enough to reach their real paths
# ---------------------------------------------------------------------------


def test_memberships_split_across_blocks(real_package: USAPPackage) -> None:
    """
    A 2-triangle mesh can never reach this: an index set only splits when it
    spans more than DEFAULT_BLOCK_SIZE elements.
    """
    pkg = real_package

    row = pkg.conn.execute(
        """
        SELECT annotation_id, asset_part_id, COUNT(*) AS blocks
        FROM usap_membership_block
        GROUP BY annotation_id, asset_part_id
        ORDER BY blocks DESC
        LIMIT 1
        """
    ).fetchone()

    print(f"\n  widest membership spans {row['blocks']} blocks "
          f"of {DEFAULT_BLOCK_SIZE:,}")

    assert row["blocks"] > 1, "no membership spans more than one block"


def test_batch_indices_round_trip_exactly(real_package: USAPPackage) -> None:
    """
    USAP stores integers and gives them back. Everything else it claims rests
    on this one property, so it is worth checking at real width rather than on
    a three-element list.
    """
    pkg = real_package
    parts = _parts_by_id(pkg)
    checked = 0

    for annotation in pkg.list_annotations(limit=200):
        for block in pkg.elements_for_annotation(
            annotation["annotation_id"], expand=True
        ):
            elements = block["elements"]
            part = parts[block["asset_part_id"]]

            assert elements == sorted(set(elements)), "indices came back unordered"
            assert min(elements) >= 0
            assert max(elements) < part["element_count"]
            checked += len(elements)

    print(f"\n  round-tripped {checked:,} element references")

    assert checked > 1000


def test_reverse_query_finds_the_annotation_that_owns_a_face(
    real_package: USAPPackage,
) -> None:
    pkg = real_package
    parts = _parts_by_id(pkg)
    annotation = _cross_asset_annotation(pkg)

    blocks = pkg.elements_for_annotation(annotation["annotation_id"], expand=True)
    block = next(b for b in blocks if b["elements"])
    part = parts[block["asset_part_id"]]
    kind = "face" if part["element_kind"] == 1 else "point"

    hits = pkg.annotations_for_elements(
        asset_part_id=block["asset_part_id"],
        element_kind=kind,
        selected_indices=block["elements"][:5],
    )

    assert annotation["annotation_uid"] in {h["annotation_uid"] for h in hits}

    for hit in hits:
        assert "primary_city_object_gml_id" in hit, "G1: the lasso list must "
        "carry the same identifier the detail panel shows"


def test_validate_report_is_clean_at_deep_and_external(
    real_package: USAPPackage,
) -> None:
    deep = real_package.validate_report(level="deep")

    assert deep.is_ok, [issue.format() for issue in deep.issues]

    external = real_package.validate_report(level="external")
    blocking = [i for i in external.issues if i.severity != "warning"]

    assert not blocking, [issue.format() for issue in blocking]

    for issue in external.issues:
        print(f"\n  external warning: {issue.format()[:120]}")


def test_the_package_survives_close_and_reopen(
    real_package: USAPPackage, real_package_dir: Path
) -> None:
    path = Path(real_package.conn.execute("PRAGMA database_list").fetchone()[2])

    before = len(real_package.list_annotations(limit=100_000))
    blocks_before = real_package.conn.execute(
        "SELECT COUNT(*) FROM usap_membership_block"
    ).fetchone()[0]

    real_package.close()

    with USAPPackage.open(path) as reopened:
        assert len(reopened.list_annotations(limit=100_000)) == before
        assert (
            reopened.conn.execute(
                "SELECT COUNT(*) FROM usap_membership_block"
            ).fetchone()[0]
            == blocks_before
        )
        assert reopened.validate_report(level="deep").is_ok


# ---------------------------------------------------------------------------
# N4 -- value fields at real scale
#
# Membership blocks answer "which elements"; value blocks answer "what value at
# each element". The second half had only ever seen 50 elements. The V1
# contract is dense -- len(values) == element_count -- so shadowing a point
# cloud means supplying a value for all 3.9M points, not just the shadowed
# ones, which is what makes this a scale test rather than a repeat.
# ---------------------------------------------------------------------------

SHADOW_AT = "2026-09-03T15:00:00Z"
DEEP_SHADOW = 0.8


def _db_bytes(pkg: USAPPackage) -> int:
    page_count = pkg.conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = pkg.conn.execute("PRAGMA page_size").fetchone()[0]

    return int(page_count) * int(page_size)


def _random_field(n: int, seed: int = 0):
    import numpy as np

    return np.random.default_rng(seed).random(n, dtype=np.float32)


def _coherent_field(n: int, seed: int = 1):
    """
    The same values, arranged so neighbouring indices agree: the first 40% in
    shadow, the rest lit, each with a little spread. Blocks then fall entirely
    inside one band or the other, which is what makes value_min/value_max able
    to exclude anything.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    field = (0.05 + rng.random(n, dtype=np.float32) * 0.05).astype("float32")
    cut = (n * 4) // 10
    field[:cut] = (0.85 + rng.random(cut, dtype=np.float32) * 0.10).astype("float32")

    return field


def test_n4_a_value_field_round_trips_at_real_scale(
    real_package: USAPPackage,
) -> None:
    """
    'The area in shadow at 3pm on 3 September 2026', over every point.

    assessed_at is load-bearing rather than cosmetic: it dates the measurement,
    so re-running for 4pm adds a second assessment instead of overwriting this
    one -- the case usap_value_block's own comment describes.
    """
    import numpy as np

    pkg = real_package
    part = _point_part(pkg)
    n = part["element_count"]

    field = _random_field(n)
    before = _db_bytes(pkg)

    started = time.perf_counter()
    annotation = pkg.annotate_value_field(
        concept="ShadowFraction",
        asset_part_id=part["asset_part_id"],
        element_kind="point",
        values=field,
        annotation_uid="shadow_3pm_random",
        assessed_at=SHADOW_AT,
        attributes={"unit": "fraction", "validAt": SHADOW_AT, "method": "synthetic"},
    )
    write = time.perf_counter() - started

    annotation_id = int(annotation["annotation_id"])
    grew = _db_bytes(pkg) - before

    started = time.perf_counter()
    read_back = pkg.values_for_annotation(annotation_id)
    read = time.perf_counter() - started

    blocks = pkg.conn.execute(
        "SELECT COUNT(*) FROM usap_value_block WHERE annotation_id = ?",
        (annotation_id,),
    ).fetchone()[0]

    print(
        f"\n  N4 point cloud, {n:,} float32 values"
        f"\n     write {write:6.2f}s | read {read:6.2f}s"
        f"\n     {blocks} value blocks, package grew {grew / (1 << 20):.1f} MB"
        f" ({grew / (n * 4) * 100:.0f}% of the raw array)"
    )

    assert read_back.shape == (n,)
    np.testing.assert_array_equal(read_back, field)

    stats = pkg.value_field_stats(annotation_id)
    print(f"     stats: {stats}")

    # A value field is a property of the geometry, so it hangs off no object.
    # That is G5's unlinked case: the app must label it concept + annotation_uid.
    assert annotation["primary_city_object_id"] is None

    assert pkg.validate_report(level="deep").is_ok


def test_n4_block_skipping_brackets_the_query_cost(
    real_package: USAPPackage,
) -> None:
    """
    Each value block stores value_min/value_max beside its payload, written
    once at insert. A comparison predicate reads those two floats and skips the
    block whole when they cannot match -- no decompression.

    Whether that helps is a property of the data, not of the code, so the two
    fields bracket it. Uniform random spreads every block across [0,1], so the
    summary excludes nothing and every block is decoded. Coherent runs give
    each block a narrow range, so most are skipped. Real shadow lands between,
    depending on whether LAS record order groups nearby points together.
    """
    pkg = real_package
    part = _point_part(pkg)
    n = part["element_count"]

    print(f"\n  N4 elements_where(>= {DEEP_SHADOW}) over {n:,} points\n")

    for label, field in (
        ("random   ", _random_field(n)),
        ("coherent ", _coherent_field(n)),
    ):
        annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=part["asset_part_id"],
            element_kind="point",
            values=field,
            annotation_uid=f"shadow_3pm_{label.strip()}",
            assessed_at=SHADOW_AT,
        )
        annotation_id = int(annotation["annotation_id"])

        total, must_decode = pkg.conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN value_max >= ? THEN 1 ELSE 0 END)
            FROM usap_value_block
            WHERE annotation_id = ?
            """,
            (DEEP_SHADOW, annotation_id),
        ).fetchone()

        payload = pkg.conn.execute(
            "SELECT SUM(LENGTH(payload)) FROM usap_value_block "
            "WHERE annotation_id = ?",
            (annotation_id,),
        ).fetchone()[0]

        started = time.perf_counter()
        hits = pkg.elements_where(annotation_id, (">=", DEEP_SHADOW))
        elapsed = time.perf_counter() - started

        print(
            f"    {label} {elapsed:6.3f}s  {len(hits):>9,} points"
            f"  |  {must_decode}/{total} blocks decoded"
            f"  |  payload {payload / (1 << 20):5.1f} MB"
            f" ({payload / (n * 4) * 100:3.0f}% of raw)"
        )

        assert len(hits) > 0


# ---------------------------------------------------------------------------
# N2b -- the configuration folder as it will really be shipped: the OGC XSDs
# and a third-party ontology, read together in one pass (US-DATA-04).
#
# data/vocabulary holds only .xsd and .json, so the folder walk's .owl branch
# had never run against real files -- only against a hand-written fixture.
# ---------------------------------------------------------------------------

REAL_ONTOLOGIES = ("uish_ontology_building.owl", "uish_ontology_sensors.owl")


def _stage_vocabulary_with_ontologies(
    realdata_dir: Path, vocabulary_dir: Path, tmp_path: Path
) -> Path:
    """The vocabulary folder plus whichever real ontologies are on disk."""
    staged = tmp_path / "vocabulary"
    shutil.copytree(vocabulary_dir, staged)

    found = 0

    for name in REAL_ONTOLOGIES:
        source = next(
            (p for p in (realdata_dir / name,
                         Path(__file__).resolve().parents[1] / name) if p.exists()),
            None,
        )

        if source is not None:
            shutil.copy2(source, staged / name)
            found += 1

    if not found:
        pytest.skip(f"none of {REAL_ONTOLOGIES} are in the checkout")

    return staged


def test_n2b_schemas_and_real_ontologies_load_in_one_folder_pass(
    pkg: USAPPackage,
    realdata_dir: Path,
    real_vocabulary_dir: Path,
    tmp_path: Path,
) -> None:
    """
    The startup path with everything in it: 22 OGC XSDs, third-party .owl
    files, and a JSON registry, dispatched by suffix in a single call.
    """
    staged = _stage_vocabulary_with_ontologies(
        realdata_dir, real_vocabulary_dir, tmp_path
    )

    suffixes = sorted({p.suffix for p in staged.iterdir()})

    assert ".owl" in suffixes, "the point of this test is an .owl in the folder"

    started = time.perf_counter()
    results = load_vocabulary_folder(pkg, staged, scheme_version="3.0")
    elapsed = time.perf_counter() - started

    print(f"\n  folder held {suffixes}, loaded in {elapsed:.2f}s")

    for name, result in sorted(results.items()):
        count = len(getattr(result, "by_uri", None) or getattr(result, "concepts", ()))
        links = getattr(result, "relationship_types", None)
        suffix = f", {len(links)} link types" if links is not None else ""
        print(f"      {Path(name).name:34} {count:>4} concepts{suffix}")

    by_scheme = {
        row["scheme"]: (row["n"], row["with_parent"])
        for row in pkg.conn.execute(
            """
            SELECT scheme, COUNT(*) AS n,
                   SUM(CASE WHEN parent_class_id IS NOT NULL THEN 1 ELSE 0 END)
                       AS with_parent
            FROM usap_semantic_class GROUP BY scheme
            """
        )
    }

    print(f"      -> {len(pkg.list_accepted_concepts())} concepts, by scheme:")
    for scheme, (n, with_parent) in sorted(by_scheme.items()):
        print(f"           {scheme:24} {n:>4} concepts, {with_parent:>4} with a parent")

    # Every source kind contributed, and each brought its own hierarchy:
    # substitutionGroup from the XSDs, same-file subClassOf from the ontology.
    assert by_scheme["citygml"][0] > 200
    assert by_scheme["citygml"][1] > 0
    assert by_scheme["ontology"][0] > 0
    assert by_scheme["ontology"][1] > 0


def test_n2b_an_ontology_parent_in_citygml_does_not_link(
    pkg: USAPPackage,
    realdata_dir: Path,
    real_vocabulary_dir: Path,
    tmp_path: Path,
) -> None:
    """
    A finding, pinned. An ADE class declaring itself a subclass of a CityGML
    class lands as a root, silently, for two independent reasons:

      1. _register_ontology_facts only resolves a parent declared in the *same
         file* (domain_vocab.py:687) -- deliberate, since guessing would be
         worse than a root.
      2. Even lifting that, the identities do not match. The XSD registers
         ADEOfBuilding as
             http://www.opengis.net/citygml/building/3.0#ADEOfBuilding
         while the ontology's OWL rendering of CityGML names it
             https://dataset-dl.liris.cnrs.fr/.../CityGML/3.0/building#ADEOfBuilding

    Nothing warns. The concept is registered and usable; only the link to the
    CityGML hierarchy is missing, so include_subclasses never reaches it.
    """
    staged = _stage_vocabulary_with_ontologies(
        realdata_dir, real_vocabulary_dir, tmp_path
    )
    load_vocabulary_folder(pkg, staged, scheme_version="3.0")

    # The CityGML class the ontology names *is* in the package, from the XSD.
    citygml_side = pkg.conn.execute(
        "SELECT scheme, class_uri FROM usap_semantic_class "
        "WHERE local_name = 'ADEOfBuilding'"
    ).fetchall()

    if not citygml_side:
        pytest.skip("this CityGML distribution has no ADEOfBuilding")

    assert any(row["scheme"] == "citygml" for row in citygml_side)

    citygml_ids = {
        row["semantic_class_id"]
        for row in pkg.conn.execute(
            "SELECT semantic_class_id FROM usap_semantic_class WHERE scheme = 'citygml'"
        )
    }

    crossing = pkg.conn.execute(
        """
        SELECT COUNT(*) AS n FROM usap_semantic_class
        WHERE scheme = 'ontology' AND parent_class_id IN (
            SELECT semantic_class_id FROM usap_semantic_class WHERE scheme = 'citygml'
        )
        """
    ).fetchone()["n"]

    orphans = pkg.conn.execute(
        "SELECT COUNT(*) AS n FROM usap_semantic_class "
        "WHERE scheme = 'ontology' AND parent_class_id IS NULL"
    ).fetchone()["n"]

    print(
        f"\n  ontology classes parented into CityGML: {crossing}"
        f"   (roots: {orphans}, CityGML classes available: {len(citygml_ids)})"
    )

    # Pinning current behaviour, not endorsing it. If this ever becomes
    # non-zero the reader learned to cross schemes, and that is a deliberate
    # change worth noticing here.
    assert crossing == 0
    assert orphans > 0

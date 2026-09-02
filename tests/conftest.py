from __future__ import annotations

from pathlib import Path
from typing import Iterator

import laspy
import numpy as np
import pytest
import trimesh

from usap import (
    DEFAULT_SCHEMA_PATH,
    ELEMENT_KIND_FACE,
    USAPPackage,
    VocabularyResult,
    load_citygml_schema,
)
from usap.constants import (
    CITYGML_3_0_BUILDING_NS,
    CITYGML_3_0_CONSTRUCTION_NS,
    CITYGML_3_0_CORE_NS,
)

CITYGML_3_0_GROUP_NS = "http://www.opengis.net/citygml/cityobjectgroup/3.0"

# Re-exported: tests that need the schema path must get it from the package,
# never from the checkout layout, so the suite passes against an installed wheel.
SCHEMA_PATH = DEFAULT_SCHEMA_PATH

# A faithful subset of the OGC CityGML 3.0 schemas, committed under tests/.
# USAP ships no CityGML vocabulary, so the suite brings its own concept source
# rather than depending on the full OGC distribution (not vendored, and
# gitignored when downloaded for local work).
CITYGML_SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "citygml_3_0_subset"


def make_pkg(tmp_path: Path, name: str = "pkg.usap.gpkg") -> USAPPackage:
    """Create a fresh empty package under tmp_path."""
    return USAPPackage.create(
        tmp_path / name,
        overwrite=True,
    )


def seed_citygml_concepts(pkg: USAPPackage) -> VocabularyResult:
    """
    Register CityGML 3.0 concepts from the fixture schemas.

    Replaces the deleted seed_default_citygml_vocabulary(). Concepts now come
    from a schema the caller supplies, so every test that needs a CityGML class
    says so explicitly, exactly as a real package must.
    """
    return load_citygml_schema(pkg, CITYGML_SCHEMA_FIXTURE, scheme_version="3.0")


# Which CityGML 3.0 properties mean "part of", and which relate two objects
# without either containing the other. This is the one thing no CityGML
# artifact states — not the XSD, not the conceptual model's data dictionary,
# not the OWL rendering — so it has to be asserted by whoever decides what
# "and its parts" should mean.
#
# In a real package these assertions live in the ontology and arrive via
# load_ontology(); here they are inline so a test that only needs a category
# does not have to carry an .owl fixture. tests/fixtures/ has the real thing
# for the tests that exercise the reader itself.
CITYGML_3_0_RELATIONSHIP_CATEGORIES = {
    # core
    ("boundary", CITYGML_3_0_CORE_NS): "containment",
    ("relatedTo", CITYGML_3_0_CORE_NS): "peer",
    ("generalizesTo", CITYGML_3_0_CORE_NS): "generalization",
    # construction
    ("filling", CITYGML_3_0_CONSTRUCTION_NS): "containment",
    ("fillingSurface", CITYGML_3_0_CONSTRUCTION_NS): "containment",
    # building
    ("buildingPart", CITYGML_3_0_BUILDING_NS): "containment",
    ("buildingRoom", CITYGML_3_0_BUILDING_NS): "containment",
    ("buildingInstallation", CITYGML_3_0_BUILDING_NS): "containment",
    ("buildingConstructiveElement", CITYGML_3_0_BUILDING_NS): "containment",
    ("buildingFurniture", CITYGML_3_0_BUILDING_NS): "containment",
    ("buildingSubdivision", CITYGML_3_0_BUILDING_NS): "containment",
    # cityobjectgroup
    ("groupMember", CITYGML_3_0_GROUP_NS): "grouping",
    ("parent", CITYGML_3_0_GROUP_NS): "grouping",
}


def seed_citygml_relationship_categories(pkg: USAPPackage) -> None:
    """
    Classify the CityGML 3.0 link types the fixtures use.

    Without this a package still records every edge and can still query them
    by name, but `descendants_of` returns the root alone and validation warns
    UNCLASSIFIED_RELATIONSHIP_TYPE — the accepted consequence of USAP shipping
    no link vocabulary of its own.
    """
    for (local_name, code_space), category in (
        CITYGML_3_0_RELATIONSHIP_CATEGORIES.items()
    ):
        pkg.register_relationship_type(
            local_name,
            code_space=code_space,
            category=category,
        )


def make_mesh_part(pkg: USAPPackage, element_count: int = 100) -> int:
    """Register a bare mesh asset with one face part; returns asset_part_id."""
    asset_id = pkg.register_asset(uri="mesh.ply", asset_kind="mesh")

    return pkg.register_asset_part(
        asset_id=asset_id,
        part_path="geometry/0",
        element_kind=ELEMENT_KIND_FACE,
        element_count=element_count,
    )


@pytest.fixture
def pkg(tmp_path: Path) -> Iterator[USAPPackage]:
    with make_pkg(tmp_path) as p:
        yield p


@pytest.fixture
def mesh_part(pkg: USAPPackage) -> int:
    return make_mesh_part(pkg)


def assert_package_valid(pkg) -> None:
    """
    Assert validate_report() is clean, printing the issues on failure
    (a bare `assert report.is_ok` fails without saying why).
    """
    report = pkg.validate_report()
    assert report.is_ok, [issue.format() for issue in report.issues]


def write_tiny_las(path: Path, point_count: int = 10) -> None:
    """
    Write a minimal LAS file with `point_count` points for tests.

    Coordinates are distinct per axis (x, x+100, x+200) so bounds are non-degenerate.
    """
    header = laspy.LasHeader(point_format=3, version="1.2")
    las = laspy.LasData(header)

    las.x = np.arange(point_count, dtype=float)
    las.y = np.arange(point_count, dtype=float) + 100.0
    las.z = np.arange(point_count, dtype=float) + 200.0

    las.write(path)


def write_tiny_mesh(path: Path) -> None:
    """
    Write a minimal 2-triangle quad mesh for tests.

    The export format is inferred from the file suffix (e.g. .ply, .obj).
    """
    vertices = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
        ]
    )

    faces = np.array(
        [
            [0, 1, 2],
            [0, 2, 3],
        ]
    )

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.export(path)


# ---------------------------------------------------------------------------
# Real-data fixtures.
#
# Everything above builds its own input: a 2-triangle mesh, a 10-point LAS, a
# 7-file XSD subset. That keeps the suite hermetic, but it means no test ever
# reaches the paths only real files reach -- block splitting past
# DEFAULT_BLOCK_SIZE, a 22-XSD vocabulary, a reverse query over 100k indices.
#
# --realdata-dir points at a directory holding real ones. Absent, every fixture
# below skips, so the default run is unchanged.
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--realdata-dir",
        action="store",
        default=None,
        metavar="PATH",
        help=(
            "Directory holding real assets (a mesh, a point cloud of the same "
            "area, a .gml, and a vocabulary/ folder). Enables tests/"
            "test_realdata.py; without it those tests skip."
        ),
    )


@pytest.fixture(scope="session")
def realdata_dir(request: pytest.FixtureRequest) -> Path:
    raw = request.config.getoption("--realdata-dir")

    if not raw:
        pytest.skip("needs --realdata-dir")

    path = Path(raw).resolve()

    if not path.is_dir():
        pytest.skip(f"--realdata-dir {path} is not a directory")

    return path


def _one(realdata_dir: Path, *candidates: str) -> Path:
    """The first candidate that exists, or skip naming all of them."""
    for name in candidates:
        candidate = realdata_dir / name

        if candidate.exists():
            return candidate

    pytest.skip(f"none of {candidates} found in {realdata_dir}")


@pytest.fixture(scope="session")
def real_mesh_path(realdata_dir: Path) -> Path:
    return _one(realdata_dir, "catania.obj")


@pytest.fixture(scope="session")
def real_las_path(realdata_dir: Path) -> Path:
    return _one(realdata_dir, "catania.las")


@pytest.fixture(scope="session")
def real_citygml_path(realdata_dir: Path) -> Path:
    return _one(realdata_dir, "catania_ids.gml", "catania.gml")


@pytest.fixture(scope="session")
def real_vocabulary_dir(realdata_dir: Path) -> Path:
    return _one(realdata_dir, "vocabulary")


@pytest.fixture(scope="session")
def real_batch_path(realdata_dir: Path) -> Path:
    return _one(realdata_dir, "batches/catania_annotations.json")


# The same categories as a project-config fragment, for builds driven by
# build_project_package_from_file rather than by SDK calls.
CITYGML_CONTAINMENT_CONFIG = [
    {"local_name": local_name, "code_space": code_space, "category": category}
    for (local_name, code_space), category in (
        CITYGML_3_0_RELATIONSHIP_CATEGORIES.items()
    )
]


# The real package is expensive to build (~60 s), so it is built once per
# session and handed to tests as a cheap copy. The copy lives beside the staged
# assets rather than in the test's own tmp_path, because asset uris are
# package-relative: a package moved away from its assets resolves them from the
# wrong directory and verify_assets reports 'missing'.
REAL_PACKAGE_NAME = "real.usap.gpkg"


@pytest.fixture(scope="session")
def real_package_dir(
    tmp_path_factory: pytest.TempPathFactory,
    real_mesh_path: Path,
    real_las_path: Path,
    real_citygml_path: Path,
    real_vocabulary_dir: Path,
    real_batch_path: Path,
) -> Path:
    """
    A staging copy of the real files, with a package built beside them.

    Copying (~1.2 s for 150 MB on a local disk) buys isolation -- no test writes
    into the caller's --realdata-dir -- and lets the build use bare-filename
    uris, which is the resolution path a delivered package actually uses.
    """
    import shutil

    from usap import build_project_package

    staged = tmp_path_factory.mktemp("realdata")

    for source in (real_mesh_path, real_las_path, real_citygml_path):
        shutil.copy2(source, staged / source.name)

    shutil.copytree(real_vocabulary_dir, staged / "vocabulary")
    shutil.copy2(real_batch_path, staged / "batch.json")

    build_project_package(
        {
            "db_path": REAL_PACKAGE_NAME,
            "vocabulary_folder": "vocabulary",
            "citygml_schema_version": "3.0",
            "relationship_types": CITYGML_CONTAINMENT_CONFIG,
            "citygml": {
                "path": real_citygml_path.name,
                "uri": real_citygml_path.name,
                "compute_hash": False,
            },
            "las": [
                {
                    "path": real_las_path.name,
                    "uri": real_las_path.name,
                    "part_path": "points/all",
                    "compute_hash": True,
                }
            ],
            "meshes": [
                {
                    "path": real_mesh_path.name,
                    "uri": real_mesh_path.name,
                    "representation_name": "real_lod1",
                    "representation_kind": "building_mesh",
                    "compute_hash": True,
                }
            ],
            "annotation_batches": ["batch.json"],
            "validation_level": "deep",
        },
        base_dir=staged,
    )

    return staged


@pytest.fixture
def real_package(real_package_dir: Path, request) -> Iterator[USAPPackage]:
    """An isolated copy of the session package, safe to mutate."""
    import shutil

    name = f"copy_{abs(hash(request.node.nodeid)) % 10**8}.usap.gpkg"
    copy = real_package_dir / name
    shutil.copy2(real_package_dir / REAL_PACKAGE_NAME, copy)

    try:
        with USAPPackage.open(copy) as pkg:
            yield pkg
    finally:
        copy.unlink(missing_ok=True)

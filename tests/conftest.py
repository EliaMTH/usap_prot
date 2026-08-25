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


# The same categories as a project-config fragment, for builds driven by
# build_project_package_from_file rather than by SDK calls.
CITYGML_CONTAINMENT_CONFIG = [
    {"local_name": local_name, "code_space": code_space, "category": category}
    for (local_name, code_space), category in (
        CITYGML_3_0_RELATIONSHIP_CATEGORIES.items()
    )
]

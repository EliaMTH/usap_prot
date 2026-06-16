# USAP — Urban Semantic Annotation Package

USAP is a prototype Python package and SQLite/GeoPackage-style data model for storing **semantic annotations over external urban data assets**.

The current prototype supports annotations over:

- **LAS/LAZ point clouds** using stable point indices.
- **Mesh files** using stable face indices.
- **CityGML semantic objects** using imported `gml:id` / object identifiers.
- **CityGML standard concepts** loaded from an external vocabulary registry.
- **ADE-like/custom concepts** loaded from an external vocabulary registry.

USAP does **not** copy geometry into the package. Instead, it stores references to external assets and compact membership blocks that identify which points, faces, or elements are annotated.

---

## Project status

This repository currently contains a working MVP prototype.

Implemented:

- Core USAP SQLite schema.
- Minimal GeoPackage metadata tables.
- Python SDK for creating, opening, validating, and querying packages.
- Semantic classes and city-object graph support.
- Annotation CRUD helpers.
- Compressed membership blocks for selected elements.
- LAS/LAZ point-cloud adapter.
- Generic mesh adapter for LoD1, LoD2, or arbitrary triangulated city meshes.
- CityGML semantic importer.
- External CityGML/ADE concept registries.
- JSON batch annotation importer.
- Real-project package builder.
- End-to-end smoke-test workflow.

Not yet implemented:

- Full CityGML geometry import.
- Full CityGML schema validation.
- Formal ADE XML schema generation or validation.
- Spatial indexing / RTree acceleration.
- Viewer or GUI integration.
- Multi-user editing workflow.
- Migration system for evolving package versions.

---

## Core idea

USAP separates **meaning** from **geometry storage**.

External files remain external:

```text
area.gml
area.las
area_lod1.obj
area_lod2.obj
city_triangulation.ply
```

The USAP package stores:

```text
asset references
asset parts
semantic concepts
city objects
annotations
membership blocks
relationships
metadata
```

A single annotation can point to multiple representations:

```text
annotation: ann_energy_roof_001
  concept: EnergyRoof
  primary CityGML object: building_1_roof_1

  memberships:
    LAS points: [100, 101, 102]
    LoD2 mesh faces: [40, 41, 42]
    generic triangulation faces: [800, 801]
```

This allows the same semantic claim to connect CityGML objects, point clouds, and meshes.

---

## Repository structure

Typical layout:

```text
usap_prot/
  pyproject.toml
  README.md
  .gitignore

  sql/
    schema.sql

  src/
    usap/
      __init__.py
      _util.py
      constants.py
      core.py
      encoding.py
      errors.py
      sqlite_utils.py
      validation.py
      geopackage.py
      domain_vocab.py
      batch.py
      project_builder.py
      synthetic.py
      adapters/
        __init__.py
        citygml_adapter.py
        las_adapter.py
        mesh_adapter.py

  vocabularies/
    citygml_3_0_mvp.json
    usap_ade_prototype.json

  examples/
    build_project_package.py
    smoke_test_project_package.py
    apply_annotation_batch.py
    build_integrated_prototype.py
    build_synthetic.py
    demo_sdk.py
    import_citygml_demo.py
    register_las_demo.py
    register_mesh_demo.py
    list_concepts_demo.py
    validate_package.py
    batches/
      example_annotation_batch.json

  project_configs/
    example_project.json
    example_project_catania.json

  scripts/
    benchmark_phase1.py
    profile_synthetic_build.py

  tests/
    test_core.py
    test_synthetic.py
    test_validation.py
    test_geopackage.py
    test_las_adapter.py
    test_citygml_adapter.py
    test_mesh_adapter.py
    test_integrated_prototype.py
    test_annotation_crud.py
    test_concept_annotation_api.py
    test_concept_registry.py
    test_external_vocabulary.py
    test_batch_annotations.py
    test_project_builder.py
```

---

## Installation

Create a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Install the package in editable mode:

```bash
python -m pip install -e .
```

Run the test suite:

```bash
python -m pytest
```

---

## Key concepts

### Asset

An external file registered in USAP.

Examples:

```text
CityGML file
LAS/LAZ point cloud
OBJ/PLY/GLB mesh
```

USAP stores the path, kind, media type, optional content hash, and metadata.

### Asset part

A stable indexable part of an asset.

Examples:

```text
points/all
geometry/0:default
geometry/1:building_mesh
```

Each asset part has:

```text
element_kind
  point
  face

element_count
  number of points or faces
```

### Semantic class / concept

A registered concept accepted by USAP.

Examples:

```text
RoofSurface
WallSurface
Window
Road
EnergyRoof
VisualFacade
```

Concepts may come from:

```text
CityGML registry
ADE/custom registry
```

### City object

A semantic object, usually imported from CityGML.

Examples:

```text
building_1
building_1_roof_1
building_1_wall_1
building_1_window_1
```

### Annotation

A semantic claim linked to one concept and optionally to one city object.

Example:

```text
annotation_uid: ann_energy_roof_001
concept: EnergyRoof
primary city object: building_1_roof_1
attributes: energy/emissions JSON metadata
```

### Membership block

A compressed set of selected element indices for one annotation, one asset part, and one element kind.

Example:

```text
annotation ann_energy_roof_001
asset part area.las points/all
selected point indices [100, 101, 102]
```

---

## Concept registries

USAP uses external JSON files to define accepted concepts.

Current registries:

```text
vocabularies/citygml_3_0_mvp.json
vocabularies/usap_ade_prototype.json
```

The CityGML registry is an MVP curated subset of common CityGML 3.0 concepts. It is not a complete CityGML ontology or schema extraction.

The ADE registry is an ADE-ready custom registry for project-specific concepts such as:

```text
EnergyBuilding
EnergyFacade
EnergyRoof
PermeabilityExternalSurface
PermeabilityUrbanZone
AcousticBuilding
AcousticUrbanArea
VisualBuilding
VisualFacade
```

List accepted concepts:

```bash
python examples/list_concepts_demo.py
python examples/list_concepts_demo.py --search Roof
```

In Python:

```python
from usap import USAPPackage, seed_default_citygml_vocabulary, seed_default_ade_vocabulary

with USAPPackage.create("concepts.usap.gpkg", schema_path="sql/schema.sql", overwrite=True) as pkg:
    seed_default_citygml_vocabulary(pkg)
    seed_default_ade_vocabulary(pkg)

    for concept in pkg.list_accepted_concepts(search="Roof"):
        print(concept["local_name"], concept["class_uri"])
```

---

## Build a real project package

Two project configs are provided:

- `project_configs/example_project_catania.json` — ready to run against the bundled
  Catania study-area data under `data/`. Those data files are large and are **not**
  committed to git, so you must supply your own `data/catania.*` to run it.
- `project_configs/example_project.json` — a generic template; edit its `area.*` paths
  to point at your own data files.

The Catania config looks like this:

```json
{
  "db_path": "../outputs/example_project_catania.usap.gpkg",
  "manifest_path": "../outputs/example_project_catania_manifest.json",
  "schema_path": "../sql/schema.sql",

  "vocabularies": [
    "../vocabularies/citygml_3_0_mvp.json",
    "../vocabularies/usap_ade_prototype.json"
  ],

  "citygml": {
    "path": "../data/catania.gml",
    "graph_name": "citygml_import",
    "also_usap_default": true,
    "compute_hash": true
  },

  "las": [
    {
      "path": "../data/catania.las",
      "part_path": "points/all",
      "compute_hash": true
    }
  ],

  "meshes": [
    {
      "path": "../data/catania.obj",
      "representation_name": "buildings_lod1",
      "representation_kind": "building_mesh",
      "lod": "LoD1",
      "compute_hash": true
    }
  ]
}
```

Run:

```bash
python examples/build_project_package.py project_configs/example_project_catania.json
```

Expected outputs:

```text
outputs/example_project_catania.usap.gpkg
outputs/example_project_catania_manifest.json
```

The manifest tells you which `asset_part_id` values to use in annotation batches.

---

## Run an end-to-end smoke test

After building a project package:

```bash
python examples/smoke_test_project_package.py \
  outputs/example_project_catania.usap.gpkg \
  outputs/example_project_catania_manifest.json \
  --batch-out outputs/smoke_batch.json
```

This creates one annotation, attaches it to:

```text
one CityGML city object
one LAS point asset part
one mesh face asset part
```

and then queries it back from both a selected LAS point and a selected mesh face.

To rerun and replace the same smoke annotation:

```bash
python examples/smoke_test_project_package.py \
  outputs/example_project_catania.usap.gpkg \
  outputs/example_project_catania_manifest.json \
  --batch-out outputs/smoke_batch.json \
  --replace-existing
```

Use a specific CityGML object:

```bash
python examples/smoke_test_project_package.py \
  outputs/example_project_catania.usap.gpkg \
  outputs/example_project_catania_manifest.json \
  --city-object-uid YOUR_GML_ID \
  --replace-existing
```

Use a specific mesh representation:

```bash
python examples/smoke_test_project_package.py \
  outputs/example_project_catania.usap.gpkg \
  outputs/example_project_catania_manifest.json \
  --mesh-representation-name buildings_lod2 \
  --replace-existing
```

---

## Batch annotation format

Batch annotations are JSON files.

Example:

```json
{
  "annotations": [
    {
      "annotation_uid": "ann_energy_roof_001",
      "concept": "EnergyRoof",
      "city_object_uid": "building_1_roof_1",
      "label": "Energy roof annotation",
      "status": "draft",
      "confidence": 0.8,
      "attributes": {
        "domain": "energy_emissions",
        "geometric_attributes": {
          "roof_slope": 31.5,
          "orientation": "SE",
          "shading": "partial"
        },
        "non_geometric_attributes": {
          "construction_era": "1946-1970",
          "use": "residential"
        },
        "derived_indicators": {
          "specific_energy_kwh_m2": null,
          "co2_emissions": null
        }
      },
      "memberships": [
        {
          "asset_part_id": 2,
          "element_kind": "point",
          "element_indices": [100, 101, 102]
        },
        {
          "asset_part_id": 3,
          "element_kind": "face",
          "element_indices": [40, 41, 42]
        }
      ]
    }
  ]
}
```

Apply a batch:

```bash
python examples/apply_annotation_batch.py \
  outputs/example_project_catania.usap.gpkg \
  path/to/batch.json
```

Replace an existing annotation with the same `annotation_uid`:

```bash
python examples/apply_annotation_batch.py \
  outputs/example_project_catania.usap.gpkg \
  path/to/batch.json \
  --replace-existing
```

The batch importer validates:

```text
concept is registered
city object exists
asset part exists
element kind matches asset part
element indices are in range
annotation UID is not duplicated unless replacement is requested
```

---

## Python API examples

### Create a package and load vocabularies

```python
from usap import (
    USAPPackage,
    seed_default_citygml_vocabulary,
    seed_default_ade_vocabulary,
)

with USAPPackage.create("demo.usap.gpkg", schema_path="sql/schema.sql", overwrite=True) as pkg:
    seed_default_citygml_vocabulary(pkg)
    seed_default_ade_vocabulary(pkg)
```

### Register LAS and mesh assets

```python
from usap import register_las_asset, register_mesh_asset

las = register_las_asset(pkg, "data/area.las")

mesh = register_mesh_asset(
    pkg,
    "data/area_lod2.obj",
    representation_name="buildings_lod2",
    representation_kind="building_mesh",
    lod="LoD2",
)
```

### Import CityGML semantics

```python
from usap import import_citygml_semantics

result = import_citygml_semantics(pkg, "data/area.gml")

print(result.object_count)
print(result.relationship_count)
```

### Annotate elements with a CityGML concept

```python
from usap import ELEMENT_KIND_FACE

annotation = pkg.annotate_elements(
    concept="RoofSurface",
    annotation_uid="ann_roof_faces_001",
    asset_part_id=mesh.primary_asset_part_id,
    element_kind=ELEMENT_KIND_FACE,
    element_indices=[10, 11, 12],
    label="Roof faces",
    status="accepted",
)
```

### Annotate elements with an ADE/custom concept

```python
from usap import ELEMENT_KIND_POINT

annotation = pkg.annotate_elements(
    concept="EnergyRoof",
    annotation_uid="ann_energy_roof_points_001",
    city_object_uid="building_1_roof_1",
    asset_part_id=las.asset_part_id,
    element_kind=ELEMENT_KIND_POINT,
    element_indices=[100, 101, 102],
    attributes={
        "domain": "energy_emissions",
        "geometric_attributes": {
            "roof_slope": 31.5
        }
    },
)
```

### Attach a second representation to the same annotation

```python
annotation = pkg.attach_annotation_elements(
    annotation_id=annotation["annotation_id"],
    asset_part_id=mesh.primary_asset_part_id,
    element_kind="face",
    element_indices=[40, 41, 42],
)
```

### Query annotations from selected elements

```python
matches = pkg.annotations_for_elements(
    asset_part_id=las.asset_part_id,
    element_kind="point",
    selected_indices=[101],
)

for match in matches:
    print(match["annotation_uid"], match["semantic_class"])
```

### Read, list, update, and delete annotations

```python
annotation = pkg.get_annotation(annotation_uid="ann_energy_roof_points_001")

items = pkg.list_annotations(
    semantic_class_local_name="EnergyRoof",
    status="draft",
)

updated = pkg.update_annotation(
    annotation["annotation_id"],
    status="accepted",
    confidence=0.9,
)

pkg.delete_annotation(annotation["annotation_id"])
```

---

## Element kinds

The user-facing API accepts readable element-kind names:

```text
point
points
face
faces
triangle
triangles
```

Internally, these are normalized to USAP constants.

This means both of these are valid:

```python
pkg.annotations_for_elements(asset_part_id=1, element_kind="point", selected_indices=[1])
```

```python
pkg.annotations_for_elements(asset_part_id=1, element_kind=ELEMENT_KIND_POINT, selected_indices=[1])
```

---

## CityGML support

The current CityGML importer is intentionally semantic-only.

It imports:

```text
gml:id / object identity
semantic class names
basic object nesting relationships
source provenance
```

It does not yet import:

```text
full CityGML geometry
full schema validation
xlink resolution
complete ADE XML interpretation
```

This is enough for the MVP because USAP uses CityGML mainly as the semantic/object identity backbone and stores geometry membership against external LAS and mesh assets.

---

## Mesh support

The mesh adapter is generic.

It is not limited to LoD1 or LoD2.

Any stable triangular mesh can be registered, for example:

```text
LoD1 building mesh
LoD2 building mesh
generic city triangulation
TIN terrain
photogrammetry mesh
simulation mesh
```

The important condition is:

```text
face indices must remain stable for the registered file version
```

If a mesh is remeshed, simplified, reordered, or re-exported in a way that changes face order, register it as a new asset.

---

## Validation

Validate a package in Python:

```python
report = pkg.validate_report()
report.print()
```

Or run an example validator:

```bash
python examples/validate_package.py outputs/example_project_catania.usap.gpkg
```

The validator checks, among other things:

```text
GeoPackage metadata presence
USAP profile presence
orphan references
membership payload validity
membership element count consistency
membership index bounds
semantic class closure
city object closure
concept registry duplicates
```

---

## Generated files and Git

Recommended `.gitignore` entries:

```text
.venv/
__pycache__/
.pytest_cache/
*.usap.gpkg
*.prof
outputs/
```

Commit source code, tests, examples, configs, and vocabulary JSON files.

Do not commit large real data files unless intentionally using Git LFS or another data-management strategy.

---

## Development workflow

Useful commands:

```bash
python -m pytest
```

Build a project package:

```bash
python examples/build_project_package.py project_configs/example_project_catania.json
```

Run smoke test:

```bash
python examples/smoke_test_project_package.py \
  outputs/example_project_catania.usap.gpkg \
  outputs/example_project_catania_manifest.json \
  --batch-out outputs/smoke_batch.json \
  --replace-existing
```

Apply annotation batch:

```bash
python examples/apply_annotation_batch.py \
  outputs/example_project_catania.usap.gpkg \
  path/to/batch.json \
  --replace-existing
```

---

## Current MVP capability

The current prototype can demonstrate the following use case:

```text
Given:
  a CityGML file for an area,
  a LAS/LAZ point cloud for the same area,
  one or more mesh representations of the same area,
  a CityGML/ADE concept registry,

USAP can:
  create a package,
  register all assets,
  import CityGML semantic objects,
  load accepted concepts,
  apply JSON annotation batches,
  attach annotations to LAS points and mesh faces,
  link annotations to CityGML objects,
  query annotations from selected elements,
  validate package integrity.
```

---

## Roadmap

Immediate next steps:

1. Build a real package for the target study area.
2. Prepare a domain-specific batch annotation file.
3. Create a demo script/notebook for the target use case.
4. Add helper functions for deriving or importing domain attributes.
5. Improve CityGML concept registry coverage.
6. Add optional xlink handling if needed by the real CityGML file.
7. Add simple spatial search or bounding-box filtering if real files require it.

Later steps:

1. Formalize the ADE registry.
2. Add ADE schema/export support.
3. Add richer CityGML import support.
4. Add viewer integration.
5. Add performance optimizations for larger cities.
6. Add versioned migrations.

---

## Prototype warning

This repository is currently a research prototype.

The file format, schema, and API may still change. Packages created with this version should be treated as experimental unless a migration strategy is added.
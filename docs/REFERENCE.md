# USAP — Urban Semantic Annotation Package

USAP is a prototype Python package and SQLite/GeoPackage-style data model for storing **semantic annotations over urban 3D assets**.

The current prototype bundles adapters for:

- **LAS/LAZ point clouds** using stable point indices.
- **Mesh files** (`.obj`/`.ply`/`.stl`) using stable face indices.
- **CityGML semantic objects** using imported `gml:id` / object identifiers.

However, any asset with stable integer-indexed elements of one of the four kinds — point, face, vertex, or feature (`feature` is declared but not yet exercised) — can be declared directly via `register_asset` + `register_asset_part`, without an adapter.

USAP is designed for concepts from:

- **CityGML standard concepts** loaded from an external vocabulary registry.
- **ADE-like/custom concepts** loaded from an external vocabulary registry.
- **minimal local schemes** (names only, optional parents) for exploratory
  work without an ontology.

USAP does **not** copy geometry into the package. Instead, it stores references to external assets and compact membership blocks that identify which points, faces, or elements are annotated.

The motivation and mental model are in [README.md](../README.md); this file is the reference manual.

---

## Project status

This repository contains a working MVP. The file format, schema, and API may still change, and packages created with this version should be treated as experimental.

What the prototype can do, end to end:

```text
Given:
  a CityGML file (or a minimal vocabulary — INGESTION.md procedure 2),
  at least one 3D asset representing (at least one of) the city objects listed in the CityGML file,

USAP can:
  create a package,
  register all assets,
  import CityGML semantic objects,
  load accepted concepts,
  apply JSON annotation batches,
  attach annotations to 3D assets,
  link annotations to city objects,
  query annotations from selected elements,
  validate package integrity.
```

---

## Repository structure

```text
usap_prot/
  pyproject.toml
  README.md  INGESTION.md  REFERENCE.md  TESTS.md

  src/usap/             the Python SDK: core, validation, geopackage,
                        domain_vocab, batch, project_builder, synthetic
    adapters/           LAS / mesh / CityGML adapters
    data/schema.sql     the package schema (USAP tables + GeoPackage
                        metadata tables + GIS views)
    data/vocabularies/  example concept registries (CityGML 3.0 MVP subset,
                        ADE prototype, minimal local scheme)
  examples/             runnable CLI scripts: build, validate, smoke test,
                        batch apply, demos
  project_configs/      example_project.json — template project config
  scripts/              synthetic benchmark + profiling
  tests/                pytest suite — every test described in TESTS.md
```

---

## Installation

We suggest creating a virtual environment. Install the package in editable
mode:

```bash
python -m pip install -e .
```

LAS is supported by the base install; LAZ and CRS parsing each need a backend
of their own, so they are optional extras:

```bash
python -m pip install -e ".[laz,crs]"
```

Without them, a `.laz` file fails to open and reading a CRS raises a
capability error — neither degrades silently into "this file has no CRS".

Run the test suite (described in [TESTS.md](TESTS.md)):

```bash
python -m pytest
```

---

## Key concepts

### 3D Asset

An external file registered in USAP, like a LAS/LAZ point cloud or an OBJ/PLY/STL mesh. USAP stores the path, kind, media type, optional content hash, and metadata.

### 3D Asset part

A stable indexable part of an asset. Each part stores its `element_kind` (point or face) and its `element_count` (number of points or faces). Element indices into a part are the coordinate system annotations live in.

### Semantic class / concept

A registered concept accepted by USAP, e.g. `RoofSurface`, `Window`, `EnergyRoof`. Concepts come from a CityGML registry, an ADE/custom registry, or a minimal local scheme (see "Concept registries").

### City object

A semantic object, usually imported from CityGML, e.g. `building_1`, `building_1_roof_1`, `building_1_window_1`.

City objects are wired to each other by **typed** edges in
`usap_city_object_relationship` (`link_city_objects`), grouped into named graphs
(`usap_default` is the one queries traverse). "This object **and its parts**" —
what `elements_for_city_object` and `list_city_objects(descendants_of=...)`
expand — follows only *containment* edge types:

```text
contains · consistsOf · boundedBy · opening      (CONTAINMENT_RELATIONSHIP_TYPES)
```

Any other type (`adjacentTo`, `connectedTo`, your own) relates two objects
without making one a part of the other, so it is not traversed unless you pass
`containment_types=(...)` explicitly. Containment must be acyclic — a cycle
would make an object its own part, and `validate_report()` flags it as
`CITY_OBJECT_GRAPH_CYCLE`.

Descendants are walked from the edges on every query; USAP stores no
precomputed object closure, so an object never has to be "rebuilt into" the
hierarchy and one created with no edges at all still answers for itself.

### Annotation

A semantic claim linked to one concept and optionally to one city object.

The primary city object is stored both as `usap_annotation.primary_city_object_id` and
as a `represents` row in `usap_annotation_object`; the SDK keeps the two in step, and
`validate_report()` reports any disagreement. Other rows in `usap_annotation_object`
carry secondary links (`concerns`, `derivedFrom`, …); city-object queries follow
`represents` links only unless asked otherwise
(`elements_for_city_object(..., link_types=(...))`).

**What belongs in USAP vs the semantic source.** USAP is authoritative for the *claim layer*: which elements, under which concept, with what status, confidence, and provenance. The CityGML/ADE (or other semantic source) is authoritative for the *meaning layer*: which concepts and objects exist, their
properties, and their hierarchy. Accordingly, an annotation's `attributes` must hold **claim-level metadata only** — how/when/by what the claim was produced (`method`, `source`, `assessed_at`, and for value fields `unit`, `validAt`).
Object properties (e.g., roof slope) stay in the semantic source, reachable through the linked city object, so there is exactly one authority for them and nothing to keep synchronized.

Example:

```text
annotation_uid: ann_energy_roof_001
concept: EnergyRoof
primary city object: building_1_roof_1
attributes: claim metadata (method, source, assessed_at)
```

### Membership block

A compressed set of selected element indices for one annotation, one asset part, and one element kind.

Example:

```text
annotation ann_energy_roof_001
asset part area.las points/all
selected point indices [100, 101, 102]
```

### Value block (annotation on a whole 3D asset)

A compressed dense array of per-element scalar values for one annotation and one asset part: element *i*'s value is `decoded[i - block_start]`. Membership stores *which* elements are a concept; value blocks store the *value* of a property at each element (e.g. shadow fraction per face). Rule of thumb: booleans and categories are **sets** (native membership, like "shadowed at 14:00", is just a concept plus the shadowed faces); reach for a value field only for genuinely **continuous** values. Value fields are bound to the geometry asset only, never to a city object, and must cover every element of the part (v1; NaN = "no value" in float fields). Stored little-endian, dtype per block (`f4` default; see `VALUE_DTYPES`), with per-block min/max for decode-free stats and query pruning.

Example:

```text
annotation ann_shadow_1400 (concept ShadowFraction, no city object)
asset part area_lod2.obj geometry/0
values float32 [0.0, 0.73, 0.5, ...]   one per face
```

---

## Concept registries

USAP ships **no** built-in taxonomy and does not enforce one. A new package starts with **zero** concepts; it holds only whatever vocabulary you seed into it.

**Register a concept before you annotate with it.** Every annotation references exactly one concept so the concept must already exist in the package, carrying at least its minimal identity:

- `scheme` — the vocabulary/namespace it belongs to
- `local_name` — its label
- `class_uri` — its globally-unique identifier (optional: derived as
  `scheme:local_name` when omitted)
- *(optional)* `parent_uri` (parent concept, for hierarchy), `scheme_version`, `is_ade`

Annotations then **reference** the registered concept; they do not re-describe it.

**The vocabulary format is the contract.** Concepts are loaded with `seed_vocabulary_file()`, which expects a JSON registry of this minimal shape (one file per scheme; list parents before children):

```json
{
  "scheme": "citygml",
  "scheme_version": "3.0",
  "is_ade": false,
  "concepts": [
    { "local_name": "AbstractThematicSurface",
      "class_uri": "citygml-3.0:construction:AbstractThematicSurface" },
    { "local_name": "RoofSurface",
      "class_uri": "citygml-3.0:building:RoofSurface",
      "parent_uri": "citygml-3.0:construction:AbstractThematicSurface" }
  ]
}
```

Required: top-level `scheme`; `concepts[]` each with `local_name`. Optional: `class_uri` (derived as `scheme:local_name` when omitted — recommended explicit for ontology-backed schemes), `parent_uri` (accepts a `class_uri` **or** the local name of an already-registered concept, resolved within the same scheme first), `scheme_version`, `is_ade`. **Any other keys are ignored.** This minimal shape is intentionally a thin JSON form of a **SKOS** concept scheme (`class_uri` → concept IRI, `local_name` →
`skos:prefLabel`, `parent_uri` → `skos:broader`, `scheme` → `skos:ConceptScheme`). Richer per-concept metadata (definitions, units, properties) is deliberately out of scope — that is the *application schema* (e.g. a CityGML ADE XSD / SHACL), not the concept scheme.

If your authoritative taxonomy lives in another format (a CityGML ADE registry, an XSD, OWL/SKOS, ...), write a small adapter that emits the registry shape above, then load it. USAP intentionally does **not**
bundle adapters for foreign formats — they are source-specific and belong in your project.

WIP: ingestion of ontologies in OWL format.

### Example registries

The files under `src/usap/data/vocabularies/` ship with the package but are
**examples only**, not a built-in taxonomy — a new package starts with zero
concepts and seeds only what you ask for:

```text
src/usap/data/vocabularies/citygml_3_0_mvp.json
src/usap/data/vocabularies/usap_ade_prototype.json
src/usap/data/vocabularies/local_minimal_example.json
```

The first two are reachable in code as `DEFAULT_CITYGML_VOCABULARY_PATH` /
`DEFAULT_ADE_VOCABULARY_PATH` (`usap.domain_vocab`), so config files need not
name them by path.

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

To consult the list of accepted concepts in a `.usap.gpkg` (without `--db`
the script builds a throwaway demo package instead):

```bash
python examples/list_concepts_demo.py --db my_area.usap.gpkg
python examples/list_concepts_demo.py --db my_area.usap.gpkg --search Roof
python examples/list_concepts_demo.py --db my_area.usap.gpkg --used
```

In Python:

```python
from usap import USAPPackage, seed_default_citygml_vocabulary, seed_default_ade_vocabulary

with USAPPackage.create("concepts.usap.gpkg", overwrite=True) as pkg:
    seed_default_citygml_vocabulary(pkg)
    seed_default_ade_vocabulary(pkg)

    for concept in pkg.list_accepted_concepts(search="Roof"):
        print(concept["local_name"], concept["class_uri"], concept["in_use"])
```

### Minimal vocabulary without an ontology

When no CityGML registry or ontology is provided, a vocabulary file only needs a `scheme` and concept `local_name`s — `class_uri` is derived as `scheme:local_name` when omitted, and `parent_uri` accepts either a `class_uri` or the local name of an already-registered concept (same-scheme resolution first). Declare just the names you need under a temporary scheme and annotate right away:

```json
{
  "scheme": "local",
  "concepts": [
    { "local_name": "TempRoof" },
    { "local_name": "TempChimney", "parent_uri": "TempRoof" }
  ]
}
```

See [`src/usap/data/vocabularies/local_minimal_example.json`](../src/usap/data/vocabularies/local_minimal_example.json).
Re-loading an updated copy is additive and idempotent; changing an existing concept's parent raises. Concepts declared this way stay identifiable by their scheme (`list_accepted_concepts(scheme="local")`), so annotations made this way can later be aligned to a full ontology-based package (the latter is WIP).

---

## Build a real project package

> The three supported ingestion/editing procedures (CityGML init,
> minimal-vocabulary init, editing) are documented end to end in
> [INGESTION.md](INGESTION.md). This section describes the config keys.

One example config is provided —
[`project_configs/example_project.json`](project_configs/example_project.json),
a generic template: edit its `../data/area.*` paths to point at your own data
files (paths are resolved relative to the config file; the data files
themselves are not committed to git). Abridged here to one of its three
meshes:

```json
{
  "db_path": "../outputs/example_project.usap.gpkg",
  "manifest_path": "../outputs/example_project_manifest.json",

  "citygml": {
    "path": "../data/area.gml",
    "graph_name": "citygml_import",
    "also_usap_default": true,
    "compute_hash": true
  },

  "las": [
    {
      "path": "../data/area.las",
      "part_path": "points/all",
      "compute_hash": true
    }
  ],

  "meshes": [
    {
      "path": "../data/area_lod2.obj",
      "uri": "area_lod2",
      "representation_name": "buildings_lod2",
      "representation_kind": "building_mesh",
      "lod": "LoD2",
      "compute_hash": true
    }
  ],

  "annotation_batches": [
    "../examples/batches/example_annotation_batch.json"
  ]
}
```

Key notes:

- `"annotation_batches"` — batch files applied right after the assets are
  registered, so one build call ingests the annotations too.
- an asset's optional `"uri"` (a stable logical name, `area_lod2` above) lets
  batch memberships reference parts as `"asset_uri": "<that name>"` instead of
  the numeric `asset_part_id` from the manifest.
- `"srs_id": 25833` (+ optional `"srs_wkt"`) — declares the package CRS for the
  GIS extent layer (one CRS per package). Without it, an EPSG code found in the
  LAS files' CRS WKT is promoted automatically when they all agree; otherwise
  the layer stays in the undefined SRS (−1), which is honest for
  local-coordinate meshes.
- `"schema_path"` / `"vocabularies"` are optional; both default to the files
  shipped inside the package.
- `"validation_level"` — the level the build validates at before committing
  (`deep` by default; see Validation).
- the whole build is **one transaction**: on failure a fresh build leaves no
  package behind at all, and an `update=True` run leaves the previous package
  exactly as it was.

Run:

```bash
python examples/build_project_package.py project_configs/example_project.json
```

Expected outputs:

```text
outputs/example_project.usap.gpkg
outputs/example_project_manifest.json
```

The manifest lists each part's `asset_part_id` (usable in batches as an
alternative to `asset_uri`).

To apply a config to an **existing** package — add assets, apply editing
batches — pass `--update` (Python: `build_project_package_from_file(path,
update=True)`). Registration is idempotent, and in update mode batches run
with `replace_existing=True`.

To see which concepts a built package accepts — optionally filtered — point
the concept lister at it (each line shows local name, CityGML vs ADE/custom,
`used:N`/`unused`, scheme, and full `class_uri`):

```bash
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg --search Roof
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg --used
```

---

## Opening a package in QGIS

A `.usap.gpkg` is a valid GeoPackage. Adding it to QGIS (or listing it with `ogrinfo`) shows four read-only layers:

```text
usap_annotations_view    attributes   annotations with concept, city object,
                                      status, and element counts
usap_concepts_view       attributes   accepted concept registry + usage
usap_city_objects_view   attributes   city objects (carriers show
                                      object_status = 'temporary')
usap_asset_extents       features     one derived 2D bounding box per
                                      registered asset
```

Notes:

- The extent boxes are **derived summaries** (the union of each asset's part
  bounds, captured at registration) — never actual geometry. They are written
  automatically, kept by `ON DELETE CASCADE`, and checked by `validate_report()`
  against the part bounds.
- The layer CRS defaults to the undefined SRS (−1) until declared — see the
  `"srs_id"` config key above or `set_package_srs(pkg.conn, epsg)` from Python
  (safe before or after registration; existing boxes are re-encoded).
- Declaring a CRS **requires its definition**: pass `"srs_wkt"` alongside
  `"srs_id"`, or install `usap[crs]` so the WKT can be looked up from the EPSG
  code. GeoPackage reserves "undefined" definitions for the built-in SRS ids
  −1 and 0 and requires a record defining every SRS a package actually uses.
- `set_package_srs` re-stamps the CRS id on the stored extent boxes but does
  **not** transform coordinates: USAP assumes one CRS per package. Assets
  registered in different CRSs are reported as a `MIXED_ASSET_CRS` warning
  rather than silently misplaced.
- Each view exposes its key as `OGC_FID`, the alias GDAL recognises as a
  view's feature id (a column merely called `fid` is carried as an ordinary
  attribute, so features got GDAL's own row numbers instead of USAP ids), and
  aggregate columns are `CAST` to INTEGER so they are not inferred as strings.
- Fine-grained content — element memberships, value fields — is not exposed as
  layers; use the Python API for those.
- USAP registers itself in `gpkg_extensions` as an Extended GeoPackage. The
  `definition` column currently holds a **placeholder** URI
  (`https://usap.invalid/...`) until the extension document has a public home
  — the file is readable everywhere, but the registration is not yet a
  resolvable reference.

---

## Run an end-to-end smoke test

After building a project package:

```bash
python examples/smoke_test_project_package.py \
  outputs/example_project.usap.gpkg \
  outputs/example_project_manifest.json \
  --batch-out outputs/smoke_batch.json
```

This creates one annotation, attaches it to:

```text
one CityGML city object
one LAS point asset part
one mesh face asset part
```

and then queries it back from both a selected LAS point and a selected mesh face.

Useful flags:

- `--replace-existing` — rerun and replace the same smoke annotation.
- `--city-object-uid YOUR_GML_ID` — use a specific CityGML object.
- `--mesh-representation-name buildings_lod2` — use a specific mesh
  representation.

---

## Reproduce the benchmark

The synthetic benchmark measures build and query performance on a generated
city — the evidence behind the project's performance claim (no external data
needed; run from the repo root):

```bash
python scripts/benchmark_phase1.py --buildings 1000 --repeat 5 --md bench.md
```

It generates a synthetic city (`create_synthetic_package` / `SyntheticConfig`,
also runnable standalone via `examples/build_synthetic.py`), times the build
and the core queries, validates the package, and writes the report to the
`--md`/`--json` paths. `--schema` defaults to the packaged schema; sizes are
tunable with `--buildings`, `--roof-faces`, `--wall-faces`, `--ground-faces`.

---

## Batch annotation format

Batch annotations (the "linking JSON" of [INGESTION.md](INGESTION.md)) are
JSON files. Several fields are derivable, so the minimal entry is just
*object + elements* (procedure 1) or *object + concept + elements*
(procedure 2):

- `concept` — optional when the linked city object already has a class
  (inherited from it); required otherwise, and when creating carriers.
- `annotation_uid` — optional when a city object is linked; derived as
  `ann_{object_uid}_{concept_local_name}` (stable across re-runs, so
  re-applying with `--replace-existing` edits in place).
- `element_kind` — optional; defaults to the asset part's stored kind.
- parts are referenced by `asset_part_id` (int) **or** `asset_uri`
  (+ `part_path` when the asset has several parts) — exactly one of the two.
- top-level `"create_missing_city_objects": true` (minimal-vocabulary
  procedure only) lets unknown `city_object_uid`s create carrier city
  objects: classed by the entry's concept, `object_status='temporary'`
  (the marker for later CityGML alignment), nothing else. Without the flag,
  unknown names fail loudly.

Full-form example:

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
        "method": "roof_detector_v2",
        "source": "survey_2026_06",
        "assessed_at": "2026-06-30T14:00:00Z"
      },
      "memberships": [
        {
          "asset_part_id": 2,
          "element_kind": "point",
          "element_indices": [100, 101, 102]
        },
        {
          "asset_uri": "area_lod2",
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
  outputs/example_project.usap.gpkg \
  path/to/batch.json
```

Replace an existing annotation with the same `annotation_uid`:

```bash
python examples/apply_annotation_batch.py \
  outputs/example_project.usap.gpkg \
  path/to/batch.json \
  --replace-existing
```

An annotation may carry `"value_fields"` instead of (or alongside) `"memberships"` —
at least one of the two is required. Values are listed inline, one per element of the
asset part, with JSON `null` meaning "no value" (stored as NaN; float dtypes only):

```json
{
  "annotations": [
    {
      "annotation_uid": "ann_shadow_1400",
      "concept": "ShadowFraction",
      "attributes": { "validAt": "2026-06-21T14:00:00Z", "unit": "fraction" },
      "value_fields": [
        {
          "asset_part_id": 3,
          "element_kind": "face",
          "values": [0.0, 0.73, null, 0.5],
          "value_dtype": "f4"
        }
      ]
    }
  ]
}
```

The batch importer validates:

```text
concept is registered (or inheritable from the linked object's class)
city object exists (unless create_missing_city_objects is set)
asset part exists and the asset_uri reference is unambiguous
element kind matches asset part (when given; defaulted otherwise)
element indices are in range
value fields cover the whole asset part; dtype is supported
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

with USAPPackage.create("demo.usap.gpkg", overwrite=True) as pkg:
    seed_default_citygml_vocabulary(pkg)
    seed_default_ade_vocabulary(pkg)
```

(`schema_path` defaults to the schema shipped inside the package, so this works
from a plain wheel install; pass it explicitly only to use a modified schema.)

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
        "method": "manual_selection",
        "assessed_at": "2026-06-30T14:00:00Z",
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

# Moving an annotation to another city object rewrites its 'represents' link
# in the same transaction, so it stops answering queries for the old object.
pkg.update_annotation(
    annotation["annotation_id"],
    primary_city_object_id=pkg.resolve_city_object("building_1_roof_2"),
)

pkg.delete_annotation(annotation["annotation_id"])
```

### Per-element value fields

```python
import numpy as np

# one value per face of the asset part; NaN = "no value"
shadow = np.clip(np.random.default_rng(0).normal(0.4, 0.2, mesh_face_count), 0, 1)

ann = pkg.annotate_value_field(
    concept="ShadowFraction",          # any registered concept: CityGML, ADE, or local
    asset_part_id=mesh_part_id,
    element_kind="face",
    values=shadow,                     # dtype: explicit > whitelisted ndarray dtype > 'f4'
    attributes={"validAt": "2026-06-21T14:00:00Z", "unit": "fraction"},
)
annotation_id = ann["annotation_id"]   # primary_city_object_id is always NULL

values = pkg.values_for_annotation(annotation_id)          # full dense array back
faces = pkg.elements_where(annotation_id, (">", 0.5))      # sorted face-index set
dim = pkg.elements_where(annotation_id, lambda v: (v > 0.2) & (v < 0.8))
stats = pkg.value_field_stats(annotation_id)               # min/max/count, no decode

# editing is whole-field rewrite (the exception path)
pkg.replace_value_field(annotation_id, mesh_part_id, "face", corrected_values)
```

---

## Element kinds

The user-facing API accepts readable element-kind names — `point`/`points`,
`face`/`faces`, `triangle`/`triangles` — and normalizes them to USAP
constants internally, so `element_kind="point"` and
`element_kind=ELEMENT_KIND_POINT` are equivalent.

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

**What counts as CityGML.** Elements become city objects only when their
namespace is a CityGML one (`*opengis.net/citygml*`, any module, versions
1.0/2.0/3.0) — an element merely *named* `Building` in some other vocabulary
is skipped, and a document declaring no CityGML namespace at all is refused
rather than imported as zero objects. Likewise, only a real `gml:id`
(`*opengis.net/gml*`) is adopted as object identity; an `id` attribute from
another namespace is ignored and a generated uid is used instead.

> **Known limitation — version provenance.** The detected CityGML version is
> recorded in the asset metadata (`citygml_version_hint`), but concepts always
> come from the shipped CityGML **3.0** vocabulary, so a 2.0 `Building` is
> registered under a `citygml-3.0:` class URI with `scheme_version` 3.0. This
> is pending the vocabulary-ingestion rework; until then, treat the class URI
> of a non-3.0 import as approximate.

---

## Mesh support

The mesh adapter reads **`.obj`, `.ply` and `.stl`** and is not limited to
LoD1/LoD2: building meshes, city triangulations, TIN terrain, photogrammetry
or simulation meshes all register the same way. Two conditions:

- **Face indices must remain stable for the registered file version.** If a
  mesh is remeshed, simplified, reordered, or re-exported in a way that
  changes face order, register it as a new asset.
- **Vertices must already be in their final coordinates.** The adapter reads
  geometries, not scenes.

`.glb`/`.gltf` are therefore **refused** rather than partially supported: a
glTF scene positions its geometries with node transforms and may instance one
geometry at several nodes, so reading the geometries alone yields bounds in
the wrong place and one asset part where the scene has several instances —
wrong data rather than missing data. Export with transforms baked in, or wait
for scene-graph support (see the future-work notes).

### Large meshes

Registration is the **only** step that opens a mesh file. Everything after it
— annotating, editing, querying — works from the element count stored on the
asset part, so a 10 GB mesh is no more expensive to annotate than a 10 MB one.

Registration itself needs just two facts (face count, bounding box), and
loading a whole mesh to get them does not survive a large file. Files over
**256 MB** are therefore read in a streaming pass instead; `stream=True` /
`stream=False` overrides the threshold:

```python
register_mesh_asset(pkg, "city.ply", representation_name="city")               # auto
register_mesh_asset(pkg, "city.ply", representation_name="city", stream=True)  # forced
```

Measured on a 174 MB binary PLY (6 M faces), both paths recording identical
face counts and bounds:

| | time | peak RSS |
|---|---:|---:|
| `stream=False` (trimesh load) | 2.40 s | 758 MB |
| `stream=True` | 0.68 s | 121 MB |

Streaming readers exist for **`.ply`** (ASCII and binary) and **`.obj`** only,
and treat one file as one part. Two cases raise instead of guessing:

- an OBJ carrying several `o`/`g` groups — a normal load would split it into
  one part per group, and annotations bind to part names, so the same file
  must not register differently depending on its size;
- any other format (`.stl`) — no streaming reader, so load it in full.

`compute_hash=True` (the default) re-reads the whole file to SHA-256 it, which
is minutes on a 10 GB asset. It is what makes a later change to that file
detectable (`validate_report(level="external")`), so skip it deliberately, not
by accident.

---

## Validation

Validate a package in Python:

```python
report = pkg.validate_report()          # level="deep" by default
report.print()
```

Or run an example validator:

```bash
python examples/validate_package.py outputs/example_project.usap.gpkg
python examples/validate_package.py outputs/example_project.usap.gpkg --level external
```

### Levels

Validation costs scale with what it is allowed to read, so it comes in three
levels. `report.is_ok` always means "no errors **at the level you asked
for**" — a clean `basic` report is not a claim about payloads, and a clean
`deep` report is not a claim about the files on disk.

| Level | Reads | Use it for |
|---|---|---|
| `basic` | SQL columns only — never a block payload | packages with millions of membership blocks, or a fast pre-flight |
| `deep` *(default)* | + every membership/value payload | the normal correctness check |
| `external` | + every registered asset file (SHA-256) | before trusting a package whose sources may have moved or changed |

```text
basic     GeoPackage metadata and registered layers
          USAP profile presence
          orphan references
          membership/value block structure (counts, bounds, element kinds)
          semantic class closure
          concept registry duplicates
          annotation primary object / 'represents' link agreement
          duplicate relationship edges (warning)

deep      + membership payload decoding, offsets, stored min/max agreement
          + value payload decoding and stored min/max agreement
          + asset extent recomputation
          + city object containment cycles
          + annotation status / confidence / attributes-JSON domain values

external  + asset file exists            (ASSET_FILE_MISSING)
          + asset file hash unchanged    (ASSET_FILE_CHANGED)
          + asset registered unhashed    (ASSET_NOT_HASHED, warning)
```

`verify_assets(pkg.conn)` runs the external check on its own and returns one
record per asset (`ok` / `missing` / `changed` / `unhashed`) instead of a
report. Note what this does **not** do: USAP can detect that a file changed,
but it cannot rebind annotations to it — element indices into the new file
may mean something entirely different. Re-register the new version as its own
asset.

`build_project_package` validates at `deep` before committing; set
`"validation_level"` in the project config to change that.

### Domain values

`status`, `object_status` and `confidence` are constrained, on write
(`create_annotation`/`update_annotation`/`create_city_object` raise) and at
the `deep` validation level for packages written another way:

```text
annotation status    draft | accepted | rejected | superseded
city object status   accepted | temporary
confidence           NULL, or a number in [0, 1]
attributes_json      NULL, or text that parses as JSON
```
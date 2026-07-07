# USAP — Urban Semantic Annotation Package

USAP is a research prototype: a small SQLite/GeoPackage-style file format
(`*.usap.gpkg`) and Python package for storing **editable, element-level
semantic annotations over external 3D urban data assets** — without copying any
geometry.

This is an MVP whose purpose is to test whether the idea is *usable*, not to be
a finished product. 

---

## The problem it addresses

Urban analysis (energy and emissions, soil permeability, acoustic comfort,
visual wellbeing) increasingly works from the same area captured in several
forms at once:

- a semantic city model (CityGML / CityJSON),
- one or more meshes (LoD1/LoD2, photogrammetry, triangulated terrain),
- a LAS/LAZ point cloud.

There is no lightweight, editable place to record a claim like *"this roof — in
the CityGML model, in the LoD2 mesh, and in the point cloud — is an `BuildingRoof`
with these attributes"* down to the exact points and faces, and then query it
back from a selection. Each format keeps semantics in its own silo (CityGML
objects, 3D Tiles feature IDs, the LAS classification byte) and none of them
references the others.

USAP is an attempt to fill that gap.

---

## What USAP is

A `*.usap.gpkg` file stores, for one study area:

- **references** to the external assets (it never copies their geometry);
- the exact **element indices** an annotation covers — which points, which faces
  — as compressed *membership blocks*;
- the **semantic concept** of each annotation (e.g. `RoofSurface`, `EnergyRoof`),
  drawn from external vocabulary registries;
- editable **annotation records** with label, status, confidence, attributes
  (attributes hold *claim-level* metadata only — method, source, timestamps;
  object properties stay in the semantic source, e.g. CityGML/ADE, so there
  is exactly one authority for them);
- optional **per-element value fields** — a dense scalar per point/face (e.g.
  shadow fraction per face at hour H), stored as compressed typed blocks and
  queryable by value;
- a lightweight mirror of **city-object identity** and a typed **relationship
  graph** — used to retrieve annotations across an object and its parts (e.g. a
  building together with its roof and walls), not as a general-purpose graph
  store — optionally imported from CityGML.

A single annotation can therefore span a CityGML object, LAS points, and mesh
faces of the same physical thing.

So USAP is really **two things at once**:

1. an **annotation index** — which elements mean what, and
2. a **lightweight semantic-object store** — stable object identities and the
   typed relationship graph that annotation queries traverse (optionally
   mirrored from CityGML). The graph is a query accelerator for the annotation
   index above, not an independent city-model database.

It is not a 3D city model, a geometry store, or a replacement for
CityGML, CityJSON, 3DCityDB, 3D Tiles, or GeoPackage. When a CityGML source
exists, it stays the semantic authority and USAP is the editable query/annotation
layer on top of it.

---

## Mental model

```text
external asset            area.las  /  area.obj  /  area.gml
  └─ stable asset part    points/all , a mesh primitive
       └─ exact elements  points 100,101,102 ; faces 40,41,42
            └─ annotation  editable claim: concept + attributes + status
                 └─ concept       EnergyRoof  (CityGML / ADE registry)
                      └─ city object  building_1_roof_1
```

One annotation, many representations:

```text
annotation: ann_energy_roof_001
  concept: EnergyRoof
  primary CityGML object: building_1_roof_1
  memberships:
    LAS points:        [100, 101, 102]
    LoD2 mesh faces:   [40, 41, 42]
    triangulation:     [800, 801]
```

---

## Concepts & vocabularies

USAP ships **no** built-in taxonomy and does not enforce one. A new package starts with
**zero** concepts; it holds only whatever vocabulary you seed into it.

**Register a concept before you annotate with it.** Every annotation references exactly one
concept — `usap_annotation.semantic_class_id` is `NOT NULL` with a foreign key into
`usap_semantic_class` — so the concept must already exist in the package, carrying at least
its minimal identity:

- `scheme` — the vocabulary/namespace it belongs to
- `local_name` — its label
- `class_uri` — its globally-unique identifier (optional: derived as
  `scheme:local_name` when omitted)
- *(optional)* `parent_uri` (parent concept, for hierarchy), `scheme_version`, `is_ade`

Annotations then **reference** the registered concept; they do not re-describe it.

**The vocabulary format is the contract — not the example files.** Concepts are loaded with
`seed_vocabulary_file()`, which expects a JSON registry of this minimal shape (one file per
scheme; list parents before children):

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

Required: top-level `scheme`; `concepts[]` each with `local_name`. Optional: `class_uri`
(derived as `scheme:local_name` when omitted — recommended explicit for ontology-backed
schemes), `parent_uri` (accepts a `class_uri` **or** the local name of an
already-registered concept, resolved within the same scheme first), `scheme_version`,
`is_ade`. **Any other keys are ignored.** This minimal shape is
intentionally a thin JSON form of a **SKOS** concept scheme (`class_uri` → concept IRI,
`local_name` → `skos:prefLabel`, `parent_uri` → `skos:broader`, `scheme` →
`skos:ConceptScheme`). Richer per-concept metadata (definitions, units, properties) is
deliberately out of scope — that is the *application schema* (e.g. a CityGML ADE XSD / SHACL),
not the concept scheme.

**No ontology yet? Use a minimal local scheme.** If no CityGML+ADE registry or ontology is
available, declare just the names you need (plus optional parent-child links) under a
temporary scheme and annotate right away — hierarchy-accelerated queries work the same:

```json
{
  "scheme": "local",
  "concepts": [
    { "local_name": "TempRoof" },
    { "local_name": "TempChimney", "parent_uri": "TempRoof" }
  ]
}
```

See [`vocabularies/local_minimal_example.json`](vocabularies/local_minimal_example.json).
Re-loading an updated copy of the file is additive and idempotent (changing an existing
concept's parent raises). Local concepts stay identifiable by their scheme
(`list_accepted_concepts(scheme="local")`), so annotations made this way can later be
aligned to a full ontology-based package — that transfer tooling is future work.

**Bring your own adapter for other formats.** If your authoritative taxonomy lives in another
format (a CityGML ADE registry, an XSD, OWL/SKOS, ...), write a small adapter that emits the
registry shape above, then load it. USAP intentionally does **not** bundle adapters for foreign
formats — they are source-specific and belong in your project. The files under `vocabularies/`
are **examples only**, not a built-in taxonomy.

---

## Per-element value fields

Besides membership sets ("these faces *are* a RoofSurface"), an annotation can carry a
**value field**: one scalar per element of an asset part — e.g. the shadow fraction of
every mesh face at 14:00. Rule of thumb: booleans and categories are **sets** (native
membership — "shadowed at 14:00" is just a concept plus the shadowed faces); reach for a
value field only for genuinely **continuous** values.

A value field is a property of the *geometry asset*: it binds a registered concept
(CityGML, ADE, or a minimal local scheme) to the elements of one asset part, and never
references a city object. Field metadata (unit, timestamp, method) lives in the
annotation's attributes; many timesteps = many annotations, one per (field, timestep).

```python
ann = pkg.annotate_value_field(
    concept="ShadowFraction",              # must be registered, like any annotation
    asset_part_id=mesh_part_id,
    element_kind="face",
    values=shadow,                         # one value per face; NaN = "no value"
    attributes={"validAt": "2026-06-21T14:00:00Z", "unit": "fraction"},
)

faces = pkg.elements_where(ann["annotation_id"], (">", 0.5))
# -> sorted face indices, the same shape as a membership query result
```

Values are stored as zlib-compressed little-endian typed blocks (`f4` by default, see
`VALUE_DTYPES`), must cover every element of the part (v1 contract), and are edited by
whole-field rewrite. Casting is strict: an explicit dtype must represent the data exactly
(out-of-range or non-integral values for an integer dtype raise instead of silently
wrapping/truncating; only float precision rounding is allowed). Annotation batches can
carry them too (`"value_fields"`, with JSON `null` as "no value"). See
[VALUE_FIELDS_DESIGN.md](VALUE_FIELDS_DESIGN.md) for the full design.

---

## How it relates to existing work

To be clear, USAP is not a new city-model standard. It overlaps with, and deliberately
defers to, several existing technologies:

| Existing technology | What it does | What USAP adds / why not just use it |
|---|---|---|
| **CityGML / CityJSON / 3DCityDB** | Full semantic 3D city model (geometry **and** semantics together) | USAP is an *overlay*, not a city model. It mirrors only identity/class/relationships and leaves geometry and full attributes in the source. |
| **3D Tiles + glTF `EXT_mesh_features` / `EXT_structural_metadata`** | Feature IDs and metadata attached to mesh features | The closest analog for *mesh* semantics. USAP differs by being an editable working file, not embedded in the tiles, and by spanning multiple representations of one object. |
| **LAS ASPRS `classification` + extra dimensions** | A semantic class stored *inside* the point cloud | The closest analog for *point* semantics. USAP differs by supporting arbitrary vocabularies, cross-asset links, and editing without rewriting a large file. |
| **GeoPackage** | OGC SQLite container | Used as the container. USAP currently uses only minimal GeoPackage metadata (see Limitations). |
| **W3C Web Annotation Data Model** | Generic `target + selector + body` annotations | The conceptual ancestor. USAP is essentially this pattern specialized to 3D urban data, with the "selector" being element indices inside an asset part. |

The novel combination USAP is testing is: **element-level, editable,
cross-representation** semantic annotation in a single lightweight file.

---

## Status

This is a working **MVP**. It can, end to end:  create a package · register LAS/LAZ, mesh, and CityGML assets · import CityGML semantic objects and relationships · load CityGML/ADE concept registries · apply JSON annotation batches · attach one annotation to a CityGML object, LAS points, and mesh faces · query annotations back from selected elements · validate package integrity.

A synthetic benchmark (1,000 buildings ≈ 500k faces) builds in ~1 s into a
~3.7 MiB file, and the core queries return in single- to low-double-digit
milliseconds without scanning the whole model. This supports a **narrow** claim. The data model and query design are efficient enough to justify continuing, although they
do **not** yet demonstrate real-world interoperability.

See [REFERENCE.md](REFERENCE.md) for the full feature list.

---

## Limitations

- **Stable element identity is the load-bearing assumption.** An annotation is
  bound to *one immutable version* of an external file. LAS point order and mesh
  face order are **not** guaranteed to survive reprojection, re-tiling, thinning,
  remeshing, re-export, or conversion to COPC (which re-orders points by design).
  USAP records a content hash to *detect* that a file changed, but it cannot
  *rebind* annotations — if a source file legitimately changes, its annotations
  must be treated as stale. There is no spatial/geometric re-binding yet.
- **Membership encoding is deliberately simple** (sorted `uint32` offsets + zlib,
  in blocks). A production system would likely use *roaring bitmaps*; this is a
  simple-first, dependency-light choice. Roaring bitmaps will be adopted in the first actual release.
- **"GeoPackage" is minimal.** The file carries the GeoPackage container magic and
  a few `gpkg_*` tables, but USAP is **not** a registered OGC extension. Generic
  GIS tools may open the container but will not understand USAP semantics. This will be for sure dealt in the first actual release.
- **CityGML import is semantic-only**: identity (`gml:id`), class names, basic
  nesting, and provenance. No geometry import, no full schema validation, no
  xlink resolution, no complete ADE XML interpretation.
- **No spatial index, no viewer/GUI, single-writer SQLite** (a local working
  file, not a multi-user server), and **no migration system**. The file format,
  schema, and API may still change.

---

## Quickstart

Requires Python ≥ 3.11. Dependencies: `numpy`, `laspy`, `lxml`, `trimesh`.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pytest                 # run the test suite
```

### Build a package from a config file (recommended)

The supported creation/ingestion/editing flows are **three explicit
procedures** — init from CityGML, init from a minimal vocabulary (no
ontology), and editing an existing package — documented step by step in
[INGESTION.md](INGESTION.md). In short: describe the study area in **one JSON
config** and run the builder once.

**1. Point a config at your data.** Copy
[`project_configs/example_project.json`](project_configs/example_project.json)
and edit the paths to your own CityGML, point cloud, and meshes. Paths are
resolved relative to the config file:

```jsonc
{
  "db_path": "../outputs/my_area.usap.gpkg",
  "manifest_path": "../outputs/my_area_manifest.json",
  "schema_path": "../sql/schema.sql",
  "vocabularies": [
    "../vocabularies/citygml_3_0_mvp.json",
    "../vocabularies/usap_ade_prototype.json"
  ],
  "citygml": { "path": "../data/area.gml", "also_usap_default": true },
  "las":     [ { "path": "../data/area.las", "part_path": "points/all" } ],
  "meshes":  [ { "path": "../data/area_lod2.obj", "uri": "area_lod2", "representation_name": "buildings_lod2", "lod": "LoD2" } ],
  "annotation_batches": [ "../batches/my_links.json" ]
}
```

**2. Write the linking JSON** (`my_links.json`) — which city object owns
which elements. The minimal entry is object + elements; `concept`,
`annotation_uid`, and `element_kind` are derived when omitted, and assets are
referenced by the `uri` you gave them (numeric `asset_part_id` from the
manifest also works):

```json
{
  "annotations": [
    {
      "city_object_uid": "building_1_roof_1",
      "memberships": [
        { "asset_uri": "area_lod2", "element_indices": [100, 101, 102] }
      ]
    }
  ]
}
```

**3. Build** (run from the repo root) — registers the assets, imports the
CityGML objects, loads the vocabularies, applies the linking JSON, validates,
and writes the manifest:

```bash
python examples/build_project_package.py project_configs/my_area.json
python examples/validate_package.py     outputs/my_area.usap.gpkg
```

To **edit** an existing package later (add assets, change annotations), point
a config at the same `db_path` and add `--update`; to build **without any
CityGML** using a minimal vocabulary and auto-created carrier objects, see
procedure 2 in [INGESTION.md](INGESTION.md).

**4. See which concepts the package accepts.** USAP only accepts annotations
whose `concept` is in the loaded vocabularies. List them — optionally filtered —
straight from the built package:

```bash
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg --search Roof
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg --used
```

Each line shows the concept's local name, whether it is a standard CityGML or an
ADE/custom concept, whether it is used by annotations in this package (`used:N` /
`unused`; `--used` filters to used ones), its scheme, and its full `class_uri`.

A ready-to-run Catania config
([`project_configs/example_project_catania.json`](project_configs/example_project_catania.json))
is included; supply your own `data/catania.*` files to use it. The full batch
format and every command-line flag are documented in [REFERENCE.md](REFERENCE.md).

### Or drive it from Python

```python
from usap import (
    USAPPackage,
    seed_default_citygml_vocabulary,
    seed_default_ade_vocabulary,
    register_las_asset,
    ELEMENT_KIND_POINT,
)

with USAPPackage.create("demo.usap.gpkg", schema_path="sql/schema.sql", overwrite=True) as pkg:
    seed_default_citygml_vocabulary(pkg)
    seed_default_ade_vocabulary(pkg)

    las = register_las_asset(pkg, "data/area.las")

    pkg.annotate_elements(
        concept="EnergyRoof",
        annotation_uid="ann_energy_roof_001",
        asset_part_id=las.asset_part_id,
        element_kind=ELEMENT_KIND_POINT,
        element_indices=[100, 101, 102],
    )

    for match in pkg.annotations_for_elements(
        asset_part_id=las.asset_part_id, element_kind="point", selected_indices=[101]
    ):
        print(match["annotation_uid"], match["semantic_class"])
```


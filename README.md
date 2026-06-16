# USAP — Urban Semantic Annotation Package

USAP is a research prototype: a small SQLite/GeoPackage-style file format
(`*.usap.gpkg`) and Python package for storing **editable, element-level
semantic annotations over external 3D urban data assets** — without copying any
geometry.

This is an MVP whose purpose is to test whether the idea is *usable*, not to be
a finished product. Read the [Limitations](#limitations--read-this-before-relying-on-it)
section before drawing conclusions from it.

---

## The problem it addresses

Urban analysis — energy and emissions, soil permeability, acoustic comfort,
visual wellbeing — increasingly works from the **same area captured in several
forms at once**:

- a semantic city model (CityGML / CityJSON),
- one or more meshes (LoD1/LoD2, photogrammetry, triangulated terrain),
- a LAS/LAZ point cloud.

There is no lightweight, editable place to record a claim like *"this roof — in
the CityGML model, in the LoD2 mesh, and in the point cloud — is an `EnergyRoof`
with these attributes"* down to the exact points and faces, and then query it
back from a selection. Each format keeps semantics in its own silo (CityGML
objects, 3D Tiles feature IDs, the LAS classification byte) and none of them
references the others.

USAP is an attempt to fill exactly that gap.

---

## What USAP is

A `*.usap.gpkg` file stores, for one study area:

- **references** to the external assets (it never copies their geometry);
- the exact **element indices** an annotation covers — which points, which faces
  — as compressed *membership blocks*;
- the **semantic concept** of each annotation (e.g. `RoofSurface`, `EnergyRoof`),
  drawn from external vocabulary registries;
- editable **annotation records** with label, status, confidence, attributes;
- a lightweight mirror of **city-object identity** and typed **relationship
  graphs**, optionally imported from CityGML.

A single annotation can therefore span a CityGML object, LAS points, and mesh
faces of the same physical thing.

So USAP is really **two things at once**:

1. an **annotation index** — which elements mean what, and
2. a **lightweight semantic-object store** — stable object identities and typed
   relationship graphs (optionally mirrored from CityGML).

It is explicitly **not** a 3D city model, a geometry store, or a replacement for
CityGML, CityJSON, 3DCityDB, 3D Tiles, or GeoPackage. When a CityGML source
exists, it stays the semantic authority; USAP is the editable query/annotation
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

## How it relates to existing work

USAP is not a new city-model standard. It overlaps with, and deliberately
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

This is a working **MVP**. It can, end to end:

create a package · register LAS/LAZ, mesh, and CityGML assets · import CityGML
semantic objects and relationships · load CityGML/ADE concept registries · apply
JSON annotation batches · attach one annotation to a CityGML object, LAS points,
and mesh faces · query annotations back from selected elements · validate package
integrity.

A synthetic benchmark (1,000 buildings ≈ 500k faces) builds in ~1 s into a
~3.7 MiB file, and the core queries return in single- to low-double-digit
milliseconds without scanning the whole model. This supports a **narrow** claim —
the data model and query design are efficient enough to justify continuing — and
does **not** yet demonstrate real-world interoperability.

See [REFERENCE.md](REFERENCE.md) for the full feature list and the
[design roadmap](.chats/USAP_FINAL_DESIGN_ROADMAP.md) for what is intentionally
out of scope for now.

---

## Limitations — read this before relying on it

- **Stable element identity is the load-bearing assumption.** An annotation is
  bound to *one immutable version* of an external file. LAS point order and mesh
  face order are **not** guaranteed to survive reprojection, re-tiling, thinning,
  remeshing, re-export, or conversion to COPC (which re-orders points by design).
  USAP records a content hash to *detect* that a file changed, but it cannot
  *rebind* annotations — if a source file legitimately changes, its annotations
  must be treated as stale. There is no spatial/geometric re-binding yet.
- **Membership encoding is deliberately simple** (sorted `uint32` offsets + zlib,
  in blocks). A production system would likely use roaring bitmaps; this is a
  simple-first, dependency-light choice.
- **"GeoPackage" is minimal.** The file carries the GeoPackage container magic and
  a few `gpkg_*` tables, but USAP is **not** a registered OGC extension. Generic
  GIS tools may open the container but will not understand USAP semantics.
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

A minimal annotation, in Python:

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

To build a package from real study-area data, see
[`examples/build_project_package.py`](examples/build_project_package.py) and the
configs in [`project_configs/`](project_configs/). Full command-line workflows
(project builds, batch annotation, smoke tests, validation) are documented in
[REFERENCE.md](REFERENCE.md).

---

## Where to go next

- **[REFERENCE.md](REFERENCE.md)** — the complete manual: every concept, the full
  API, batch format, CLI workflows, and validation. (This is the previous long
  README, kept as a second reading.)
- **[.chats/USAP_FINAL_DESIGN_ROADMAP.md](.chats/USAP_FINAL_DESIGN_ROADMAP.md)** —
  design rationale, the full schema, and the phased roadmap.
- **[usap_devdiary.md](usap_devdiary.md)** — how the prototype was built and why,
  step by step.

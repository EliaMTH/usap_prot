# [WIP] USAP — Urban Semantic Annotation Package

USAP is a small SQLite/GeoPackage-style file format (`*.usap.gpkg`) and Python package for storing **editable, element-level semantic annotations over external 3D urban data assets** without copying any source 3D geometry. (It does store a derived 2D bounding box per asset, so packages open as maps — see Limitations.)

---

## The problem it addresses

Urban analysis increasingly works from the same area captured in several forms at once:

- a semantic city model (CityGML / CityJSON),
- one or more meshes (LoD1/LoD2, photogrammetry, triangulated terrain),
- a LAS/LAZ point cloud.

The assets actually used for visualization, analysis, or interaction are often separate meshes, point clouds, tiles, or derived model exports.

The semantic model defines the authoritative city objects, their classes, attributes, and relationships. However, the operational 3D assets do not preserve a reliable, editable, element-level link back to the semantic objects they represent (e.g. down to the specific faces of a mesh). Moreover, each format keeps semantics in its own "style" (CityGML objects, 3D Tiles feature IDs, the LAS classification byte) and none of them references the others.

USAP is an attempt to fill that gap.

---

## What USAP is

A `*.usap.gpkg` file stores, for one study area:

- **references** to the external assets (it never copies their geometry);
- the exact **element indices** an annotation covers;
- the **semantic concept** of each annotation (e.g. `RoofSurface`, `EnergyRoof`), drawn from external vocabulary registries (e.g. derived from a semantic model such as CityGML/ADE);
- editable **annotation records** with label, status, confidence, and claim-level attributes (method, source, timestamps — object properties stay in the semantic source, e.g. CityGML/ADE, so there is exactly one authority for them);
- optional **per-element value fields** — a dense scalar per point/face (e.g. shadow fraction per face at hour H), stored as compressed typed blocks and queryable by value;
- a lightweight mirror of **city-object identity** and a typed **relationship graph** used to retrieve annotations across an object and its parts (e.g. a building together with its roof and walls).

USAP works best when paired with a CityGML semantic model of the annotated objects, but it also works with a minimal custom vocabulary, so it can be used in a more exploratory context.

Like a CityGML 3.0 city object, which can have multiple geometric representations, a single annotation can span multiple representations — e.g. a CityGML object, LAS points, and mesh faces.

USAP is **not** a 3D city model, a geometry store, or a replacement for a semantic city model. When a CityGML source exists, it stays the semantic authority and USAP is the editable query/annotation layer on top of it.

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

A new package starts with **zero concepts** and enforces no taxonomy of its own: it accepts only vocabulary you seed into it (a thin JSON form of a SKOS concept scheme, or a minimal local scheme when no ontology is available yet). Every annotation references exactly one registered concept. Example registries do ship with the package — a CityGML 3.0 MVP subset, an ADE prototype, a minimal local scheme — and the CityGML importer seeds the first of them; they are starting points to seed or replace, not a built-in taxonomy.

## Per-element value fields

Besides membership sets ("these faces *are* a RoofSurface"), an annotation can carry a **value field**: one scalar per element of an asset part. E.g. _the shadow fraction of every mesh face at 14:00_ is stored as compressed typed blocks and queryable by value (`elements_where`). Sets cover booleans and categories; value fields are for genuinely continuous values.

**Where the metadata lives:** the concept's *definition* stays in the semantic source (vocabulary/ADE). The claim's own tags (`validAt`, `unit`, `method`) stay in the annotation's attributes, with one annotation per (field, timestep), `validAt` is what identifies a field, so it must live in the package for it to stay self-describing. An ADE may additionally catalog the analysis (hour + annotation uid); that complements, never replaces, the in-package copy.

---

## How it relates to existing work

USAP overlaps with, and deliberately defers to, several existing technologies:

| Existing technology | What it does | What USAP adds / why not just use it |
|---|---|---|
| **CityGML / CityJSON / 3DCityDB** | Full semantic 3D city model (geometry **and** semantics together) | USAP is an *overlay*, not a city model. It mirrors only identity/class/relationships and leaves geometry and full attributes in the source. |
| **3D Tiles + glTF `EXT_mesh_features` / `EXT_structural_metadata`** | Feature IDs and metadata attached to mesh features | The closest analog for *mesh* semantics. USAP differs by being an editable working file, not embedded in the tiles, and by spanning multiple representations of one object. |
| **LAS ASPRS `classification` + extra dimensions** | A semantic class stored *inside* the point cloud | The closest analog for *point* semantics. USAP differs by supporting arbitrary vocabularies, cross-asset links, and editing without rewriting a large file. |
| **GeoPackage** | OGC SQLite container | Used as the container. Packages open in QGIS/GDAL: browsable attribute layers plus a derived per-asset extent-box features layer. |

The combination USAP is testing is an uncommon one: **element-level, editable, cross-representation** semantic annotation in a single lightweight file. Every individual ingredient is known prior art (see [ACCELERATOR_ABLATION.md](docs/ACCELERATOR_ABLATION.md) §3); what is unusual is carrying them together, and this repository does not claim a systematic prior-art review.

If you know other tools that should be here, let us know!

---

## Limitations

- **Once processed, 3D assets are supposed to be immutable.** An annotation is bound to *one immutable version* of an external file. LAS point order and mesh face order are **not** guaranteed to survive reprojection, re-tiling, thinning, remeshing, re-export, or conversion to COPC (which re-orders points by design). USAP records a content hash to *detect* that a file changed, but it cannot
  *rebind* annotations.
- **Membership encoding is deliberately simple** (sorted `uint32` offsets + zlib, in blocks). Adopting *roaring bitmaps* is planned as immediate future work.
- **Trusted inputs only.** Nothing here is hardened against hostile files: a
  CityGML document is parsed as a full tree in memory, and an arbitrary
  SQLite file is refused only by a profile check. Compressed payloads *are*
  bounded on decode, but treat a `.usap.gpkg` from a third party the way you
  would treat any other file you were handed.
- **GIS tools see a summary, not the semantics.** A `.usap.gpkg` opens in QGIS/GDAL as a real GeoPackage: three read-only attribute layers (annotations, concepts, city objects) and one features layer drawing a derived 2D bounding box per registered asset (from the bounds captured at registration — never actual geometry). Fine-grained USAP content (element memberships, value fields) still requires the SDK, and USAP is **not** a registered OGC extension. 

---

Explore USAP in more detail with:

- [INGESTION.md](docs/INGESTION.md) — the three supported creation/editing procedures.
- [REFERENCE.md](docs/REFERENCE.md) — the full manual (concepts, config keys, batch format, Python API).
- [TESTS.md](docs/TESTS.md) — the test suite.

The rest of the documentation lives in [docs/](docs/).
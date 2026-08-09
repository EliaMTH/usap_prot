# [WIP] USAP — Urban Semantic Annotation Package

USAP is a small SQLite/GeoPackage-style file format (`*.usap.gpkg`) and Python package for storing **editable, element-level semantic claims that link indexed elements in operational 3D urban assets to semantic concepts and, when available, authoritative city-object identities**. It does not copy source 3D geometry. It stores only references, hashes, index-space metadata, annotations, and a derived 2D bounding box per asset so packages can open as maps (see Limitations).

---
## The problem it addresses

Urban analysis increasingly works from the same area represented in several forms at once:

- a semantic city model such as CityGML or CityJSON;
- one or more meshes, including LoD models, photogrammetry, and triangulated terrain;
- a LAS/LAZ point cloud;
- tiles or other derived assets used for visualization, analysis, or interaction.

A semantic city model may already contain geometry, and it already defines the association between each city object and its native geometry. **USAP does not repeat or replace that association.** The gap appears when operational 3D assets are separate from the semantic authority: their points, faces, or other elements usually do not preserve a reliable, editable link to the authoritative city-object instances they represent.

Storing fine-grained annotations directly in every source asset is also often undesirable. High-resolution assets can be large; annotation records are mutable; each format exposes semantics differently; and the same claim may need to cover several representations. Editing embedded annotations may require rewriting large geometry files or the authoritative semantic model, while element-to-annotation queries need an index organized for that direction of access.

USAP fills this gap with a compact, separate claim layer. It is most useful when annotations target individual elements of operational assets, span several assets, change independently of the source data, or must be queried efficiently in both directions. It is not needed merely to restate whole-object semantics or the native object-geometry association already present in the semantic source.

---
## What USAP is

A `*.usap.gpkg` file stores, for one study area:

- **references and optional hashes** for external assets, without copying their geometry;
- the stable **asset parts and element-index spaces** used by annotations;
- the exact **element indices** covered by each annotation;
- the **semantic concept** asserted by each annotation, drawn from a registered external or local vocabulary;
- an optional link to the authoritative **city-object instance** represented or concerned by the claim;
- editable **annotation records** with status, confidence, and claim-level attributes such as method and source, each carrying one or more dated **assessments** (one evaluation of the claim against one 3D asset);
- optional **per-element value fields**, stored as compressed typed blocks and queryable by value;
- a lightweight mirror of **city-object identity** and a typed, directed **relationship graph** used to retrieve annotations across an object and its parts, or across whatever else the source says it relates to.

The division of authority is deliberate:

- the semantic source owns the **meaning layer**: which objects and concepts exist, their authoritative properties, and their relationships;
- USAP owns the **claim layer**: which indexed elements are associated with which object or concept, with what status, confidence, provenance, and temporal context.

USAP works best when paired with a semantic city model, but it can also use a minimal custom vocabulary for exploratory workflows.

A single annotation can link one authoritative city object and one semantic concept to memberships in several operational 3D assets, for example LAS points and mesh faces. The city object is the semantic referent of the claim; it is not another geometric membership.

USAP is **not** a 3D city model, a geometry store, or a replacement for a semantic authority. When a CityGML source exists, it remains authoritative and USAP is the editable, query-optimized annotation layer beside it.

---
## Mental model

```text
semantic authority
  city object: building_1_roof_1
  concept:     EnergyRoof
                    │
                    ▼
USAP annotation
  editable claim: concept + object link + attributes + status
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
operational asset         operational asset
  area.las                  area.obj
  └─ points/all             └─ mesh primitive
     └─ points                 └─ faces
        [100,101,102]             [40,41,42]
```

One annotation, multiple asset memberships — grouped by the evaluation that
made them:

```text
annotation: ann_energy_roof_001
  concept: EnergyRoof
  primary city object: building_1_roof_1
  assessment (2026-06-30, area.las):
    LAS points:        [100, 101, 102]
  assessment (2026-06-30, area.obj):
    LoD2 mesh faces:   [40, 41, 42]
    triangulation:     [800, 801]
  assessment (2027-05-14, area.obj):
    LoD2 mesh faces:   [40, 41, 42, 43]
```

An **assessment** is one dated evaluation of the claim against one 3D asset. The
concept and the city object are stated once, on the annotation; what varies
between evaluations — the date, the asset, the extent, the method — lives on the
assessment. Re-surveying does not fork the claim into a second annotation that
can drift from the first. Applications that only ever evaluate once never have
to mention assessments: one is created implicitly.

---
## Concepts & vocabularies

A new package starts with **zero concepts** and enforces no taxonomy of its own. Every annotation references exactly one registered concept, and every concept comes from a source you supply.

For CityGML that source is the **OGC schemas themselves**: `load_citygml_schema()` reads each element's namespace and its `substitutionGroup`, so both the classes and their hierarchy are the normative ones rather than a transcription. For anything without a schema — an ADE, a local scheme for exploratory work — a thin JSON concept registry does the same job.

USAP deliberately ships **no CityGML vocabulary**. It used to, and the file accumulated exactly the errors a hand-written one accumulates: surfaces filed under the wrong module, a class parented to the wrong supertype, a class that exists only in CityGML 2.0. Two example registries do ship — an ADE prototype and a minimal local scheme — as starting points to seed or replace, not as a taxonomy.

The same holds for **link types**. Which CityGML property means "part of" is stated in no CityGML artifact — not the XSD, not the conceptual model, not an OWL rendering — so USAP does not assert it either. The package builder does, and until they do, an imported edge is stored and queryable by name but is not reported as a part.

## Per-element value fields

Besides membership sets ("these faces are a `RoofSurface`"), an annotation can carry a **value field**: one scalar per element of an asset part. For example, the shadow fraction of every mesh face at 14:00 is stored as compressed typed blocks and queried with `elements_where`. Sets cover booleans and categories; value fields are for genuinely continuous values.

**Where the metadata lives:** the concept definition remains in the semantic source or vocabulary. The claim's own tags (`unit`, `method`) remain in the annotation or assessment attributes. A field measured again at a later date is a second **assessment** of the same annotation — one claim, one concept, one city object, several dated evaluations — not a second annotation per timestep. An external application schema may also catalog the analysis; that complements rather than replaces the in-package claim metadata.

---
## How it relates to existing work

USAP overlaps with, and deliberately defers to, several existing technologies:

| Existing technology | What it does | What USAP adds / why not just use it |
|---|---|---|
| **CityGML / CityJSON / 3DCityDB** | Authoritative semantic city model; it may also contain geometry already linked to its city objects | USAP does not duplicate the native object-geometry association. It adds compact, editable mappings from elements of separate operational assets to the same authoritative object identities, together with cross-asset and reverse element queries. |
| **3D Tiles + glTF `EXT_mesh_features` / `EXT_structural_metadata`** | Feature IDs and metadata attached to mesh features | The closest analogue for mesh semantics. USAP is an editable working file rather than metadata embedded in a delivery asset, and one claim can cover several representations. |
| **LAS ASPRS `classification` + extra dimensions** | Classes and attributes stored inside the point cloud | The closest analogue for point semantics. USAP supports arbitrary registered vocabularies, authoritative object links, cross-asset claims, and edits without rewriting the point-cloud file. |
| **GeoPackage** | OGC SQLite container | Used as the container. Packages open in QGIS/GDAL as browsable attribute layers plus a derived per-asset extent-box feature layer. |

The combination USAP is testing is an uncommon one: **element-level, editable, cross-representation semantic annotation in a single lightweight file**. Every individual ingredient is known prior art (see [ACCELERATOR_ABLATION.md](docs/ACCELERATOR_ABLATION.md) §3); what is unusual is carrying them together, and this repository does not claim a systematic prior-art review.

The storage benefit is workload-dependent. A few whole-object facts may be best kept only in the semantic authority. USAP is intended for dense or mutable element-level claims over operational assets, especially when those assets are large, heterogeneous, or reused by several analyses.

If you know other tools that should be here, let us know.

## Standards positioning

USAP's annotation model lines up with the **W3C Web Annotation Data Model**: an annotation carries bodies identifying the semantic concept and the authoritative city object, and targets selecting indexed elements of operational 3D assets.

**Full adoption is future work** — a published JSON-LD context, a USAP vocabulary for the 3D-specific terms W3C leaves extensible, and conforming export/import. USAP does **not** currently claim conformance, and a `.usap.gpkg` is not a W3C JSON-LD document.

What is already in place are the parts that could not be added afterwards without rewriting existing packages: every package carries a stable identifier, content hashes use a canonical `algorithm:digest` form, timestamps are UTC ISO-8601, asset parts record which convention assigned their element indices, and concepts carry provenance from which stable identifiers can be derived. The mapping and the remaining work are in [FW_FEATURE_W3C_WEB_ANNOTATION_PROFILE.md](FW_FEATURE_W3C_WEB_ANNOTATION_PROFILE.md).

---
## Limitations

- **Once processed, 3D assets are supposed to be immutable.** An annotation is bound to one immutable version of an external file. LAS point order and mesh face order are not guaranteed to survive reprojection, re-tiling, thinning, remeshing, re-export, or conversion to COPC, which reorders points by design. USAP records a content hash to detect that a file changed, but it cannot rebind annotations.
- **Membership is stored as roaring bitmaps**, in blocks of 16384 elements, using CRoaring's portable serialization. The payload is therefore readable by Java, Go, C++, Rust, and other compatible roaring implementations rather than being a private Python blob. Block width is a deliberate compromise: wider blocks usually compress better, while narrower blocks give the reverse query more precise pruning.
- **Trusted inputs only.** Nothing here is hardened against hostile files. A CityGML document is parsed as a full tree in memory and walked twice, and an arbitrary SQLite file is refused only by a profile check. Compressed payloads are bounded on decode, but treat a `.usap.gpkg` from a third party as you would any other untrusted file.
- **GIS tools see a summary, not the complete annotation model.** A `.usap.gpkg` opens in QGIS/GDAL as a real GeoPackage: four read-only attribute layers (annotations, assessments, concepts, city objects) and one feature layer drawing a derived 2D bounding box per registered asset. Fine-grained memberships and value fields still require the SDK, and USAP is not a registered OGC extension.

---

Explore USAP in more detail with:

- [API.md](docs/API.md) — every public call in one place, one line each. Start here to see what the SDK offers;
- [REFERENCE.md](docs/REFERENCE.md) — the full manual: the data model, integrating USAP into an application, concept registries, config keys, batch format, validation;
- [INGESTION.md](docs/INGESTION.md) — the three supported creation and editing procedures, end to end;
- [SCHEMA_WIRING.md](docs/SCHEMA_WIRING.md) — how the tables, views and indexes connect;
- [TESTS.md](docs/TESTS.md) — what the test suite covers and why.

Two documents record decisions rather than describe the current state:
[ACCELERATOR_ABLATION.md](docs/ACCELERATOR_ABLATION.md) (are the query tables
worth their cost) and [VALUE_FIELDS_DESIGN.md](docs/VALUE_FIELDS_DESIGN.md).

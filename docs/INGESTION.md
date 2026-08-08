# USAP ingestion — the three procedures

These are the supported creation, data-ingestion, and editing procedures. Everything runs from two JSON files plus one semantic authority:

- a **project config** — what the package is made of: operational 3D assets, the semantic source, and which linking files to apply;
- a **linking JSON** (annotation batch) — which indexed elements of which operational asset are associated with which city-object instance and concept;
- the **semantic authority** — either a CityGML file (procedure 1) or a minimal vocabulary JSON (procedure 2).

One command executes either procedure end to end:

```python
from usap import build_project_package_from_file

build_project_package_from_file("project.json")               # create (1, 2)
build_project_package_from_file("update.json", update=True)   # edit (3)
```

(or `python examples/build_project_package.py project.json [--update]`).

The division of authority is the same in all three procedures:

- **USAP owns the claim layer** — which indexed elements are associated with which object or concept, with what status, confidence, provenance, and temporal metadata;
- **the semantic source owns the meaning layer** — which concepts and objects exist, their authoritative properties, and their hierarchy.

A CityGML source may contain its own geometry, but this workflow does not copy or restate the native CityGML object-geometry association. The CityGML import is used for object identity, class, relationships, and provenance. Element memberships are stored for the separately registered operational 3D assets. USAP stores references and indices — never source geometry and never authoritative object properties.

---
## Procedure 1 — init from 3D assets + CityGML + linking JSON

City-object names in the linking JSON are the **`gml:id` values** from the CityGML file. Objects, their classes, and their decomposition come from the CityGML import; the linking JSON only states which elements in the registered operational assets correspond to which authoritative city object.

`project.json`:
```json
{
  "db_path": "city.usap.gpkg",
  "manifest_path": "city_manifest.json",
  "citygml": { "path": "city.gml" },
  "meshes": [
    { "path": "city_mesh.ply", "uri": "city_mesh", "representation_name": "city_mesh" }
  ],
  "las": [
    { "path": "area.las" }
  ],
  "annotation_batches": ["links.json"]
}
```

`links.json` — the minimal entry is *object + elements*:
```json
{
  "annotations": [
    {
      "city_object_uid": "building_1_roof_1",
      "memberships": [
        { "asset_uri": "city_mesh", "element_indices": [120, 121, 122] }
      ]
    }
  ]
}
```

What is derived when omitted:
- `concept` — inherited from the linked object's CityGML class
  (say it explicitly to make a *different* claim, e.g. an ADE concept like
  `EnergyRoof` on a `RoofSurface` object);
- `annotation_uid` — `ann_{object_uid}_{concept}` (deterministic, so
  re-applying the file edits in place instead of duplicating);
- `element_kind` — the asset part's stored kind;
- `asset_uri` refers to the `uri` given at asset registration (give assets
  stable logical URIs in the config); add `"part_path"` only when the asset
  has several parts. Numeric `"asset_part_id"` from the manifest still works.

Optional per entry: `label`, `status`, `confidence`, `attributes`
(claim-level metadata only — method, source, timestamps), `value_fields`
(dense per-element values, see REFERENCE.md).

## Procedure 2 — init from 3D assets + minimal vocabulary + linking JSON

No CityGML. Semantics comes from a minimal vocabulary (names only, optional
parent links); city-object names can be **anything, as long as they are
unique**. The linking JSON carries the documented minimum per entry:
**id + what it is + element ids** — and must opt in with
`"create_missing_city_objects": true`.

`vocab.json`:
```json
{
  "scheme": "local",
  "concepts": [
    { "local_name": "TempSurface" },
    { "local_name": "TempRoof", "parent_uri": "TempSurface" }
  ]
}
```

`project.json`: as in procedure 1, with `"vocabularies": ["vocab.json"]` and
no `citygml` section.

`links.json`:
```json
{
  "create_missing_city_objects": true,
  "annotations": [
    {
      "city_object_uid": "tower_A_roof",
      "concept": "TempRoof",
      "memberships": [
        { "asset_uri": "city_mesh", "element_indices": [0, 1, 2] }
      ]
    }
  ]
}
```

Each unknown `city_object_uid` creates a **carrier city object**: classed by
the entry's concept, `object_status = 'temporary'`, nothing else. The
resulting package answers the same queries as a CityGML-built one — by
object (`elements_for_city_object("tower_A_roof")`), by concept and
subclass (`elements_for_semantic_class("TempSurface",
include_subclasses=True)` returns the roof via the vocabulary's parent
link), and in reverse (`annotations_for_elements`).

Carriers are the alignment hook: when a proper CityGML-backed package
arrives, find them with `object_status = 'temporary'` and map them onto real
objects (alignment tooling is future work).

## Procedure 3 — edit an existing USAP package

The same file formats are applied to the existing package with `update=True`:

```python
build_project_package_from_file("update.json", update=True)
```

- **Add assets**: list them in the config; registration is idempotent, so
  already-known assets (same URI + hash) are skipped and new ones added.
- **Add concepts**: an edit can only make claims with concepts the package
  already accepts; to extend the accepted list, provide the new concepts in
  the vocabulary-registry format and list the file either in the config's
  `"vocabularies"` key or in the linking JSON's own top-level
  `"vocabularies"` key (loaded in the same transaction as its annotations).
  Seeding is additive, idempotent, and enriching: new concepts are added, and
  on an already-registered concept any field still `NULL` (provenance,
  `scheme_version`, a parent) is filled in from the new file. A field that
  already holds a different value raises rather than being rewritten, so an
  edit can enrich the registry but never silently redefine it.
- **Edit annotations**: list batches in `annotation_batches`; in update mode
  they run with `replace_existing=True` — an entry with an existing
  `annotation_uid` (given or derived) updates the fields it carries and
  replaces the memberships/value fields it lists, leaving the rest intact.
- Standalone editing without a config also works:
  `apply_annotation_batch_file(pkg, "edit.json", replace_existing=True)`.

**Discouraged:** using procedure-2-style custom names in a package built
from CityGML (procedure 1). The guardrail is strictness by default — a
linking file without `"create_missing_city_objects": true` fails loudly on
any name the package does not know, so ad-hoc carriers cannot slip in
unnoticed.

---
## What each step reads and stores

| Input | Read for | Stored |
|---|---|---|
| Operational 3D assets (LAS, mesh) | element counts + part structure (the index space), optional SHA-256 + bounds | URI, hash, parts, counts — never geometry |
| CityGML | object identity, class, decomposition, and source provenance | mirrored ids/classes/relationships — any CityGML geometry and authoritative object attributes remain only in the source |
| Vocabulary JSON | accepted concepts (+ parent links) | `usap_semantic_class` + hierarchy closure |
| Linking JSON | claims connecting objects/concepts to asset elements (+ optional value fields) | compressed membership/value blocks and annotation records |

If your pipeline already knows the element counts, assets can also be
declared without reading the files at all: `register_asset` +
`register_asset_part` in the SDK.

**Large assets.** Registration is the only normal ingestion step that opens an operational asset file. After registration, annotation editing and querying use the stored index-space metadata and USAP's membership/value blocks; they do not reopen the source geometry. Source-file size therefore no longer directly controls query cost. Package operations still scale with the number and density of stored memberships, value blocks, and returned results.

Registration reads only what it stores: LAS/LAZ from the header alone, and meshes over 256 MB in a streaming pass rather than a full load (`stream=True`/`False` to override; see REFERENCE.md → Mesh support → Large meshes for supported formats). The remaining optional full-file read is `compute_hash`: it can take minutes on a 10 GB file, but it is what makes later source changes detectable, so disable it knowingly.

Keeping the annotations in USAP also means that creating, accepting, rejecting, or replacing a claim does not rewrite the CityGML authority, mesh, or point-cloud source. This separation is especially useful for large or shared assets, but it is not a claim that every CityGML workflow needs a USAP package.

## GIS interoperability

Every package built by these procedures opens in QGIS/GDAL: three read-only
attribute layers (annotations, concepts, city objects) plus a features layer
drawing one **derived 2D bounding box per asset** — written automatically at
registration from the stored part bounds (for LAS, literally the header box;
no geometry file is re-read). Declare the package CRS with `"srs_id"` in the
project config when you know it (an EPSG code found in the LAS CRS is promoted
automatically when unambiguous); local-coordinate meshes stay in the undefined
SRS (−1). Since assets are immutable in USAP, boxes are written once and
removed by cascade when an asset is deleted and re-ingested.

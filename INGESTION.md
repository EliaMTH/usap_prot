# USAP ingestion — the three procedures

These are the supported creation / data-ingestion / editing procedures
(designed in `DATA_INGESTION_REVAMP.md`). Everything runs off two JSON files:

- a **project config** — what the package is made of: assets, semantic source,
  and which linking files to apply;
- a **linking JSON** (annotation batch) — which city object owns which
  elements of which 3D asset.

One command executes either procedure end to end:

```python
from usap import build_project_package_from_file

build_project_package_from_file("project.json")               # create (1, 2)
build_project_package_from_file("update.json", update=True)   # edit (3)
```

(or `python examples/build_project_package.py project.json [--update]`).

The division of authority behind all three: **USAP owns the claim layer**
(which elements, under which concept, status/confidence/provenance) and the
**semantic source owns the meaning layer** (which concepts and objects exist,
their properties, their hierarchy). USAP stores references and element
indices — never geometry, never object properties.

---

## Procedure 1 — init from 3D assets + CityGML + linking JSON

City-object names in the linking JSON are the **gml ids** from the CityGML
file. Objects, their classes, and their decomposition come from the CityGML
import; the linking JSON only has to say *which object owns which elements*.

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
  stable logical uris in the config); add `"part_path"` only when the asset
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

## Procedure 3 — edit an existing usap

Same file formats, applied to the existing package with `update=True`:

```python
build_project_package_from_file("update.json", update=True)
```

- **Add assets**: list them in the config; registration is idempotent, so
  already-known assets (same uri + hash) are skipped and new ones added.
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
| 3D assets (LAS, mesh) | element counts + part structure (the index space), optional SHA-256 + bounds | uri, hash, parts, counts — never geometry |
| CityGML | object identity, class, decomposition | mirrored ids/classes/relationships — never geometry or object attributes |
| vocabulary JSON | accepted concepts (+ parent links) | `usap_semantic_class` + hierarchy closure |
| linking JSON | annotations: object ↔ elements (+ optional value fields) | compressed membership/value blocks, annotation records |

If your pipeline already knows the element counts, assets can also be
declared without reading the files at all: `register_asset` +
`register_asset_part` in the SDK.

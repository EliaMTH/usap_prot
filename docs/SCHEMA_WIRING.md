# USAP schema wiring

How the objects in [`sql/schema.sql`](../sql/schema.sql) connect: which foreign keys wire
the tables together, and which tables each view reads from.

**Object counts:** 14 USAP tables + 4 GeoPackage plumbing tables = 18 base tables,
plus 4 views and 6 indexes.

In every diagram, an arrow `A ──▶ B` means **"A has a foreign key pointing to B"**
(A references B; the arrow points at the thing being referenced).

---

## 1. The USAP data model — how the tables wire together

```mermaid
flowchart TB
    ASSET[usap_asset]
    SCLASS[usap_semantic_class]
    APART[usap_asset_part]
    AEXT[usap_asset_extent]
    SCC[usap_semantic_class_closure]
    CO[usap_city_object]
    COR[usap_city_object_relationship]
    COC[usap_city_object_closure]
    ANN[usap_annotation]
    ANNO[usap_annotation_object]
    MB[usap_membership_block]
    VB[usap_value_block]
    PROF[usap_profile]
    LOG[usap_edit_log]

    APART -->|asset_id| ASSET
    AEXT -->|asset_id| ASSET
    SCLASS -->|parent_class_id| SCLASS
    SCC -->|ancestor + descendant| SCLASS
    CO -->|semantic_class_id| SCLASS
    CO -->|source_asset_id| ASSET
    COR -->|parent + child| CO
    COR -->|source_asset_id| ASSET
    COC -->|ancestor + descendant| CO
    ANN -->|semantic_class_id| SCLASS
    ANN -->|primary_city_object_id| CO
    ANNO -->|annotation_id| ANN
    ANNO -->|city_object_id| CO
    MB -->|annotation_id| ANN
    MB -->|asset_part_id| APART
    VB -->|annotation_id| ANN
    VB -->|asset_part_id| APART

    classDef hub fill:#f2b134,stroke:#7a5a12,color:#1a1200,stroke-width:2px;
    classDef standalone fill:#e6ebf0,stroke:#8a97a3,color:#1a2028,stroke-dasharray:4 3;
    class ANN hub
    class PROF,LOG standalone
```

**The shape to notice:** `usap_annotation` (highlighted) is the hub of the whole model.
It reaches "up" to *what* something means (`usap_semantic_class`) and *which object* it
is about (`usap_city_object`), and everything below it (`_object`, `_membership_block`,
`_value_block`) reaches "down" to pin the annotation onto concrete geometry
(`usap_asset_part`). The two `_closure` tables are precomputed shortcuts hanging off
their respective hierarchies (classes, objects). `usap_profile` and `usap_edit_log`
(dashed) stand alone — no foreign keys in or out.

### Foreign keys, table by table

| Child table | Column | ──▶ References |
|-------------|--------|---------------|
| `usap_asset_part` | `asset_id` | `usap_asset(asset_id)` |
| `usap_asset_extent` | `asset_id` | `usap_asset(asset_id)` |
| `usap_semantic_class` | `parent_class_id` | `usap_semantic_class(semantic_class_id)` (self) |
| `usap_semantic_class_closure` | `ancestor_class_id` | `usap_semantic_class(semantic_class_id)` |
| `usap_semantic_class_closure` | `descendant_class_id` | `usap_semantic_class(semantic_class_id)` |
| `usap_city_object` | `semantic_class_id` | `usap_semantic_class(semantic_class_id)` |
| `usap_city_object` | `source_asset_id` | `usap_asset(asset_id)` |
| `usap_city_object_relationship` | `parent_city_object_id` | `usap_city_object(city_object_id)` |
| `usap_city_object_relationship` | `child_city_object_id` | `usap_city_object(city_object_id)` |
| `usap_city_object_relationship` | `source_asset_id` | `usap_asset(asset_id)` |
| `usap_city_object_closure` | `ancestor_city_object_id` | `usap_city_object(city_object_id)` |
| `usap_city_object_closure` | `descendant_city_object_id` | `usap_city_object(city_object_id)` |
| `usap_annotation` | `semantic_class_id` | `usap_semantic_class(semantic_class_id)` |
| `usap_annotation` | `primary_city_object_id` | `usap_city_object(city_object_id)` |
| `usap_annotation_object` | `annotation_id` | `usap_annotation(annotation_id)` |
| `usap_annotation_object` | `city_object_id` | `usap_city_object(city_object_id)` |
| `usap_membership_block` | `annotation_id` | `usap_annotation(annotation_id)` |
| `usap_membership_block` | `asset_part_id` | `usap_asset_part(asset_part_id)` |
| `usap_value_block` | `annotation_id` | `usap_annotation(annotation_id)` |
| `usap_value_block` | `asset_part_id` | `usap_asset_part(asset_part_id)` |

`usap_profile` and `usap_edit_log` have no foreign keys.

**Primary-object invariant.** An annotation's primary city object is recorded twice:
in `usap_annotation.primary_city_object_id` and as a `represents` row in
`usap_annotation_object`. When the column is not NULL, the matching `represents` row
must exist. `create_annotation` and `update_annotation` maintain both together;
`validate_report()` reports a disagreement as `ANNOTATION_PRIMARY_OBJECT_LINK_MISSING`.
Additional `usap_annotation_object` rows (other objects, other `relation_type`s) are
free-form and are not constrained by this invariant.

---

## 2. Which tables each view reads from

The four views are read-only overlays — none stores data; each is a `JOIN` across the
tables above. Here an arrow `V ──▶ T` means **"view V SELECTs from table T"**. All four
alias their primary key to `fid` and are registered in `gpkg_contents` so QGIS/GDAL can
browse them.

```mermaid
flowchart LR
    AEXTV["usap_asset_extents (features)"]:::view
    ANNV["usap_annotations_view"]:::view
    CONV["usap_concepts_view"]:::view
    COV["usap_city_objects_view"]:::view

    AEXTV --> AEXT[usap_asset_extent]
    AEXTV --> ASSET[usap_asset]
    AEXTV --> APART[usap_asset_part]
    ANNV --> ANN[usap_annotation]
    ANNV --> SCLASS[usap_semantic_class]
    ANNV --> CO[usap_city_object]
    ANNV --> MB[usap_membership_block]
    ANNV --> VB[usap_value_block]
    CONV --> SCLASS
    CONV --> ANN
    COV --> CO
    COV --> SCLASS
    COV --> ASSET

    classDef view fill:#bfe3d6,stroke:#2f6b57,color:#0e2a20,stroke-width:2px;
```

Each view flattens a little "star" of tables into one browsable layer for GIS tools.
`usap_asset_extents` is the only **features** (mappable) layer because it is the only
view with a geometry column; the other three are **attributes** (non-spatial) layers.

---

## 3. The GeoPackage plumbing (separate, standard, not USAP data)

These four are required by the GeoPackage spec, not invented by USAP. They wire only to
each other — the catalog machinery that lets generic GIS tools discover the layers.

```mermaid
flowchart LR
    GGC[gpkg_geometry_columns] -->|table_name| GC[gpkg_contents]
    GGC -->|srs_id| GSRS[gpkg_spatial_ref_sys]
    GC -->|srs_id| GSRS

    classDef plumb fill:#e2e8ee,stroke:#7d8b99,color:#1a2028;
    class GGC,GC,GSRS plumb
```

`gpkg_contents` is the bridge between the two worlds: the USAP views get listed *in*
`gpkg_contents` (the "register this layer" insert in
[`src/usap/geopackage.py`](../src/usap/geopackage.py)), which is how the standard
plumbing comes to point at the USAP model. (`gpkg_extensions`, the fourth plumbing table,
has no foreign keys — USAP registers itself there so tools know the file carries USAP
tables.)

---

## 4. Indexes (neither tables nor views)

Fast-lookup structures on non-primary-key columns:

| Index | On table | Column(s) |
|-------|----------|-----------|
| `usap_scc_by_descendant` | `usap_semantic_class_closure` | `descendant_class_id` |
| `usap_rel_by_parent_graph` | `usap_city_object_relationship` | `graph_name, parent_city_object_id, relationship_type` |
| `usap_rel_by_child_graph` | `usap_city_object_relationship` | `graph_name, child_city_object_id, relationship_type` |
| `usap_annotation_by_class` | `usap_annotation` | `semantic_class_id` |
| `usap_mb_by_element_block` | `usap_membership_block` | `asset_part_id, element_kind, block_start` |
| `usap_vb_by_part` | `usap_value_block` | `asset_part_id, element_kind` |

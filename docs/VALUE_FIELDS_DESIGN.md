# Design draft — Per-element value fields in USAP

**Status:** implemented (v1, 2026-07-06) — `usap_value_block` + the
`annotate_value_field` / `values_for_annotation` / `elements_where` /
`value_field_stats` API in `src/usap/core.py`; batch `"value_fields"` support;
validation in `validate_report()`. V1 requires full coverage (NaN = "no value");
partial/sub-range fields, sparse overlays, and acceleration indexes remain future
work. Originally a design discussion (2026-06-26).
**One-liner:** add a second element-level payload type — a *value per element* (a scalar
field over a mesh's faces / a cloud's points) — alongside the existing *set membership*,
so fields like "shadowing per face at hour H" can be stored, queried by value, and
highlighted, **without tying them to a city object**.

---

## 1. Motivation

USAP today stores exactly one kind of element-level data: **set membership** —
"these face/point indices belong to concept C" — as roaring bitmap blocks in
`usap_membership_block` (see `src/usap/encoding.py` for the codec; the decode is
`BitMap.deserialize` → `np.frombuffer(..., uint32)` → `+ block_start`). Note that
value blocks below keep the plain `zlib` path: they are dense arrays of values,
not sets of indices, so roaring does not apply to them.

What's missing is **element → value**: a number attached to *each* element. Concretely:

> A property computed on a mesh, one value per face — e.g. the **shadowing** of the mesh
> at a given hour. Could be boolean (lit/shadowed) or, in general, continuous
> (shadow fraction 0–1, irradiance kWh/m², temperature …). Many timesteps are possible.

### Requirements (agreed)
- **All values possible** — not just boolean; arbitrary continuous scalars.
- **Write-once**, then read-many. Editing is supported but is an **exception** (bug fixes),
  not the intended workflow.
- **Query by value** is a first-class need — e.g. "faces where shadow > 0.5 at 14:00".
- **No city object required** — the field is a property of the *geometry asset*, bound to
  its elements. (USAP already allows annotations with a `NULL` primary city object and
  element-only membership.)

---

## 2. Key concept: membership vs. value

| | element → **concept** (have) | element → **value** (new) |
|---|---|---|
| meaning | which elements *are* C | the value of property P *at* each element |
| shape | sparse **set** | dense **array** |
| store | `usap_membership_block` (roaring bitmap of indices) | `usap_value_block` (compressed value array) |
| good for | categories, booleans, "this set is a RoofSurface" | continuous scalar fields |

**Important:** a *boolean / categorical* field does **not** need any new mechanism — it is a
set, and sets are native. "Shadowed: true/false at 14:00" is just an annotation
(concept `Shadowed`, `validAt=14:00`) whose **membership** is the shadowed faces; the rest are
false by omission; multiple hours → multiple annotations. Reach for value-blocks **only** for
genuinely continuous fields.

Two layouts to **avoid** for continuous data:
- **row-per-element** (`element_index, value, …`): cardinality blow-up (N × fields × timesteps),
  defeats the compression USAP is built on.
- **one concept per distinct value** (the membership trick on continuous data): ≈ one concept
  per face. This is the only place a "fixed number of values" would be forced on you — and it
  throws away the "all values possible" requirement. Don't make binning the *primary* store.

---

## 3. The shadow example, fully modeled

- **Concept:** `Shadowing` / `ShadowFraction`. CityGML core has no such concept, so define it in
  an **ADE** (exactly like the energy ADE in the demo). It is an element-level annotation
  concept; `applies_to` the mesh asset, not a building.
- **One annotation per (field, timestep):**
  `concept = ShadowFraction`,
  `attributes_json = { "validAt": "2026-06-21T14:00:00Z", "method": "raytrace_v1",
  "scenario": "summer_solstice", "unit": "fraction", "valueMin": 0, "valueMax": 1 }`,
  `primary_city_object_id = NULL`.
- **Payload:** a **value-block** = compressed `float32` array, one value per face
  (`element_kind = face`), aligned to face index `0..N-1`.
- **Query** "faces shadowed > 0.5 at 14:00" = decompress that annotation's value-block +
  vectorized `values > 0.5` → matching face indices → highlight. The query **output is a
  face-set** — identical shape to a membership result — so it plugs straight into the existing
  highlight / filter path with no downstream changes. (Unlike the building filter, there is **no
  city-object resolve step**: the value query yields the geometry directly. Single-stage.)
- **Many hours** → many annotations, each with its own `validAt` + value-block (the same
  temporal/multi-assessment pattern the demo already uses for roofs).

---

## 4. Design

### 4.1 Storage — `usap_value_block` (sibling of `usap_membership_block`)
A new table that mirrors the membership block exactly, carrying *values* instead of *indices*:

| column | notes |
|---|---|
| `annotation_id` | FK → `usap_annotation` (the semantic owner) |
| `asset_part_id` | FK → `usap_asset_part` (which geometry) |
| `element_kind` | 1 = face, 2 = point (reuse existing constant) |
| `block_start` | first element index covered by this block |
| `element_count` | number of values in the block |
| `value_dtype` | `'f4' | 'f2' | 'u1' | 'i2' | …` — how to decode the payload |
| `payload` | BLOB = `zlib(np.ascontiguousarray(values).tobytes())` for elements `block_start … block_start+count-1` |

- **Aligned by position:** element *i*'s value = `decoded[i - block_start]`. Dense over the
  block's range. A field can span **multiple blocks** (chunked), like membership does.
- **Sparse / "no value" elements:** either reserve a sentinel (`NaN` for floats) or mark the
  valid elements with an accompanying membership set on the same annotation. Decide per field
  (see Open Questions).
- **Compression:** same `zlib` path. Smooth physical fields (shadow, irradiance) compress well
  (spatial coherence); high-entropy/random fields keep ~`sizeof(dtype)`/element — that's fine,
  it still stores and round-trips.

> Metadata (unit, value range, method, validAt) lives on the annotation's `attributes_json` to
> start. Only promote to a dedicated `usap_value_field` table if per-field metadata/stats become
> heavy or need indexing.

### 4.2 Querying by value
- Default implementation = **decompress + vectorized predicate** → element indices
  (sorted). Simple and fast: milliseconds for a few million elements; sub-millisecond at the
  demo's mesh scale (~137k faces, ~0.5 MB/field).
- **Acceleration is optional and additive** — only if profiling demands it (many fields ×
  timesteps, or very low latency), and **behind the same function signature**:
  - **sort-permutation:** store elements ordered by value → range query = binary search + slice,
    exact values preserved; or
  - **coarse bin-index:** precompute the face-set per value range (membership-style) → instant
    coarse queries, exact values still in the block.
  Both ride *on top of* the exact block; neither replaces it.

### 4.3 Editing (the exception path)
- **Whole-block rewrite** (`replace_value_field`) — a field is small; cheap to rewrite
  occasionally. This is the primary edit mechanism (matches write-once-read-many).
- Optionally a **sparse overlay** of `(index → corrected value)` layered on the base block, if
  you want to avoid rewriting large fields. Defer unless needed.

---

> **Profile 0.4.0 note.** The column list in section 4.1 is the one in force
> when this was written. `usap_value_block` now carries an `assessment_id` as
> well, and its `UNIQUE` is scoped to that rather than to `annotation_id`: a
> field measured again at a later date is a second *assessment* of the same
> annotation, so the same part legitimately carries two complete fields.
> `annotation_id` stays on the block, denormalised, so value readers remain a
> single indexed lookup.
>
> The advice in this document to model a time series as "one annotation per
> field and timestep" is superseded: the timestep is now the assessment's
> `assessed_at`, and the annotation is stated once. The full-coverage v1
> contract is unchanged — it is enforced per assessment, which is why
> `_validate_value_blocks` groups on `(assessment_id, asset_part_id,
> element_kind)`.

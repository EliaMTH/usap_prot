# Design draft — Per-element value fields in USAP

**Status:** draft, not implemented. Captures a design discussion (2026-06-26).
**Owner:** Elia.
**One-liner:** add a second element-level payload type — a *value per element* (a scalar
field over a mesh's faces / a cloud's points) — alongside the existing *set membership*,
so fields like "shadowing per face at hour H" can be stored, queried by value, and
highlighted, **without tying them to a city object**.

---

## 1. Motivation

USAP today stores exactly one kind of element-level data: **set membership** —
"these face/point indices belong to concept C" — as compressed `uint32` index blocks in
`usap_membership_block` (see `src/usap/encoding.py` for the codec; `_materialize` in the
demo backend shows the decode: `zlib.decompress` → `np.frombuffer(..., uint32)` → `+ block_start`).

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
| store | `usap_membership_block` (compressed `uint32` indices) | `usap_value_block` (compressed value array) |
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

## 5. What must be added (practical checklist)

### Schema — `sql/schema.sql`
- `CREATE TABLE usap_value_block (...)` per §4.1.
- Indexes: on `annotation_id`; on `(asset_part_id, element_kind)`.
- (Optional, later) `usap_value_field` metadata table.

### Codec — `src/usap/encoding.py`
- Add `encode_value_block(values: np.ndarray) -> (dtype_str, bytes)` and
  `decode_value_block(dtype_str, payload) -> np.ndarray`, mirroring the existing membership
  block encode/decode helpers here. (Membership = `zlib` of `uint32` offsets; values =
  `zlib` of `ndarray.tobytes()` + a dtype tag.)

### Library API — `src/usap/core.py` (`USAPPackage`), mirroring the membership methods
| existing (membership) | new (value field) |
|---|---|
| `attach_annotation_elements` / `replace_annotation_membership` | `attach_value_field(annotation_id, asset_part_id, element_kind, values, *, dtype=None, chunk=...)` / `replace_value_field(...)` |
| `elements_for_annotation` | `values_for_annotation(annotation_id, *, asset_part_id=None, element_kind=None) -> np.ndarray` |
| `annotations_for_elements` (reverse) | `elements_where(annotation_id, predicate, *, asset_part_id=None) -> list[int]` (predicate = callable or `(op, threshold)`) |
| — | `value_field_stats(annotation_id) -> {min,max,count}` (query defaults / GUI) |

- `constants.py`: a small `value_dtype` whitelist; reuse `element_kind`.
- `batch.py` / `project_builder.py`: optional batch ingestion of value fields (parallel to how
  membership batches are applied) if fields are built in bulk.
- `domain_vocab.py` / `vocabularies/`: an example ADE concept (`Shadowing` / `SolarIrradiance`)
  so the concept is vocabulary-governed like everything else.

### Validation — `src/usap/validation.py`
- value-block `element_count` consistent with the `asset_part` element count (or within range);
- `value_dtype` recognized and payload decodes to that dtype/length;
- block ranges non-overlapping and in bounds; no orphan blocks (annotation exists).
- Add these to `validate_report()`.

### Docs
- `README.md` / `REFERENCE.md`: a "Per-element value fields" section — the membership-vs-value
  distinction, when to use which, and the shadow example.
- This file.

### Tests — `tests/`
- **round-trip:** write a float field → `values_for_annotation` equals the input.
- **query intent:** `elements_where(>, t)` returns *exactly* the elements above `t`
  (assert the set, not just the count) — and assert it works with `primary_city_object_id = NULL`
  (the asset-level, no-city-object case is the whole point).
- **chunking:** a multi-block field reassembles in element order.
- **edit:** `replace_value_field` overwrites; old values gone.
- **validation:** bad dtype / wrong length / out-of-range block is rejected.
- **dtype fidelity:** `u1` / `f2` / `f4` survive round-trip within type precision.

---

## 6. Demo integration (optional, later)
Mirrors the building filter, but simpler (single-stage):
- **Backend:** `POST /api/query/by-value` → pick a value-field annotation + `op` + `threshold`
  → `elements_where(...)` → return the face indices (+ the pure USAP scan time). No city-object
  resolve — the result *is* the geometry.
- **Frontend:** a small control like the building filter, but operating on a face field; reuse
  `applyHighlight` with the returned face indices.
- **Timing bar:** one number ("USAP value scan"), even cleaner than the two-stage building filter.

---

## 7. Open questions / decisions to make
- **Dense vs sparse:** does every element always have a value, or can a field be partial?
  → sentinel/`NaN` vs an accompanying membership set marking "has value".
- **dtype policy:** fixed whitelist (`f4, f2, u1, i2, …`); stored per block.
- **Metadata home:** annotation `attributes_json` (start here) vs a dedicated `usap_value_field`
  table (promote only if needed).
- **Inline vs reference:** for truly *bulk / external* simulation output you don't query
  element-wise inside USAP, store the annotation + a **reference** (URI/blob id) instead of an
  inline block. Decide per field by size, authoritativeness, and whether in-package value queries
  are actually needed. (These requirements — exact values + in-package value queries — point to
  **inline**.)
- **Multi-component fields** (RGB, vector per element): a `component_count` column or separate
  blocks. **Out of scope for v1** (scalar only).

---

## 8. Scope boundary (v1)
**In:** scalar value per element, one `asset_part`, chunked blocks, `zlib` payload,
decompress-and-scan query, whole-block edit, an ADE concept, validation, tests.
**Out (later):** acceleration index (sort-permutation / bins), multi-component fields,
external-reference fields, sparse overlays for edits.

**Guiding principle:** the value-block is the *only* new primitive. The query's output is the
existing set/membership shape, so nothing downstream (highlight, filters, closures) has to bend.

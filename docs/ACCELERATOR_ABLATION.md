# Storage vs. query: which USAP tables are actually necessary?

This document records a design investigation (2026-07) into three questions:

1. If USAP only had to **store** annotations efficiently (no query layer),
   which tables are the irreducible minimum — and is there still novelty in
   that concept alone?
2. Which tables serve which USAP query?
3. Are the "query tables" (closures, block pruning, stored min/max) actually
   **necessary**, or could smart queries over the base tables replace them?

Question 3 was answered empirically with an ablation benchmark:
[scripts/benchmark_accelerator_ablation.py](../scripts/benchmark_accelerator_ablation.py)
(the script writes per-scale reports to whatever `--md`/`--json` paths you
pass; `outputs/` is gitignored, so the numbers quoted below are **not**
accompanied by their archived raw reports, exact environment, or run
dispersion — re-run the script to reproduce them).

---

## 1. The minimum storage-only schema

Four tables are irreducible; a fifth is arguable:

| Table | Why it is irreducible |
|---|---|
| `usap_asset` | Anchors claims to an external file (`uri` + `content_hash`). |
| `usap_asset_part` | Declares the index space (element kind, count, origin). An index set is meaningless without it. |
| `usap_annotation` | The claim itself (concept, status, confidence). |
| `usap_membership_block` | The payload — which elements. |
| `usap_semantic_class` | Arguable: could collapse to a `class_uri` column on the annotation. |

Everything else is droppable in a storage-only view:

- `usap_semantic_class_closure`, `usap_city_object_closure` (removed in
  0.1.0 as a result of this investigation, see §4) — derivable
  accelerators (from `parent_class_id` / relationship edges).
- `usap_city_object` + `usap_city_object_relationship` +
  `usap_annotation_object` — the identity layer; needed only for
  "which object" claims (the relationship table is base data, not derivable).
- `usap_value_block` — a separate feature (value fields).
- `usap_edit_log` — provenance only; serves no query.
- `usap_profile` — the uniform-block-size contract is a *query* concern;
  each membership block row carries its own `block_size`/`encoding`.
- `usap_asset_extent` + `gpkg_*` + views — GIS interop.

Note that in a storage-only framing even the blocked layout loses its
justification: blocks partitioned at a global size exist so the reverse query
can compute candidate `block_start`s. Pure storage would be one compressed
delta-encoded array per (annotation, asset part, kind).

**Novelty assessment (storage-only).** Every individual ingredient is known
prior art: standoff annotation (W3C Web Annotation, brat), compressed integer
sets (roaring bitmaps — which USAP now uses directly rather than merely
resembling), labels-outside-the-asset via stable indices (ScanNet
`.segs.json`/`.aggregation.json`), per-element IDs → property tables (glTF
`EXT_mesh_features`/`EXT_structural_metadata`), LAS classification codes.
What survives the storage-only cut as genuinely uncommon:

1. **Cross-representation claims** — one annotation carrying membership in
   LAS points *and* mesh faces *and* a triangulation simultaneously. This is
   USAP's strongest single ingredient, and it is purely a storage-model
   property.
2. Annotation-as-revisable-claim (status/confidence/attributes,
   CityGML/ADE vocabulary as data) rather than label-as-baked-in-attribute.
3. The GeoPackage packaging — one GIS-openable file.

Verdict: niche but real, and load-bearing on the **combination** — stripped
to single-representation index sets in SQLite, it would be an engineering
composition, not a new concept.

## 2. Which tables serve which query

| Query (`core.py`) | Tables doing the work | Accelerator used |
|---|---|---|
| `annotations_for_elements` (pick → claims) | `usap_membership_block`, `usap_profile` | Global block size → candidate `block_start`s; index `usap_mb_by_element_block`; only candidate payloads decoded. |
| `elements_for_annotation` | `usap_membership_block` | `UNIQUE(annotation_id, …)` auto-index. |
| `elements_for_semantic_class` | `usap_semantic_class_closure`, `usap_annotation`, `usap_membership_block` | Closure PK expands "class + subclasses"; `usap_annotation_by_class`. |
| `elements_for_city_object` | `usap_city_object`, `usap_city_object_relationship`, `usap_annotation_object` ∪ `primary_city_object_id`, `usap_membership_block` | Recursive CTE expands descendants per graph, following containment edge types only. |
| `values_for_annotation`, `elements_where`, `value_field_stats` | `usap_value_block` (+ `usap_asset_part` coverage contract) | Per-block `value_min`/`value_max` skipping; stats never decodes a payload. |
| `list_*` browse queries | `usap_annotation`, `usap_city_object`, `usap_semantic_class` + link tables | Tree expansion uses relationship edges directly (not closure). |
| GIS browse (QGIS) | `usap_asset_extent`, `gpkg_*`, views | — |

Serving no query: `usap_edit_log`. Also: `usap_membership_block.min_element_index`
/ `max_element_index` are **written on every block but read by no query**
(`annotations_for_elements` prunes by `block_start` only) — a dead accelerator.

## 3. Ablation experiment

### Method

`scripts/benchmark_accelerator_ablation.py` builds a synthetic package
(`create_synthetic_package`), then extends it so every accelerator has real
work: Building becomes parent of Roof/Wall/Ground (the generator leaves all
classes as roots), buildings are grouped under districts under one root
(object tree depth 3), and two whole-part `f4` value fields are written — a
gradient (min/max pruning can skip blocks) and uniform noise (it cannot).

Each ablation runs the accelerated SDK query and a **naive equivalent using
only base tables** (recursive CTEs, full decode), asserts the results are
**identical** (any mismatch aborts the run), and times both. Scales tested:
100 / 500 / 2000 buildings = 50k / 250k / 1M faces (6,059 membership blocks at
the largest, at `block_size` 16384). All equality assertions passed at every
scale — **every USAP query is answerable from the base tables alone.**

### Results (2000 buildings, 1M faces, repeat=5, mean ms)

Re-measured after membership moved to roaring bitmaps at `block_size` 16384
(6,059 blocks); the original `u32-zlib` @4096 figures are kept alongside for
comparison. A1 and A5 are the rows the codec change touches.

| Ablation | Accelerated | Naive | Speedup | was (u32-zlib @4096) |
|---|---:|---:|---:|---|
| A1 block pruning — 100-face pick, 1 block | 0.43 | 72.2 | **167×** | 0.30 / 251× |
| A1 block pruning — 1000 faces across 20 blocks | 7.6 | 79.1 | **10×** | 5.5 / 13× |
| A2 semantic-class closure vs recursive CTE | 20.3 | 19.3 | 1.0× (CTE parity) | 18.5 / 0.8× |
| A3 city-object closure vs recursive CTE (root, depth 3) | — | — | — | 35.1 / 0.9× (closure removed in 0.1.0) |
| A4 value min/max skipping — gradient field (13/16 blocks skipped) | 4.2 | 13.8 | 3.3× | 2.8 / 4.1× |
| A4 value min/max skipping — uniform noise (0/16 skipped) | 18.5 | 23.8 | 1.3× | 15.4 / 1.1× |
| A5 `value_field_stats` stored min/max vs full decode | 0.06 | 13.0 | **221×** | 0.04 / 245× |

The A1 *ratios* fall because roaring speeds the naive path up too — it decodes
every block, and roaring decodes ~4.5× faster per block than zlib+`intersect1d`
(2.0 µs vs 9.2 µs on a 300-element block). Measured at equal block size the
codec makes the accelerated path faster as well: 0.44 → 0.22 ms one-block and
8.5 → 2.0 ms many-block, both @4096. The residual gap at 16384 is the coarser
pruning that width buys, traded for the bitmap container (see
`constants.DEFAULT_BLOCK_SIZE`).

Scaling of the A1 one-block pick (accelerated / naive, ms). The accelerated
path is flat in asset size — it reads one block whatever the package holds —
while the naive path grows with the block count, which is the whole point:

| Scale | blocks | A1 one-block pick | was (u32-zlib @4096) |
|---|---:|---|---|
| 50k faces | 303 | 0.64 / 3.5 (5×) | 0.31 / 3.8 (12×) |
| 250k faces | 1,514 | 0.41 / 19.5 (48×) | 0.31 / 18.8 (60×) |
| 1M faces | 6,059 | 0.43 / 72.2 (167×) | 0.30 / 75.6 (251×) |

A3 is absent: the city-object closure it measured was removed in 0.1.0 (§4).

Accelerator costs at 1M faces: class closure 11 rows; package 10.70 MiB
(13.11 MiB before roaring — the membership table alone went 1,960 KB → 324 KB
of pages, and its payloads 1,557 KB → 89 KB).

### The A3 planner trap (why the naive CTE needs care)

The first naive descendant CTE (plain `JOIN`) took **6.8s** at 1M faces —
160× slower than the closure. Decomposition showed the recursive traversal
*alone* took 6.0s over just 8k edges: SQLite's planner chose
`usap_rel_by_to_graph` (then named `usap_rel_by_child_graph`) on its
`graph_name` prefix and rescanned every
edge per recursive step. Rewriting the recursive step as
`FROM descendants AS d CROSS JOIN usap_city_object_relationship AS r`
(SQLite's `CROSS JOIN` pins join order) dropped traversal to **10ms**, after
which the closure-free query slightly *beats* the SDK's closure-backed one.
Same logical query, ~400× apart on the spelling.

## 4. Verdicts

| Structure | Verdict |
|---|---|
| Membership block partitioning + `usap_mb_by_element_block` | **Necessary for the tested workload.** 13–251× on the interactive reverse query; the win comes from the storage layout itself and cannot be recovered by smarter SQL (the cost is payload decoding, not query planning). |
| `usap_semantic_class_closure` | **Not performance-necessary.** The recursive CTE over `parent_class_id` is marginally faster at every scale tested. |
| `usap_city_object_closure` | **Removed.** Not performance-necessary — a well-written CTE matches it — and it was the source of two silent-wrong-answer bugs (§4.1). |
| `value_min`/`value_max` skipping | Cheap, worth keeping; pays 4× on structured data, ~nothing on uniform data. |
| SQL-only `value_field_stats` | Huge ratio (245×) but naive is only ~10ms at 1M values — convenience, not necessity. |
| `min/max_element_index` on membership blocks | **Dead weight** — written always, read never. Drop or start using. |

### 4.1 Why `usap_city_object_closure` was removed (decision, 0.1.0)

The benchmark above says the closure is a performance wash. It was removed
anyway, and **performance was not the deciding argument** — correctness was.
A stored transitive closure is derived state that must be rebuilt to stay
true, and two defects followed directly from that:

- an object created without edges got no self-row, so it vanished from the
  default `include_descendants=True` query while validation called the
  package valid;
- the rebuild selected parent/child ids only, ignoring `relationship_type`,
  so a non-containment edge (`adjacentTo`) made its target a *part* of the
  source and handed back the neighbour's elements.

Neither is fixable by being more careful: the first is a rebuild the write
path did not owe, the second requires the containment-type policy to be
decided at write time, which freezes it into stored rows. Walking the edges
puts both where they belong — the policy becomes a query argument
(`relationship_categories` / `relationship_types`), and there is nothing to
keep in step. The rebuild that
`link_city_objects` triggered per edge (O(objects x depth), full table
rewrite) also disappears, which matters at city scale.

The costs in §5 below were accepted knowingly, with these mitigations:

- **planner fragility** — the `CROSS JOIN` spelling now lives in exactly one
  place (`_descendants_cte` in `core.py`) with the reason in its docstring,
  and this benchmark still aborts on any accelerated/naive mismatch;
- **third-party queryability** — `list_city_objects(descendants_of=...)`
  answers "this object and its parts" from the SDK; raw-SQL consumers do
  have to write the recursive CTE themselves, which is a genuine regression
  for QGIS/DB-Browser users and the main price paid here;
- **read-time cost** — accepted; USAP object graphs are shallow (building →
  part → surface → opening).

> **Profile 0.3.0 note.** The column names quoted throughout this document
> (`parent_city_object_id`, `child_city_object_id`, `relationship_type`) are
> the ones in force when the measurements were taken; they are now
> `from_city_object_id`, `to_city_object_id` and a `relationship_type_id`
> foreign key. The measurements and the decisions still hold — the recursive
> CTE is unchanged apart from those names, and its `CROSS JOIN` is still what
> keeps the planner off the 400x path.
>
> The one thing 0.3.0 adds is `usap_relationship_type.category`, which decides
> what counts as containment. That is *not* the materialized state rejected in
> section 4.1: it is a property of the vocabulary (a few hundred rows), never
> of an edge, so no edit to any edge can invalidate it, and every query may
> still override it. What 4.1 rejected was a per-edge closure that had to be
> rebuilt — and the second bug it lists, a rebuild ignoring `relationship_type`
> and making an `adjacentTo` target a *part*, is now structurally impossible:
> the type is a foreign key and traversal filters on its category.

## 5. Downsides of replacing closures with smart queries

*(The analysis as written before the §4.1 decision — kept because the trade
is real and was made with these arguments in view, not against them.)*

1. **Planner fragility (demonstrated).** 10ms vs 6s on the spelling of one
   join; depends on `CROSS JOIN`'s SQLite-specific order-pinning; planner
   behavior varies across the SQLite versions bundled with different Pythons.
   A closure read has no plan to get wrong.
2. **Third-party queryability degrades.** A USAP package is a plain
   GeoPackage; today "object + descendants" is a trivial SELECT for QGIS /
   DB Browser / R. As a recursive CTE every consumer must know the magic
   spelling — and a view can't hide it, since views take no parameters and
   would recompute the full closure per read. The materialized closure is
   part of the format's interop contract.
3. **Cost moves from write-time to read-time.** Paid per query instead of
   per edit, scaling with the traversed subgraph. USAP is read-heavy, which
   favors precomputation as packages grow.
4. **Complexity relocates rather than disappearing** — from tested Python
   rebuild code to hand-tuned SQL with a "do not remove this CROSS JOIN"
   dependency.

**Overall:** at prototype scale the performance argument is a wash, so the
closure decision rests on (1) and (2) — which favored keeping the closures.
The correctness argument in §4.1 outweighed them for the *object* closure,
which was removed; `usap_semantic_class_closure` is not affected (class
parentage is seeded, not edited edge by edge, and carries no type policy) and
stays. The other simplification candidate found here is the dead
`min/max_element_index` columns.

## Reproducing

```bash
python scripts/benchmark_accelerator_ablation.py --buildings 2000 --repeat 5 \
    --md outputs/ablation_2000_buildings.md
```

Any naive/accelerated result mismatch aborts the run, so a completed run
establishes that the accelerators and the base tables answer the tested
queries equivalently over the tested fixtures. That is an equivalence check,
not a proof of functional completeness in general.

> **Profile 0.4.0 note.** Membership and value blocks gained an `assessment_id`
> owner, and the `UNIQUE(annotation_id, ...)` constraint whose auto-index
> served the annotation-first lookups measured here is now scoped to the
> assessment. That index did not disappear: it is declared explicitly as
> `usap_mb_by_annotation` / `usap_vb_by_annotation` over the same columns in the
> same order, and `annotation_id` stays denormalised on the block so no forward
> query has to join `usap_assessment` to reach it. The reverse-query index
> (`usap_mb_by_element_block`) and the recursive-CTE `CROSS JOIN` — the two
> things this document actually measures — are untouched.

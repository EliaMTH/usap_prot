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
(per-scale reports in `outputs/ablation_*_buildings.md`).

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

- `usap_semantic_class_closure`, `usap_city_object_closure` — derivable
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
sets (roaring bitmaps), labels-outside-the-asset via stable indices (ScanNet
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
| `elements_for_city_object` | `usap_city_object`, `usap_city_object_closure`, `usap_annotation_object` ∪ `primary_city_object_id`, `usap_membership_block` | Closure PK expands descendants per graph. |
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
100 / 500 / 2000 buildings = 50k / 250k / 1M faces (312 / 1,560 / 6,239
membership blocks). All equality assertions passed at every scale — **every
USAP query is answerable from the base tables alone.**

### Results (2000 buildings, 1M faces, repeat=5, mean ms)

| Ablation | Accelerated | Naive | Speedup |
|---|---:|---:|---:|
| A1 block pruning — 100-face pick, 1 block | 0.30 | 75.6 | **251×** |
| A1 block pruning — 1000 faces across 20 blocks | 5.5 | 72.8 | **13×** |
| A2 semantic-class closure vs recursive CTE | 18.5 | 15.4 | 0.8× (CTE faster) |
| A3 city-object closure vs recursive CTE (root, depth 3) | 35.1 | 32.3 | 0.9× (CTE faster) |
| A4 value min/max skipping — gradient field (13/16 blocks skipped) | 2.8 | 11.5 | 4.1× |
| A4 value min/max skipping — uniform noise (0/16 skipped) | 15.4 | 17.5 | 1.1× |
| A5 `value_field_stats` stored min/max vs full decode | 0.04 | 9.9 | 245× |

Scaling of the two headline cases (accelerated / naive, ms):

| Scale | A1 one-block pick | A3 closure vs CTE |
|---|---|---|
| 50k faces | 0.31 / 3.8 (12×) | 1.3 / 1.2 (0.9×) |
| 250k faces | 0.31 / 18.8 (60×) | 7.2 / 6.2 (0.9×) |
| 1M faces | 0.30 / 75.6 (251×) | 35.1 / 32.3 (0.9×) |

Accelerator costs at 1M faces: city-object closure = 30,089 rows vs 8,044
relationship rows (3.7× row amplification), rebuild 0.07s; class closure
11 rows; package 13.11 MiB.

### The A3 planner trap (why the naive CTE needs care)

The first naive descendant CTE (plain `JOIN`) took **6.8s** at 1M faces —
160× slower than the closure. Decomposition showed the recursive traversal
*alone* took 6.0s over just 8k edges: SQLite's planner chose
`usap_rel_by_child_graph` on its `graph_name` prefix and rescanned every
edge per recursive step. Rewriting the recursive step as
`FROM descendants AS d CROSS JOIN usap_city_object_relationship AS r`
(SQLite's `CROSS JOIN` pins join order) dropped traversal to **10ms**, after
which the closure-free query slightly *beats* the SDK's closure-backed one.
Same logical query, ~400× apart on the spelling.

## 4. Verdicts

| Structure | Verdict |
|---|---|
| Membership block partitioning + `usap_mb_by_element_block` | **Necessary.** 13–251× on the interactive reverse query; the win comes from the storage layout itself and cannot be recovered by smarter SQL (the cost is payload decoding, not query planning). |
| `usap_semantic_class_closure` | **Not performance-necessary.** The recursive CTE over `parent_class_id` is marginally faster at every scale tested. |
| `usap_city_object_closure` | **Not performance-necessary** — a well-written CTE matches it. Its real value is robustness and interop (below). |
| `value_min`/`value_max` skipping | Cheap, worth keeping; pays 4× on structured data, ~nothing on uniform data. |
| SQL-only `value_field_stats` | Huge ratio (245×) but naive is only ~10ms at 1M values — convenience, not necessity. |
| `min/max_element_index` on membership blocks | **Dead weight** — written always, read never. Drop or start using. |

## 5. Downsides of replacing closures with smart queries

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
closure decision rests on (1) and (2) — both favor keeping the closures.
The genuine simplification candidate found by this investigation is not a
table but the dead `min/max_element_index` columns.

## Reproducing

```bash
python scripts/benchmark_accelerator_ablation.py --buildings 2000 --repeat 5 \
    --md outputs/ablation_2000_buildings.md
```

Any naive/accelerated result mismatch aborts the run; a completed run is
itself the functional-completeness proof.

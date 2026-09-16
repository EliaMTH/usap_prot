# Design draft — Ordered element paths in USAP

Status: **implemented** in profile 0.5.0 (`usap_path_block`). This document
records the reasoning; `docs/REFERENCE.md` is the normative description of
what shipped. Where the two differ, REFERENCE is right.

## 1. Motivation

USAP stores an annotation's elements as a **set**. Membership is a roaring
bitmap (`usap_membership_block`, codec in `src/usap/encoding.py`), and
`as_index_array` sorts and de-duplicates every write before it is encoded. The
set comes back ascending, always.

That is right for almost everything. "These 400 faces are a RoofSurface" has no
order to lose.

It is wrong for a claim whose membership has a **direction**:

> A road centreline over a mesh is faces 28299 → 28300 → 28301 → … *in that
> order*, from one end to the other. A traversal, a route, a polyline. Stored as
> a set, there is no first element, no last, and no order in between — and the
> order was not hidden, it was destroyed at write time.

### Requirements (agreed)

- **Order is data**, not presentation. It must round-trip exactly.
- **Repeats are legal.** A route may cross the same face twice; the path is a
  sequence, not a permutation of a set.
- **Disjoint runs.** One claim may be several separate stretches, and the gap
  between them is meaningful.
- **A run may cross asset parts** — see §3, this is the requirement the interim
  convention cannot meet at all.
- **Membership must stay derivable and must not drift.** Every existing reverse
  query (`annotations_for_elements`, `elements_for_*`) reads membership blocks
  and must keep working untouched.

---

## 2. Key concept: set vs sequence

| | element **set** (have) | element **sequence** (new) |
|---|---|---|
| meaning | which elements are C | in which order the claim traverses them |
| shape | sparse set, ascending, unique | ordered list, repeats allowed |
| store | `usap_membership_block` (roaring) | `usap_path_block` (ordered `uint32`) |
| good for | surfaces, regions, categories | centrelines, routes, traversals |

The two are not alternatives. A path-bearing annotation has **both**: the
sequence is the source, and the membership is its index. Everything that reads
membership today keeps reading membership.

**Roaring cannot be reused** for the sequence. `encode_roaring` calls
`as_index_array`, which sorts and de-duplicates — the two operations the
sequence exists to survive.

---

## 3. Why `usap:path` in `attributes` is not enough

0.5.0 documented an interim home for the sequence: a reserved `usap:path` key
inside an annotation's `attributes`, holding a list of lists of absolute
indices (REFERENCE.md § Annotation).

```json
"usap:path": [[28299, 28300, 28301, 28302, 28303]]
```

It buys interpretability — a reader who did not write the package can read it —
and nothing else. Three weaknesses, in increasing order of severity:

1. **Nothing validates it.** An index past the part's `element_count` is
   rejected instantly as membership and stored silently inside this key.
2. **It drifts.** Rewriting the membership leaves the path describing the old
   selection, with nothing to notice.
3. **It cannot name the asset part its indices belong to.** ← the blocker

### 3.1 Why (3) is the one that decides this

Element indices **restart at zero in every asset part**. The mesh adapter says
so in the metadata it writes (`src/usap/adapters/mesh_adapter.py`):
`"indexing": "zero_based_face_order_in_this_geometry"`.

```
part 7   road_tile_a    50 000 faces   indices 0 … 49999
part 8   road_tile_b    30 000 faces   indices 0 … 29999
```

Face `12` exists in both and is a different triangle in each. Now a road
crossing the tile boundary:

```json
[[ ..., 49998, 49999, 0, 1, 2, ... ]]
```

Are those trailing `0, 1, 2` part 7's faces (the route jumped back to the start
of the first tile) or part 8's (it continued into the second)? A bare list of
integers has nowhere to say. The key is well defined **only** when the whole
annotation lives in a single `(asset part, element kind)` pair.

This is not a rule that can be tightened. A JSON value carrying only integers
has no slot for the part, and adding one means inventing a nested object format
— at which point it is a table with worse ergonomics and no constraints.

**A column can say it. That is the entire reason this is a table.**

Once it is a table, (1) and (2) come along for free: the write path can
range-check indices exactly as membership does, and it can compute the
membership *from* the path in the same transaction.

### 3.2 What the real data says

Measured on `data/catania.usap.gpkg` (profile 0.4.0):

| | |
|---|---|
| annotations / assessments / membership blocks | 19 691 / 21 332 / 26 004 |
| annotations spanning >1 asset part | **1 641** (8.3%) |
| assessments spanning >1 asset part | **0** |
| assets, and parts per asset | 3 assets, **one part each** |
| annotations carrying `usap:path` | 0 |

The 1 641-vs-0 split is structural, not luck. The two parts are `catania.las`
and `catania.obj` — **different assets**. An assessment binds to an asset, so a
claim evaluated against both representations becomes two assessments of one
annotation, each touching exactly one part. Multi-part at the annotation level,
single-part at the assessment level.

A path lives at the assessment level, so in 21 332 real assessments the
cross-part case occurs **zero** times — because the mesh adapter registers one
part per geometry and `catania.obj` holds a single unnamed geometry
(`geometry/0:default`). A route over a one-geometry mesh cannot cross parts.

**This does not settle the requirement.** Catania is not Matera, and
`strade_matera.usap.gpkg` (profile 0.1.0, no longer openable) is not in the
repo. The sharp question for the consumer is not "do your polylines cross asset
parts" but:

> Is your road mesh exported as several geometries, and is that split **spatial**
> (tiles) or **thematic** (one geometry for roads, one for buildings)?

Only a spatial split puts one road in two parts. A thematic split keeps every
road inside one part however long it is, and is the more common export from a
city pipeline.

§4.1 resolves this so the answer is not needed before building.

---

## 4. Design

### 4.1 Storage — `usap_path_block`

```sql
CREATE TABLE usap_path_block (
    path_block_id       INTEGER PRIMARY KEY,

    assessment_id       INTEGER NOT NULL
        REFERENCES usap_assessment(assessment_id)
        ON DELETE CASCADE,

    asset_part_id       INTEGER NOT NULL
        REFERENCES usap_asset_part(asset_part_id)
        ON DELETE CASCADE,

    segment_ordinal     INTEGER NOT NULL,
    continues_previous  INTEGER NOT NULL DEFAULT 0
        CHECK (continues_previous IN (0, 1)),

    element_count       INTEGER NOT NULL,
    encoding            TEXT NOT NULL DEFAULT 'u32-seq-zlib',
    payload             BLOB NOT NULL,

    UNIQUE(assessment_id, segment_ordinal)
);

-- Serves the ON DELETE CASCADE scan from usap_asset_part. The forward read
-- (WHERE assessment_id = ? ORDER BY segment_ordinal) is served by the UNIQUE
-- autoindex above; adding an index for it would be flagged by
-- tests/test_geopackage.py::test_no_explicit_index_duplicates_a_unique_autoindex.
CREATE INDEX usap_path_by_part
ON usap_path_block(
    asset_part_id
);
```

A row is one **segment**: a contiguous stretch of the sequence whose indices all
live in one asset part. Read order is `ORDER BY segment_ordinal`.

**A run** — one disjoint stretch of the path, the inner list of `usap:path` — is
a maximal span of segments in which every segment after the first has
`continues_previous = 1`. A segment with `continues_previous = 0` starts a new
run. Segment 0 must always be 0.

```
segment_ordinal  0    part 7   [..., 49998, 49999]   continues_previous 0   ┐ run 0
segment_ordinal  1    part 8   [0, 1, 2, ...]        continues_previous 1   ┘
segment_ordinal  2    part 8   [500, 501]            continues_previous 0     run 1
```

#### Why these columns and not others

**`assessment_id`, not `annotation_id`.** Membership is assessment-scoped
(`UNIQUE(assessment_id, asset_part_id, element_kind, block_start)`). An
annotation with two assessments has two element sets — the 2026 survey and the
2027 one. A path keyed on the annotation could not say which evaluation it
orders, and the invariant in §4.3 would have two membership sets to test
against. It would also outlive `delete_assessment`, surviving the deletion of
the only membership it described.

**No `annotation_id`.** Recoverable through `usap_assessment.annotation_id`.
The siblings denormalise it to keep forward queries on a tuned single-index plan
over millions of blocks (`docs/ACCELERATOR_ABLATION.md`); a path is a handful of
rows per annotation and the join is served by `usap_assessment_by_annotation`.
The cascade still reaches it transitively — `path → assessment → annotation` —
and both `delete_annotation` and `delete_assessment` already rely on nothing but
`ON DELETE CASCADE`. Not storing it also removes the need for the check that
`ASSESSMENT_ANNOTATION_MISMATCH` performs on the siblings.

**No `element_kind`.** `usap_asset_part` declares `UNIQUE(asset_id, part_path,
element_kind)`, so `asset_part_id → element_kind` is a functional dependency.
The siblings denormalise it to serve `usap_mb_by_element_block`, a reverse-query
index this table has no equivalent of — a path is read forward only. Not storing
it removes the failure mode `MEMBERSHIP_ELEMENT_KIND_MISMATCH` exists to catch.

**`asset_part_id`, which is *not* recoverable.** An assessment binds to an
**asset**, not a part, and one asset may register many parts. Where it registers
exactly one, the part is recoverable — but that is precisely the case
`usap:path` already handled correctly. The case that needs this table is the
case where recovery fails. It is also circular with §4.3: deriving the
membership requires range-checking indices against the part's `element_count`,
so if the part were only knowable by reading the membership, the first write
would have nothing to read, and a path could never move to a different part.

**`segment_ordinal` + `continues_previous`, not `run_ordinal` + `seq_start`.**
Both express the same thing. This pairing makes the run boundary explicit data
instead of something inferred from ordinal grouping, gives a simpler key
(`UNIQUE(assessment_id, segment_ordinal)`), and — the reason it was chosen —
makes the single-part case the degenerate case rather than a second schema. If
every run lives in one part, every `continues_previous` is 0 and
`segment_ordinal` *is* the run ordinal: no unused column, no nesting in the API
(§4.4), and no profile bump the day a spatially tiled mesh appears. It also
gives chunking of very large runs for free, by the same mechanism.

**`element_count` counts positions in the payload**, which may exceed the
membership it derives, because a path may revisit an element. It also gives the
exact expected decompressed size, so the decode ceiling can be exact the way
`decode_value_block`'s is rather than falling back on `MAX_DECOMPRESSED_BYTES`.

### 4.2 Codec — `u32-seq-zlib`

Contiguous little-endian `uint32`, zlib at the default level, standard zlib
header, no count prefix. Element count is `len(decompressed) / 4`.

That is **byte-identical to the historical `u32-zlib` layout** already specified
in REFERENCE.md § "The historical `u32-zlib` encoding", which is why it costs
nothing to specify and little to implement: the consumer's C++ reader already
decodes this layout.

It carries a **different token** because three rules are inverted:

| | `u32-zlib` (legacy membership) | `u32-seq-zlib` (path) |
|---|---|---|
| values are | offsets relative to `block_start` | **absolute** element indices |
| order | strictly ascending | **sequence order** |
| duplicates | forbidden | **allowed** |

The names must differ for three reasons, none of them about the bytes:

- `u32-zlib` is documented as **out of support** — this SDK refuses the profile
  of packages carrying it. "We no longer decode `u32-zlib`" and "path blocks are
  `u32-zlib`" cannot both stand in REFERENCE.md.
- `encoding` is a **per-row** column precisely so a codec can change without a
  profile bump (the lesson of the 0.1.0 packages that hold either codec). One
  string meaning two things in two tables makes the next codec decision twice.
- Validation would accept and reject the same string a few hundred lines apart
  (`UNSUPPORTED_MEMBERSHIP_ENCODING` rejects it; the path check would accept it).

Deliberately **not** delta/zigzag/varint for v1. It would beat plain `uint32` on
a near-monotonic path by roughly 2× in the good case and nothing in the bad one,
at the cost of a new normative spec section and new consumer-side code, in a
release that already forces every package to be rewritten. The per-row
`encoding` column means a better codec can land later with no profile bump at
all.

### 4.3 Derivation — membership from path

**The path is the source; the membership is its index.** `set_annotation_path`
writes both in one transaction:

1. group the path's segments by `asset_part_id`;
2. for each group, `element_kind` comes from the part row;
3. concatenate the group's indices, then `as_index_array` — which sorts and
   de-duplicates, so the derived membership is `sorted(set(path))` per part;
4. range-check against the part's `element_count`, via the existing
   `_validate_membership_indices`;
5. write the membership blocks through the existing `replace_annotation_membership`
   path, so every reverse query keeps working unchanged.

Membership is written **eagerly**, as real rows, not as a view. The reverse
element query is what the whole storage layout was tuned around; deriving it
lazily would put a decode of every path segment in front of it. The cost is that
the same indices are stored twice — once ordered, once as a bitmap — which is
accepted.

**The reverse direction must be blocked, or the invariant is a convention
again.** `replace_annotation_membership` and `attach_annotation_elements` raise
when the target `(assessment, asset part)` already carries path segments, unless
called with `drop_path=True` — the explicit "I re-lassoed this, the order is
gone" escape. Silently leaving a stale path and silently discarding one are both
worse than the interim.

That rule is enforceable only inside this SDK, and the consumer writes
GeoPackages from C++. So the check in §4.5 is not redundant with it — it is the
half that reaches third-party writers.

### 4.4 API

Two new methods. No flags on existing readers.

```python
pkg.set_annotation_path(annotation_id, segments, *, assessment=None)
pkg.path_for_annotation(annotation_id, *, assessment=None, expand=True)
```

`segments` is a **flat list**, mirroring the table, with `continues_previous`
omitted in the ordinary case:

```python
segments = [
    {"asset_part_id": 7, "element_indices": [...]},
    {"asset_part_id": 8, "element_indices": [...], "continues_previous": True},
    {"asset_part_id": 8, "element_indices": [...]},
]
```

`assessment=None` defaults through `_resolve_write_assessment`, exactly as every
other write does, so a caller who has never heard of assessments never names
one. `set_annotation_path(annotation_id, None)` drops the path and leaves the
membership as a plain set — so there is no third method.

`path_for_annotation` returns one entry per **run**, in path order, each
carrying its segments in order — so a caller sees the disjoint stretches
directly rather than having to fold `continues_previous` itself. It follows
`elements_for_annotation`'s `expand` convention: compact row metadata, or
decoded indices.

#### Discovery: unconditional, in the summaries that already exist

`_assessment_summary` already computes `selected_count` per assessment with a
correlated subquery. `path_run_count` goes beside it the same way, and into
`_annotation_membership_summary` and `_assessment_membership_summary` per part.

`get_annotation`, `list_annotations`, `list_assessments` and
`annotations_for_elements` then all report whether a path exists, from the call
an application already makes, with no argument and no second query:

```
read the annotation as today
  → result carries path_run_count: 3
  → call path_for_annotation if you care
```

`path_run_count: 0` means there is nothing to fetch, and an application that
ignores the field behaves exactly as it does now.

#### Why not a flag on the element readers

`elements_for_annotation(..., ordered=True)` was considered and rejected. It
cannot work, for a structural reason: that call returns **one dict per membership
block**, and blocks are partitions of index space at
`default_block_size` (16 384). A path does not respect those boundaries. For
`[16000, 16400, 16001]` the membership lands as block 0 holding two indices and
block 1 holding one; emitting path order means yielding 16000 (block 0), 16400
(block 1), 16001 (block 0 again), which cannot be produced while grouping by
block. A path interleaves the very structure the return value is built from.

So the flag could only return a *different* structure — a separate method in
disguise, whose caller must branch without being able to see that it must.
Three further problems: no defensible behaviour when no path exists (report the
unordered set and the flag lies; raise and the caller must already know the
answer); `asset_part_id=` narrowing would yield a silently truncated route; and
`elements_for_semantic_class` / `elements_for_city_objects` span many
annotations of which only some are ordered, where one flag has no coherent
meaning. It also leaves the write side untouched, which is where the real
problem is — writes are per-part, a path is cross-part.

**No existing read call changes its output shape or its ordering.** Membership
still comes back ascending from every reader that returns it today. The only
addition is one integer in the summary dicts.

### 4.5 Validation

New codes, following the per-code pattern in `src/usap/validation.py`:

| code | severity | check |
|---|---|---|
| `PATH_MEMBERSHIP_MISMATCH` | error | sorted, de-duplicated path ≠ membership, per `(assessment, asset part)` |
| `PATH_OUT_OF_ASSET_PART_RANGE` | error | an index ≥ the part's `element_count` |
| `PATH_OUTSIDE_ASSESSMENT_ASSET` | error | segment's part belongs to another asset — mirrors `MEMBERSHIP_OUTSIDE_ASSESSMENT_ASSET` |
| `UNSUPPORTED_PATH_ENCODING` | error | `encoding` is not `u32-seq-zlib` |
| `CORRUPT_PATH_PAYLOAD` | error | payload does not decompress |
| `PATH_COUNT_MISMATCH` | error | decoded length ≠ declared `element_count` |
| `PATH_SEGMENT_ORDINAL_GAP` | error | ordinals are not contiguous from 0 within an assessment |
| `PATH_FIRST_SEGMENT_CONTINUES` | error | segment 0 has `continues_previous = 1` |
| `ORPHAN_PATH_ASSESSMENT` / `ORPHAN_PATH_ASSET_PART` | error | dangling FK, mirroring the existing orphan family |
| `PATH_IN_ATTRIBUTES` | warning | an annotation still carries `usap:path` — see §5 |

`usap validate` picks all of these up with no CLI change: it delegates to
`validate_report`, which is the single rule set.

### 4.6 Batch format

`apply_annotation_batch` rejects unrecognised keys, so a path key is deliberate
work rather than something that starts working by itself.

`"path"` is a **sibling of `"memberships"`**, not a key inside one: a
`memberships` entry is per-part, and a path is cross-part.

```json
{
  "annotation_uid": "ann_road_via_roma",
  "concept": "RoadCentreline",
  "label": "Via Roma, centreline",
  "path": [
    { "asset_part_id": 7, "element_indices": [49998, 49999] },
    { "asset_part_id": 8, "element_indices": [0, 1, 2], "continues_previous": true }
  ]
}
```

An entry carrying **both** `"path"` and a `"memberships"` entry for a part the
path also covers raises. That combination is the drift this design exists to
prevent, and the membership is derived anyway.

### 4.7 GIS-facing view

`usap_annotations_view` gains `path_run_count`, cast to INTEGER like the other
aggregate columns. It is how a plain-SQL reader — including the consumer's C++
side — tells an ordered annotation from an unordered one without decoding
anything.

---

## 5. What this costs, and the `usap:path` migration

The table is additive, but `CURRENT_PROFILE_VERSION` moves and
`SUPPORTED_PROFILE_VERSIONS` accepts one version, so **packages must be
rewritten**. That is the standing policy — packages are experimental and are
rebuilt, not migrated — and it is the second consecutive bump to ask it.

**`usap:path` stops being a storage location.** The new build rejects it on
write, with an error naming `set_annotation_path`, and `PATH_IN_ATTRIBUTES`
reports packages that still carry it. Automatic conversion on open was
considered and is not possible in general: it cannot name the part when an
annotation spans several, which is the whole reason for the table. Making the
migration loud is better than converting the cases that happen to be convertible
and silently mis-converting the rest.

In `data/catania.usap.gpkg`, zero annotations carry `usap:path` and zero carry
any `attributes_json` at all — so for that dataset the migration moves nothing.

---

## 6. Questions that were open, and how they were settled

1. **Can a run cross asset parts?** Yes, and the schema makes the single-part
   case free: `segment_ordinal` + `continues_previous` rather than
   `run_ordinal` + `seq_start`. When nothing crosses a boundary every
   `continues_previous` is `0`, one row is one run, and the writer never
   mentions the column. The alternative would have cost a nesting level in
   `set_annotation_path`'s argument forever, to express something that occurs
   zero times in 21 332 real assessments — and a profile bump the day a
   spatially tiled mesh appears.

2. **May a path cover only part of a membership?** No. The path's element set
   must *equal* the membership, and `set_annotation_path` derives the
   membership rather than checking it. A partial "order these, leave the rest
   unordered" mode would weaken the invariant to a subset relation, which is
   the thing that cannot then be validated.

3. **`drop_path=True` on `replace_annotation_membership`.** Keyword-only,
   default `False`, same spelling on `attach_annotation_elements`, and
   deliberately *not* on `annotate_elements` — that mints a new annotation, so
   there is never a path to drop. It discards the assessment's whole path, not
   the target part's segments alone: ordinals are contiguous across the
   assessment, so removing the middle would leave the segments either side no
   longer joined up. The drop is written to `usap_edit_log`.

4. **Which profile version.** Folded into 0.5.0 rather than taken as a second
   bump: 0.5.0 had been tagged but not released, so the consumer rebuilds
   packages once instead of twice.

5. **Total path length in the summaries.** Left out. `path_run_count` answers
   "is this ordered, and in how many pieces", which is what decides whether to
   fetch; the length is one decode away for a caller that wants it, and
   `_annotation_path_summary` already carries `position_count` per part.

6. **`EMPTY_PATH_SEGMENT`** was added to the code table during implementation.
   A row with `element_count = 0` and a valid empty payload would otherwise
   validate clean and mean nothing; `EMPTY_VALUE_BLOCK` is the precedent.

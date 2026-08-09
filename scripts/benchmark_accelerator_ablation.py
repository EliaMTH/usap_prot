"""
Accelerator ablation benchmark: are USAP's "query tables" actually necessary?

USAP's minimum storage-only schema is usap_asset / usap_asset_part /
usap_annotation / usap_membership_block (+ usap_semantic_class). Everything
else the queries touch is an accelerator holding no independent information:

    A1  membership block_start pruning (usap_mb_by_element_block index and
        the uniform-block-size contract in usap_profile)
    A2  usap_semantic_class_closure    (derivable from parent_class_id)
    A4  per-block value_min/value_max skipping in elements_where
    A5  SQL-only aggregates in value_field_stats

A3 (usap_city_object_closure) used to be measured here. It was found not to
be performance-necessary, and elements_for_city_object now walks the
relationship edges with the recursive CTE that used to be A3's naive side —
so there is no longer an accelerator to ablate.

For each ablation this script runs the accelerated SDK query and a naive
equivalent that uses only base tables (recursive CTEs, full decode), asserts
the results are identical (the minimal schema answers every query), and
times both sides (what each accelerator is worth). Any result mismatch
aborts the run.

The synthetic package is extended in-script before measuring:
  * Roof/Wall/Ground get Building as parent class (the generator leaves all
    classes as roots, which would make the class-closure ablation trivial).
  * Buildings are grouped under districts under one root object, so the
    object tree has depth 3 instead of 1.
  * Two whole-part value fields are written: a gradient (min/max pruning
    can skip most blocks) and uniform noise (pruning can skip nothing).

Usage:
    python scripts/benchmark_accelerator_ablation.py --buildings 500
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import numpy as np

from usap.constants import CITYGML_3_0_CORE_NS, DEFAULT_GRAPH_NAME
from usap import (
    DEFAULT_SCHEMA_PATH,
    ELEMENT_KIND_FACE,
    SyntheticConfig,
    USAPPackage,
    create_synthetic_package,
)
from usap.encoding import decode_roaring, decode_value_block

MEMBERSHIP_BLOCK_COLUMNS = (
    "membership_block_id",
    "annotation_id",
    "asset_part_id",
    "element_kind",
    "block_start",
    "block_size",
    "encoding",
    "element_count",
    "min_element_index",
    "max_element_index",
)


@dataclass(frozen=True)
class TimingResult:
    name: str
    repeat: int
    mean_ms: float
    min_ms: float
    max_ms: float


@dataclass(frozen=True)
class AblationResult:
    name: str
    accelerated: TimingResult
    naive: TimingResult
    detail: str

    @property
    def speedup(self) -> float:
        if self.accelerated.mean_ms == 0:
            return float("inf")
        return self.naive.mean_ms / self.accelerated.mean_ms


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"

    if size < 1024 * 1024:
        return f"{size / 1024:.2f} KiB"

    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MiB"

    return f"{size / (1024 * 1024 * 1024):.2f} GiB"


def time_operation(name: str, repeat: int, func):
    """
    Like benchmark_phase1.time_operation, but also returns the last result
    so ablations can compare accelerated vs naive outputs.
    """
    durations: list[float] = []
    last_result = None

    for _ in range(repeat):
        start = time.perf_counter()
        last_result = func()
        end = time.perf_counter()

        durations.append((end - start) * 1000.0)

    timing = TimingResult(
        name=name,
        repeat=repeat,
        mean_ms=mean(durations),
        min_ms=min(durations),
        max_ms=max(durations),
    )

    return timing, last_result


def count_rows(pkg: USAPPackage, table_name: str) -> int:
    row = pkg.conn.execute(
        f"SELECT COUNT(*) AS n FROM {table_name}"
    ).fetchone()

    return int(row["n"])


def distinct_block_starts(indices: list[int], block_size: int) -> int:
    return len({(index // block_size) * block_size for index in indices})


def make_spread_indices(
    total_face_count: int,
    block_size: int,
    requested_blocks: int = 20,
    faces_per_block: int = 50,
) -> list[int]:
    """
    A selection spread across several membership blocks (benchmark_phase1's
    Q2 pattern: ideally 1000 faces across 20 blocks).
    """
    available_blocks = math.ceil(total_face_count / block_size)
    block_count = min(requested_blocks, available_blocks)

    indices: list[int] = []

    for block_number in range(block_count):
        block_start = block_number * block_size

        for offset in range(faces_per_block):
            index = block_start + offset

            if index < total_face_count:
                indices.append(index)

    return indices


# ---------------------------------------------------------------------
# Naive query variants (base tables only, no accelerator structures)
# ---------------------------------------------------------------------


def naive_annotations_for_elements(
    pkg: USAPPackage,
    asset_part_id: int,
    element_kind: int,
    selected_indices: list[int],
) -> list[dict]:
    """
    A1 naive: no block_start pruning. Fetch EVERY membership block of the
    asset part, decode every payload, intersect in Python. Only needs the
    membership table itself (plus display joins).
    """
    selected = set(selected_indices)

    rows = pkg.conn.execute(
        """
        SELECT
            mb.annotation_id,
            mb.block_start,
            mb.payload,
            a.annotation_uid,
            a.label,
            a.status,
            sc.local_name AS semantic_class,
            sc.class_uri AS semantic_class_uri,
            co.object_uid AS primary_city_object_uid
        FROM usap_membership_block AS mb
        JOIN usap_annotation AS a
            ON a.annotation_id = mb.annotation_id
        JOIN usap_semantic_class AS sc
            ON sc.semantic_class_id = a.semantic_class_id
        LEFT JOIN usap_city_object AS co
            ON co.city_object_id = a.primary_city_object_id
        WHERE mb.asset_part_id = ?
          AND mb.element_kind = ?
        ORDER BY mb.block_start, mb.annotation_id
        """,
        (asset_part_id, element_kind),
    ).fetchall()

    matches_by_annotation: dict[int, dict] = {}

    for row in rows:
        block_start = int(row["block_start"])
        elements = {
            block_start + offset for offset in decode_roaring(row["payload"])
        }
        hits = selected.intersection(elements)

        if not hits:
            continue

        annotation_id = int(row["annotation_id"])

        if annotation_id not in matches_by_annotation:
            matches_by_annotation[annotation_id] = {
                "annotation_id": annotation_id,
                "annotation_uid": row["annotation_uid"],
                "label": row["label"],
                "status": row["status"],
                "semantic_class": row["semantic_class"],
                "semantic_class_uri": row["semantic_class_uri"],
                "primary_city_object_uid": row["primary_city_object_uid"],
                "matched_elements": [],
            }

        matches_by_annotation[annotation_id]["matched_elements"].extend(hits)

    results = list(matches_by_annotation.values())

    for result in results:
        result["matched_elements"] = sorted(set(result["matched_elements"]))

    return results


def naive_class_descendant_blocks(
    pkg: USAPPackage,
    semantic_class_id: int,
) -> list[dict]:
    """
    A2 naive: recursive CTE over parent_class_id instead of reading
    usap_semantic_class_closure.
    """
    columns = ", ".join(f"mb.{name}" for name in MEMBERSHIP_BLOCK_COLUMNS)

    rows = pkg.conn.execute(
        f"""
        WITH RECURSIVE sub(class_id) AS (
            SELECT ?
            UNION
            SELECT sc.semantic_class_id
            FROM usap_semantic_class AS sc
            JOIN sub ON sc.parent_class_id = sub.class_id
        )
        SELECT {columns}
        FROM usap_membership_block AS mb
        JOIN usap_annotation AS a
            ON a.annotation_id = mb.annotation_id
        WHERE a.semantic_class_id IN (SELECT class_id FROM sub)
        ORDER BY
            mb.asset_part_id,
            mb.element_kind,
            mb.block_start,
            mb.annotation_id
        """,
        (semantic_class_id,),
    ).fetchall()

    return [dict(row) for row in rows]


def fetch_value_blocks(pkg: USAPPackage, annotation_id: int) -> list:
    return pkg.conn.execute(
        """
        SELECT block_start, element_count, value_dtype, payload
        FROM usap_value_block
        WHERE annotation_id = ?
        ORDER BY block_start
        """,
        (annotation_id,),
    ).fetchall()


def naive_elements_where_gt(
    pkg: USAPPackage,
    annotation_id: int,
    threshold: float,
) -> list[int]:
    """
    A4 naive: same predicate loop as elements_where((">", t)) but with no
    value_min/value_max block skipping — every block is decoded.
    """
    hits: list[np.ndarray] = []

    for row in fetch_value_blocks(pkg, annotation_id):
        block = decode_value_block(
            row["payload"], row["value_dtype"], int(row["element_count"])
        )

        with np.errstate(invalid="ignore"):
            mask = block > threshold

            if block.dtype.kind == "f":
                mask = mask & ~np.isnan(block)

        block_hits = np.nonzero(mask)[0]

        if block_hits.size:
            hits.append(block_hits + int(row["block_start"]))

    if not hits:
        return []

    return np.concatenate(hits).tolist()


def naive_value_field_stats(pkg: USAPPackage, annotation_id: int) -> dict:
    """
    A5 naive: full decode + numpy instead of the SQL-only min/max aggregate.
    """
    values = np.concatenate(
        [
            decode_value_block(
                row["payload"], row["value_dtype"], int(row["element_count"])
            )
            for row in fetch_value_blocks(pkg, annotation_id)
        ]
    )

    return {
        "min": float(np.nanmin(values)),
        "max": float(np.nanmax(values)),
        "count": int(values.size),
    }


# ---------------------------------------------------------------------
# Result comparison (the "minimal schema is still queryable" proof)
# ---------------------------------------------------------------------


def canon_reverse_result(results: list[dict]) -> dict:
    return {
        item["annotation_id"]: (
            item["annotation_uid"],
            item["label"],
            item["status"],
            item["semantic_class"],
            item["semantic_class_uri"],
            item["primary_city_object_uid"],
            tuple(item["matched_elements"]),
        )
        for item in results
    }


def canon_block_dicts(results: list[dict]) -> list[tuple]:
    return [
        tuple(item[name] for name in MEMBERSHIP_BLOCK_COLUMNS)
        for item in results
    ]


def require_equal(name: str, accelerated, naive) -> None:
    if accelerated != naive:
        raise RuntimeError(
            f"{name}: naive result differs from accelerated result — "
            f"accelerated has {len(accelerated)} entries, naive has "
            f"{len(naive)}. The ablation is invalid."
        )


# ---------------------------------------------------------------------
# Package setup on top of create_synthetic_package
# ---------------------------------------------------------------------


def deepen_package(pkg: USAPPackage, result, config: SyntheticConfig) -> dict:
    """
    Extend the synthetic package so every accelerator has real work to do.
    Returns setup metadata (ids, counts, maintenance timings).
    """
    # 1. Class hierarchy: Building becomes the parent of Roof/Wall/Ground.
    # The generator created all four as roots, and create_semantic_class
    # refuses to change an existing parent, so this writes exactly the rows
    # create_semantic_class would have written had the parentage been
    # declared at creation (self-rows already exist).
    with pkg.transaction():
        for child_class_id in (
            result.roof_class_id,
            result.wall_class_id,
            result.ground_class_id,
        ):
            pkg.conn.execute(
                """
                UPDATE usap_semantic_class
                SET parent_class_id = ?
                WHERE semantic_class_id = ?
                """,
                (result.building_class_id, child_class_id),
            )

            pkg.conn.execute(
                """
                INSERT OR IGNORE INTO usap_semantic_class_closure (
                    ancestor_class_id,
                    descendant_class_id,
                    depth
                )
                VALUES (?, ?, 1)
                """,
                (result.building_class_id, child_class_id),
            )

    # 2. Object tree depth 3: city_root -> districts -> buildings (-> surfaces).
    district_count = max(1, math.isqrt(config.building_count))

    with pkg.transaction():
        city_model_class_id = pkg.create_semantic_class(
            scheme="usap-local",
            class_uri="usap-local:city:CityModel",
            local_name="CityModel",
        )

        district_class_id = pkg.create_semantic_class(
            scheme="usap-local",
            class_uri="usap-local:city:District",
            local_name="District",
        )

        root_id = pkg.create_city_object(
            object_uid="city_root",
            semantic_class_id=city_model_class_id,
        )

        district_ids: list[int] = []

        for d in range(district_count):
            district_id = pkg.create_city_object(
                object_uid=f"district_{d:04d}",
                semantic_class_id=district_class_id,
            )
            district_ids.append(district_id)

            pkg.link_city_objects(
                root_id,
                district_id,
                "boundary",
                code_space=CITYGML_3_0_CORE_NS,
                category="containment",
                graph_name=DEFAULT_GRAPH_NAME,
            )

        buildings = pkg.conn.execute(
            """
            SELECT city_object_id
            FROM usap_city_object
            WHERE semantic_class_id = ?
            ORDER BY object_uid
            """,
            (result.building_class_id,),
        ).fetchall()

        for i, row in enumerate(buildings):
            pkg.link_city_objects(
                district_ids[i % district_count],
                int(row["city_object_id"]),
                "boundary",
                code_space=CITYGML_3_0_CORE_NS,
                category="containment",
                graph_name=DEFAULT_GRAPH_NAME,
            )

    # 3. Two whole-part value fields (f4): a gradient the min/max pruning
    # can exploit, and uniform noise it cannot.
    total = result.total_face_count
    gradient = np.linspace(0.0, 1.0, total, dtype=np.float32)
    rng = np.random.default_rng(42)
    noise = rng.random(total, dtype=np.float32)

    with pkg.transaction():
        pkg.create_semantic_class(
            scheme="usap-local",
            class_uri="usap-local:field:ShadowFraction",
            local_name="ShadowFraction",
        )

        pkg.create_semantic_class(
            scheme="usap-local",
            class_uri="usap-local:field:NoiseLevel",
            local_name="NoiseLevel",
        )

        gradient_annotation = pkg.annotate_value_field(
            concept="ShadowFraction",
            asset_part_id=result.asset_part_id,
            element_kind="face",
            values=gradient,
            status="accepted",
            label="gradient value field (pruning-friendly)",
        )

        noise_annotation = pkg.annotate_value_field(
            concept="NoiseLevel",
            asset_part_id=result.asset_part_id,
            element_kind="face",
            values=noise,
            status="accepted",
            label="uniform-noise value field (pruning-hostile)",
        )

    return {
        "district_count": district_count,
        "gradient_annotation_id": int(gradient_annotation["annotation_id"]),
        "noise_annotation_id": int(noise_annotation["annotation_id"]),
    }


# ---------------------------------------------------------------------
# Ablation runners
# ---------------------------------------------------------------------


def run_ablations(
    pkg: USAPPackage,
    result,
    setup: dict,
    repeat: int,
) -> list[AblationResult]:
    block_size = pkg.get_default_block_size()
    total_membership_blocks = count_rows(pkg, "usap_membership_block")

    ablations: list[AblationResult] = []

    # --- A1: reverse query without block_start pruning -----------------
    selections = {
        "one block": list(range(min(100, result.total_face_count))),
        "many blocks": make_spread_indices(
            result.total_face_count, block_size
        ),
    }

    for label, indices in selections.items():
        accel_timing, accel_result = time_operation(
            f"A1 accel ({label})",
            repeat,
            lambda indices=indices: pkg.annotations_for_elements(
                asset_part_id=result.asset_part_id,
                element_kind=ELEMENT_KIND_FACE,
                selected_indices=indices,
            ),
        )

        naive_timing, naive_result = time_operation(
            f"A1 naive ({label})",
            repeat,
            lambda indices=indices: naive_annotations_for_elements(
                pkg,
                result.asset_part_id,
                ELEMENT_KIND_FACE,
                indices,
            ),
        )

        require_equal(
            f"A1 ({label})",
            canon_reverse_result(accel_result),
            canon_reverse_result(naive_result),
        )

        ablations.append(
            AblationResult(
                name=(
                    f"A1 block pruning — {len(indices)} faces, {label} "
                    f"({distinct_block_starts(indices, block_size)} block-starts)"
                ),
                accelerated=accel_timing,
                naive=naive_timing,
                detail=(
                    f"naive decodes all {total_membership_blocks} blocks; "
                    f"{len(accel_result)} annotations matched"
                ),
            )
        )

    # --- A2: class subtree without usap_semantic_class_closure ---------
    accel_timing, accel_result = time_operation(
        "A2 accel",
        repeat,
        lambda: pkg.elements_for_semantic_class(
            semantic_class_id=result.building_class_id,
            include_subclasses=True,
            expand=False,
        ),
    )

    naive_timing, naive_result = time_operation(
        "A2 naive",
        repeat,
        lambda: naive_class_descendant_blocks(pkg, result.building_class_id),
    )

    require_equal(
        "A2",
        canon_block_dicts(accel_result),
        canon_block_dicts(naive_result),
    )

    ablations.append(
        AblationResult(
            name="A2 semantic-class closure vs recursive CTE (Building subtree)",
            accelerated=accel_timing,
            naive=naive_timing,
            detail=f"{len(accel_result)} membership blocks",
        )
    )

    # --- A4: elements_where without value_min/value_max skipping -------
    threshold = 0.9

    fields = {
        "gradient": setup["gradient_annotation_id"],
        "uniform noise": setup["noise_annotation_id"],
    }

    for label, annotation_id in fields.items():
        skippable = pkg.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(value_max <= ?) AS skippable
            FROM usap_value_block
            WHERE annotation_id = ?
            """,
            (threshold, annotation_id),
        ).fetchone()

        accel_timing, accel_result = time_operation(
            f"A4 accel ({label})",
            repeat,
            lambda annotation_id=annotation_id: pkg.elements_where(
                annotation_id, (">", threshold)
            ),
        )

        naive_timing, naive_result = time_operation(
            f"A4 naive ({label})",
            repeat,
            lambda annotation_id=annotation_id: naive_elements_where_gt(
                pkg, annotation_id, threshold
            ),
        )

        require_equal(f"A4 ({label})", accel_result, naive_result)

        ablations.append(
            AblationResult(
                name=f"A4 value min/max skipping — {label} field, > {threshold}",
                accelerated=accel_timing,
                naive=naive_timing,
                detail=(
                    f"skips {int(skippable['skippable'] or 0)}/"
                    f"{int(skippable['total'])} blocks; "
                    f"{len(accel_result)} hits"
                ),
            )
        )

    # --- A5: value_field_stats without stored aggregates ---------------
    annotation_id = setup["gradient_annotation_id"]

    accel_timing, accel_result = time_operation(
        "A5 accel",
        repeat,
        lambda: pkg.value_field_stats(annotation_id),
    )

    naive_timing, naive_result = time_operation(
        "A5 naive",
        repeat,
        lambda: naive_value_field_stats(pkg, annotation_id),
    )

    stats_equal = (
        accel_result["count"] == naive_result["count"]
        and math.isclose(
            accel_result["min"], naive_result["min"], rel_tol=1e-6
        )
        and math.isclose(
            accel_result["max"], naive_result["max"], rel_tol=1e-6
        )
    )

    if not stats_equal:
        raise RuntimeError(
            f"A5: naive stats differ from accelerated stats — "
            f"accelerated {accel_result}, naive {naive_result}."
        )

    ablations.append(
        AblationResult(
            name="A5 value_field_stats: stored min/max vs full decode",
            accelerated=accel_timing,
            naive=naive_timing,
            detail=f"{accel_result['count']} values, no payload decode vs full",
        )
    )

    return ablations


# ---------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------


def print_ablation_table(ablations: list[AblationResult]) -> None:
    print()
    print("| Ablation | Accel mean ms | Naive mean ms | Speedup | Detail |")
    print("|---|---:|---:|---:|---|")

    for item in ablations:
        print(
            f"| {item.name} "
            f"| {item.accelerated.mean_ms:.3f} "
            f"| {item.naive.mean_ms:.3f} "
            f"| {item.speedup:.1f}x "
            f"| {item.detail} |"
        )

    print()


def print_cost_table(costs: dict) -> None:
    print("| Accelerator cost | Value |")
    print("|---|---|")

    for label, value in costs.items():
        print(f"| {label} | {value} |")

    print()


def ablation_to_dict(item: AblationResult) -> dict:
    return {
        "name": item.name,
        "repeat": item.accelerated.repeat,
        "accelerated_mean_ms": item.accelerated.mean_ms,
        "accelerated_min_ms": item.accelerated.min_ms,
        "naive_mean_ms": item.naive.mean_ms,
        "naive_min_ms": item.naive.min_ms,
        "speedup": item.speedup,
        "results_equal": True,  # a mismatch aborts the run before reporting
        "detail": item.detail,
    }


def write_json_report(path: str | Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


def write_markdown_report(path: str | Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# USAP accelerator ablation report",
        "",
        f"Generated UTC: `{report['created_at_utc']}`",
        "",
        "Every naive variant returned results identical to the accelerated",
        "query (asserted at runtime), so all queries are answerable from the",
        "base tables alone. The tables below quantify what each accelerator",
        "buys and costs.",
        "",
        "## Package",
        "",
    ]

    for label, value in report["package"].items():
        lines.append(f"- {label}: `{value}`")

    lines += [
        "",
        "## Ablations",
        "",
        "| Ablation | Accel mean ms | Naive mean ms | Speedup | Detail |",
        "|---|---:|---:|---:|---|",
    ]

    for item in report["ablations"]:
        lines.append(
            f"| {item['name']} "
            f"| {item['accelerated_mean_ms']:.3f} "
            f"| {item['naive_mean_ms']:.3f} "
            f"| {item['speedup']:.1f}x "
            f"| {item['detail']} |"
        )

    lines += ["", "## Accelerator costs", "", "| Cost | Value |", "|---|---|"]

    for label, value in report["costs"].items():
        lines.append(f"| {label} | {value} |")

    lines += [
        "",
        "## Notes",
        "",
        "- `usap_membership_block.min_element_index` / `max_element_index` "
        "are populated on write but never used for query-time pruning "
        "(`annotations_for_elements` prunes by `block_start` only): a stored "
        "accelerator with no current query benefit.",
        "",
        f"- Python: `{platform.python_implementation()} "
        f"{sys.version.split()[0]}` on `{platform.platform()}`",
        "",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark USAP queries with vs without their accelerator "
            "structures (closures, block pruning, stored min/max)."
        )
    )

    parser.add_argument(
        "--db",
        default="benchmark_ablation.usap.gpkg",
        help="Output benchmark USAP database path.",
    )

    parser.add_argument(
        "--schema",
        default=str(DEFAULT_SCHEMA_PATH),
        help="Path to USAP schema.sql (defaults to the packaged one).",
    )

    parser.add_argument(
        "--buildings",
        type=int,
        default=500,
        help="Number of synthetic buildings to generate.",
    )

    parser.add_argument(
        "--roof-faces",
        type=int,
        default=120,
        help="Roof faces per synthetic building.",
    )

    parser.add_argument(
        "--wall-faces",
        type=int,
        default=300,
        help="Wall faces per synthetic building.",
    )

    parser.add_argument(
        "--ground-faces",
        type=int,
        default=80,
        help="Ground faces per synthetic building.",
    )

    parser.add_argument(
        "--repeat",
        type=int,
        default=5,
        help="Number of times to repeat each measured query.",
    )

    parser.add_argument(
        "--json",
        default=None,
        help="Optional path where results will be written as JSON.",
    )

    parser.add_argument(
        "--md",
        default=None,
        help="Optional path where results will be written as Markdown.",
    )

    args = parser.parse_args()

    db_path = Path(args.db)

    config = SyntheticConfig(
        building_count=args.buildings,
        roof_faces_per_building=args.roof_faces,
        wall_faces_per_building=args.wall_faces,
        ground_faces_per_building=args.ground_faces,
    )

    print("Creating synthetic package for ablation...")
    print("Database:", db_path)
    print("Buildings:", config.building_count)
    print()

    build_start = time.perf_counter()

    result = create_synthetic_package(
        db_path=db_path,
        schema_path=args.schema,
        config=config,
        overwrite=True,
    )

    build_seconds = time.perf_counter() - build_start

    with USAPPackage.open(db_path) as pkg:
        setup = deepen_package(pkg, result, config)

        db_size = os.path.getsize(db_path)
        relationship_count = count_rows(pkg, "usap_city_object_relationship")
        class_count = count_rows(pkg, "usap_semantic_class")
        class_closure_count = count_rows(pkg, "usap_semantic_class_closure")
        membership_block_count = count_rows(pkg, "usap_membership_block")
        value_block_count = count_rows(pkg, "usap_value_block")

        print(f"Build time: {build_seconds:.3f} s")
        print(f"Total faces: {result.total_face_count}")
        print(f"Districts added: {setup['district_count']} (tree depth 3)")
        print(f"Membership blocks: {membership_block_count}")
        print(f"Value blocks: {value_block_count} (2 fields)")
        print(f"Database size: {format_bytes(db_size)}")
        print()

        ablations = run_ablations(pkg, result, setup, args.repeat)

        print("All naive results identical to accelerated results.")
        print_ablation_table(ablations)

        costs = {
            "usap_city_object_relationship rows": f"{relationship_count}",
            "usap_semantic_class_closure rows": (
                f"{class_closure_count} (vs {class_count} classes)"
            ),
            "database size": format_bytes(db_size),
        }

        print_cost_table(costs)

        print(
            "Note: usap_membership_block.min/max_element_index are stored "
            "on every block but never used for query-time pruning — a "
            "write-side accelerator no query currently reads."
        )

        report = {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "package": {
                "database": str(db_path),
                "buildings": config.building_count,
                "total_faces": result.total_face_count,
                "membership_blocks": membership_block_count,
                "value_blocks": value_block_count,
                "build_seconds": round(build_seconds, 3),
                "size": format_bytes(db_size),
            },
            "ablations": [ablation_to_dict(item) for item in ablations],
            "costs": costs,
        }

        if args.json:
            write_json_report(args.json, report)
            print("Wrote JSON report:", args.json)

        if args.md:
            write_markdown_report(args.md, report)
            print("Wrote Markdown report:", args.md)


if __name__ == "__main__":
    main()

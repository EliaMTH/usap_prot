from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
import json
import platform
import sys
from datetime import datetime, timezone

from usap import (
    ELEMENT_KIND_FACE,
    SyntheticConfig,
    USAPPackage,
    create_synthetic_package,
)


@dataclass(frozen=True)
class TimingResult:
    name: str
    repeat: int
    mean_ms: float
    min_ms: float
    max_ms: float
    summary: str


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"

    if size < 1024 * 1024:
        return f"{size / 1024:.2f} KiB"

    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MiB"

    return f"{size / (1024 * 1024 * 1024):.2f} GiB"


def time_operation(name: str, repeat: int, func) -> TimingResult:
    durations: list[float] = []
    last_result = None

    for _ in range(repeat):
        start = time.perf_counter()
        last_result = func()
        end = time.perf_counter()

        durations.append((end - start) * 1000.0)

    summary = summarize_result(last_result)

    return TimingResult(
        name=name,
        repeat=repeat,
        mean_ms=mean(durations),
        min_ms=min(durations),
        max_ms=max(durations),
        summary=summary,
    )


def summarize_result(result) -> str:
    if result is None:
        return "None"

    if isinstance(result, list):
        return f"{len(result)} rows/items"

    if isinstance(result, dict):
        return f"{len(result)} keys"

    return str(result)


def count_rows(pkg: USAPPackage, table_name: str) -> int:
    row = pkg.conn.execute(
        f"SELECT COUNT(*) AS n FROM {table_name}"
    ).fetchone()

    return int(row["n"])


def distinct_block_starts(indices: list[int], block_size: int) -> int:
    return len({(index // block_size) * block_size for index in indices})


def make_q2_indices(
    total_face_count: int,
    block_size: int,
    requested_blocks: int = 20,
    faces_per_block: int = 50,
) -> list[int]:
    """
    Build a selection spread across several membership blocks.

    The ideal Q2 is:
        1000 selected faces across 20 blocks.

    If the synthetic dataset is too small, this gracefully uses fewer blocks.
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


def print_result_table(results: list[TimingResult]) -> None:
    print()
    print("| Query | Repeat | Mean ms | Min ms | Max ms | Result |")
    print("|---|---:|---:|---:|---:|---|")

    for item in results:
        print(
            f"| {item.name} "
            f"| {item.repeat} "
            f"| {item.mean_ms:.3f} "
            f"| {item.min_ms:.3f} "
            f"| {item.max_ms:.3f} "
            f"| {item.summary} |"
        )

    print()

def timing_to_dict(item: TimingResult) -> dict:
    return {
        "name": item.name,
        "repeat": item.repeat,
        "mean_ms": item.mean_ms,
        "min_ms": item.min_ms,
        "max_ms": item.max_ms,
        "summary": item.summary,
    }


def build_benchmark_report(
    *,
    db_path: Path,
    db_size: int,
    build_seconds: float,
    result,
    config: SyntheticConfig,
    block_size: int,
    annotation_count: int,
    city_object_count: int,
    relationship_count: int,
    closure_count: int,
    membership_block_count: int,
    q1_indices: list[int],
    q2_indices: list[int],
    timings: list[TimingResult],
    validation_problems: list[str],
) -> dict:
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": {
            "version": sys.version,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "database": {
            "path": str(db_path),
            "size_bytes": db_size,
            "size_human": format_bytes(db_size),
        },
        "synthetic_config": {
            "building_count": config.building_count,
            "roof_faces_per_building": config.roof_faces_per_building,
            "wall_faces_per_building": config.wall_faces_per_building,
            "ground_faces_per_building": config.ground_faces_per_building,
            "faces_per_building": (
                config.roof_faces_per_building
                + config.wall_faces_per_building
                + config.ground_faces_per_building
            ),
        },
        "package_counts": {
            "total_faces": result.total_face_count,
            "annotations": annotation_count,
            "city_objects": city_object_count,
            "relationships": relationship_count,
            "closure_rows": closure_count,
            "membership_blocks": membership_block_count,
            "default_block_size": block_size,
        },
        "build": {
            "seconds": build_seconds,
        },
        "queries": {
            "q1_selected_face_count": len(q1_indices),
            "q1_touched_blocks": distinct_block_starts(q1_indices, block_size),
            "q2_selected_face_count": len(q2_indices),
            "q2_touched_blocks": distinct_block_starts(q2_indices, block_size),
            "timings": [timing_to_dict(item) for item in timings],
        },
        "validation": {
            "ok": not validation_problems,
            "problems": validation_problems,
        },
    }


def write_json_report(path: str | Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


def write_markdown_report(path: str | Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    config = report["synthetic_config"]
    counts = report["package_counts"]
    database = report["database"]
    build = report["build"]
    queries = report["queries"]
    validation = report["validation"]

    lines = []

    lines.append("# USAP Phase 1 Benchmark Report")
    lines.append("")
    lines.append(f"Generated UTC: `{report['created_at_utc']}`")
    lines.append("")
    lines.append("## Synthetic dataset")
    lines.append("")
    lines.append(f"- Buildings: `{config['building_count']}`")
    lines.append(f"- Faces per building: `{config['faces_per_building']}`")
    lines.append(f"- Roof faces per building: `{config['roof_faces_per_building']}`")
    lines.append(f"- Wall faces per building: `{config['wall_faces_per_building']}`")
    lines.append(f"- Ground faces per building: `{config['ground_faces_per_building']}`")
    lines.append(f"- Total faces: `{counts['total_faces']}`")
    lines.append("")
    lines.append("## Package")
    lines.append("")
    lines.append(f"- Database: `{database['path']}`")
    lines.append(f"- Size: `{database['size_human']}`")
    lines.append(f"- Build time: `{build['seconds']:.3f} s`")
    lines.append(f"- Annotations: `{counts['annotations']}`")
    lines.append(f"- City objects: `{counts['city_objects']}`")
    lines.append(f"- Relationships: `{counts['relationships']}`")
    lines.append(f"- Closure rows: `{counts['closure_rows']}`")
    lines.append(f"- Membership blocks: `{counts['membership_blocks']}`")
    lines.append(f"- Default block size: `{counts['default_block_size']}`")
    lines.append("")
    lines.append("## Query setup")
    lines.append("")
    lines.append(
        f"- Q1 selected faces: `{queries['q1_selected_face_count']}` "
        f"across `{queries['q1_touched_blocks']}` block(s)"
    )
    lines.append(
        f"- Q2 selected faces: `{queries['q2_selected_face_count']}` "
        f"across `{queries['q2_touched_blocks']}` block(s)"
    )
    lines.append("")
    lines.append("## Timings")
    lines.append("")
    lines.append("| Query | Repeat | Mean ms | Min ms | Max ms | Result |")
    lines.append("|---|---:|---:|---:|---:|---|")

    for item in queries["timings"]:
        lines.append(
            f"| {item['name']} "
            f"| {item['repeat']} "
            f"| {item['mean_ms']:.3f} "
            f"| {item['min_ms']:.3f} "
            f"| {item['max_ms']:.3f} "
            f"| {item['summary']} |"
        )

    lines.append("")
    lines.append("## Validation")
    lines.append("")

    if validation["ok"]:
        lines.append("Validation OK.")
    else:
        lines.append("Validation problems:")
        lines.append("")
        for problem in validation["problems"]:
            lines.append(f"- {problem}")

    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append(f"- Python implementation: `{report['python']['implementation']}`")
    lines.append(f"- Python version: `{report['python']['version'].split()[0]}`")
    lines.append(f"- Platform: `{report['python']['platform']}`")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run phase-1 USAP synthetic benchmarks."
    )

    parser.add_argument(
        "--db",
        default="benchmark_phase1.usap.gpkg",
        help="Output benchmark USAP database path.",
    )

    parser.add_argument(
        "--schema",
        default="sql/schema.sql",
        help="Path to USAP schema.sql.",
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
        help="Optional path where benchmark results will be written as JSON.",
    )

    parser.add_argument(
        "--md",
        default=None,
        help="Optional path where benchmark results will be written as Markdown.",
    )

    args = parser.parse_args()

    db_path = Path(args.db)

    config = SyntheticConfig(
        building_count=args.buildings,
        roof_faces_per_building=args.roof_faces,
        wall_faces_per_building=args.wall_faces,
        ground_faces_per_building=args.ground_faces,
    )

    print("Creating synthetic benchmark package...")
    print("Database:", db_path)
    print("Buildings:", config.building_count)
    print(
        "Faces per building:",
        config.roof_faces_per_building
        + config.wall_faces_per_building
        + config.ground_faces_per_building,
    )
    print()

    build_start = time.perf_counter()

    result = create_synthetic_package(
        db_path=db_path,
        schema_path=args.schema,
        config=config,
        overwrite=True,
    )

    build_end = time.perf_counter()

    db_size = os.path.getsize(db_path)

    with USAPPackage.open(db_path) as pkg:
        block_size = pkg.get_default_block_size()

        annotation_count = count_rows(pkg, "usap_annotation")
        city_object_count = count_rows(pkg, "usap_city_object")
        relationship_count = count_rows(pkg, "usap_city_object_relationship")
        closure_count = count_rows(pkg, "usap_city_object_closure")
        membership_block_count = count_rows(pkg, "usap_membership_block")

        print("Synthetic package created.")
        print(f"Build time: {build_end - build_start:.3f} s")
        print(f"Database size: {format_bytes(db_size)}")
        print(f"Total faces: {result.total_face_count}")
        print(f"Annotations: {annotation_count}")
        print(f"City objects: {city_object_count}")
        print(f"Relationships: {relationship_count}")
        print(f"Closure rows: {closure_count}")
        print(f"Membership blocks: {membership_block_count}")
        print(f"Default block size: {block_size}")
        print()

        # ------------------------------------------------------------
        # Q1: 100 selected faces in one block -> annotations
        # ------------------------------------------------------------
        q1_count = min(100, config.roof_faces_per_building)
        q1_indices = list(range(q1_count))

        print(
            "Q1 selected faces:",
            len(q1_indices),
            "faces across",
            distinct_block_starts(q1_indices, block_size),
            "block(s)",
        )

        # ------------------------------------------------------------
        # Q2: 1000 selected faces across 20 blocks -> annotations
        # ------------------------------------------------------------
        q2_indices = make_q2_indices(
            total_face_count=result.total_face_count,
            block_size=block_size,
            requested_blocks=20,
            faces_per_block=50,
        )

        print(
            "Q2 selected faces:",
            len(q2_indices),
            "faces across",
            distinct_block_starts(q2_indices, block_size),
            "block(s)",
        )

        # ------------------------------------------------------------
        # Q5 setup: replace one annotation with 5000 faces
        # ------------------------------------------------------------
        replace_row = pkg.conn.execute(
            """
            SELECT annotation_id
            FROM usap_annotation
            WHERE annotation_uid = ?
            """,
            ("ann_building_000000_roof_mesh",),
        ).fetchone()

        if replace_row is None:
            raise RuntimeError("Could not find benchmark replacement annotation.")

        replace_annotation_id = int(replace_row["annotation_id"])

        replacement_faces = list(
            range(min(5000, result.total_face_count))
        )

        timings = [
            time_operation(
                "Q1 selected faces in one block -> annotations",
                args.repeat,
                lambda: pkg.annotations_for_elements(
                    asset_part_id=result.asset_part_id,
                    element_kind=ELEMENT_KIND_FACE,
                    selected_indices=q1_indices,
                ),
            ),
            time_operation(
                "Q2 selected faces across many blocks -> annotations",
                args.repeat,
                lambda: pkg.annotations_for_elements(
                    asset_part_id=result.asset_part_id,
                    element_kind=ELEMENT_KIND_FACE,
                    selected_indices=q2_indices,
                ),
            ),
            time_operation(
                "Q3 all RoofSurface blocks",
                args.repeat,
                lambda: pkg.elements_for_semantic_class(
                    semantic_class_id=result.roof_class_id,
                    include_subclasses=True,
                    expand=False,
                ),
            ),
            time_operation(
                "Q4 building_000000 descendants -> membership blocks",
                args.repeat,
                lambda: pkg.elements_for_city_object(
                    object_uid="building_000000",
                    include_descendants=True,
                    graph_name="usap_default",
                    expand=False,
                ),
            ),
            time_operation(
                "Q5 replace one annotation with up to 5000 faces",
                args.repeat,
                lambda: pkg.replace_annotation_membership(
                    annotation_id=replace_annotation_id,
                    asset_part_id=result.asset_part_id,
                    element_kind=ELEMENT_KIND_FACE,
                    element_indices=replacement_faces,
                ),
            ),
        ]

        print_result_table(timings)

        report = pkg.validate_report()
        problems = [issue.format() for issue in report.issues]

        if problems:
            print("Validation problems:")
            for problem in problems:
                print("-", problem)
        else:
            print("Validation: OK")

        report = build_benchmark_report(
            db_path=db_path,
            db_size=db_size,
            build_seconds=build_end - build_start,
            result=result,
            config=config,
            block_size=block_size,
            annotation_count=annotation_count,
            city_object_count=city_object_count,
            relationship_count=relationship_count,
            closure_count=closure_count,
            membership_block_count=membership_block_count,
            q1_indices=q1_indices,
            q2_indices=q2_indices,
            timings=timings,
            validation_problems=problems,
        )

        if args.json:
            write_json_report(args.json, report)
            print("Wrote JSON report:", args.json)

        if args.md:
            write_markdown_report(args.md, report)
            print("Wrote Markdown report:", args.md)


if __name__ == "__main__":
    main()
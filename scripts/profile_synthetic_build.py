from __future__ import annotations

import argparse
import cProfile
import pstats
from pathlib import Path

from usap import DEFAULT_SCHEMA_PATH, SyntheticConfig, create_synthetic_package


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile synthetic USAP package generation."
    )

    parser.add_argument("--db", default="profile_synthetic.usap.gpkg")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA_PATH))
    parser.add_argument("--buildings", type=int, default=1000)
    parser.add_argument("--output", default="profile_synthetic_build.prof")

    args = parser.parse_args()

    config = SyntheticConfig(
        building_count=args.buildings,
        roof_faces_per_building=120,
        wall_faces_per_building=300,
        ground_faces_per_building=80,
    )

    profiler = cProfile.Profile()

    profiler.enable()

    create_synthetic_package(
        db_path=Path(args.db),
        schema_path=args.schema,
        config=config,
        overwrite=True,
    )

    profiler.disable()

    profiler.dump_stats(args.output)

    stats = pstats.Stats(args.output)
    stats.strip_dirs()
    stats.sort_stats("cumtime")
    stats.print_stats(40)

    print()
    print("Profile saved to:", args.output)


if __name__ == "__main__":
    main()
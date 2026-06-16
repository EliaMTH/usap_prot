from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .._util import sha256_file
from ..constants import ELEMENT_KIND_POINT
from ..core import USAPPackage


@dataclass(frozen=True)
class LASRegistrationResult:
    asset_id: int
    asset_part_id: int
    path: Path
    point_count: int
    minx: float
    miny: float
    minz: float
    maxx: float
    maxy: float
    maxz: float
    crs_wkt: str | None


def _guess_las_media_type(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".las":
        return "application/vnd.las"

    if suffix == ".laz":
        return "application/vnd.laszip"

    return "application/octet-stream"


def _try_read_crs_wkt(header) -> str | None:
    try:
        crs = header.parse_crs()
    except Exception:
        return None

    if crs is None:
        return None

    try:
        return crs.to_wkt()
    except Exception:
        return str(crs)


def register_las_asset(
    pkg: USAPPackage,
    las_path: str | Path,
    *,
    uri: str | None = None,
    compute_hash: bool = True,
    part_path: str = "points/all",
) -> LASRegistrationResult:
    """
    Register a LAS/LAZ file as a USAP point-cloud asset.

    Prototype convention:
        one LAS file = one asset
        all points = one asset part
        point indices are zero-based LAS point order

    This does not copy points into USAP.
    It only records the external file and the point-index coordinate system.
    """
    import laspy

    path = Path(las_path)

    if not path.exists():
        raise FileNotFoundError(f"LAS/LAZ file not found: {path}")

    with laspy.open(path) as reader:
        header = reader.header

        point_count = int(header.point_count)

        minx = float(header.mins[0])
        miny = float(header.mins[1])
        minz = float(header.mins[2])

        maxx = float(header.maxs[0])
        maxy = float(header.maxs[1])
        maxz = float(header.maxs[2])

        crs_wkt = _try_read_crs_wkt(header)

    content_hash = sha256_file(path) if compute_hash else None

    asset_metadata = {
        "adapter": "las_adapter",
        "format": path.suffix.lower().lstrip("."),
        "point_count": point_count,
        "crs_wkt": crs_wkt,
    }

    part_metadata = {
        "adapter": "las_adapter",
        "indexing": "zero_based_file_point_order",
        "point_count": point_count,
        "note": (
            "Prototype convention: point index means the zero-based point "
            "position in the LAS/LAZ file order."
        ),
    }

    with pkg.transaction():
        asset_id = pkg.register_asset(
            uri=uri if uri is not None else str(path),
            asset_kind="pointcloud",
            media_type=_guess_las_media_type(path),
            content_hash=content_hash,
            metadata_json=json.dumps(asset_metadata),
        )

        asset_part_id = pkg.register_asset_part(
            asset_id=asset_id,
            part_path=part_path,
            element_kind=ELEMENT_KIND_POINT,
            element_count=point_count,
            index_origin="zero_based",
            minx=minx,
            miny=miny,
            minz=minz,
            maxx=maxx,
            maxy=maxy,
            maxz=maxz,
            metadata_json=json.dumps(part_metadata),
        )

    return LASRegistrationResult(
        asset_id=asset_id,
        asset_part_id=asset_part_id,
        path=path,
        point_count=point_count,
        minx=minx,
        miny=miny,
        minz=minz,
        maxx=maxx,
        maxy=maxy,
        maxz=maxz,
        crs_wkt=crs_wkt,
    )
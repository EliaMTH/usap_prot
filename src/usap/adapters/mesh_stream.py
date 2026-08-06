"""
Header/streaming readers for large meshes.

Registration needs exactly two things from a mesh file: how many faces it has
(the index space annotations are written against) and its bounding box (the
derived 2D extent for the GIS layer). ``trimesh.load`` materializes the whole
mesh to produce them, which for a 10 GB city mesh means 10-20 GB of RAM for
two numbers per part.

These readers stream instead: a PLY face count comes straight out of the
header, and bounds come from one pass over the vertex block. Nothing here
builds a face array.

Scope is deliberately narrow — PLY and OBJ, one part per file. trimesh splits
a grouped OBJ into one geometry per ``o``/``g`` group, and ``part_path`` is
what annotations bind to, so a file that would decompose differently is
refused rather than silently registered under a different part identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..errors import USAPError


# Vertices are read in chunks of this many so a 10 GB file never lands in
# memory at once.
_VERTEX_CHUNK = 1_000_000

_PLY_SCALAR_DTYPES = {
    "char": "i1", "int8": "i1",
    "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2",
    "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4",
    "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4",
    "double": "f8", "float64": "f8",
}


@dataclass(frozen=True)
class StreamedMeshPart:
    """What registration needs from a mesh file, without loading it."""

    name: str
    face_count: int
    minx: float | None
    miny: float | None
    minz: float | None
    maxx: float | None
    maxy: float | None
    maxz: float | None


def _bounds_from_chunks(chunks) -> tuple[float | None, ...]:
    minimum: np.ndarray | None = None
    maximum: np.ndarray | None = None

    for chunk in chunks:
        if chunk.size == 0:
            continue

        chunk_min = chunk.min(axis=0)
        chunk_max = chunk.max(axis=0)

        minimum = chunk_min if minimum is None else np.minimum(minimum, chunk_min)
        maximum = chunk_max if maximum is None else np.maximum(maximum, chunk_max)

    if minimum is None:
        return (None,) * 6

    return (
        float(minimum[0]), float(minimum[1]), float(minimum[2]),
        float(maximum[0]), float(maximum[1]), float(maximum[2]),
    )


def _read_ply_header(handle) -> dict:
    """
    Parse a PLY header into {format, elements: [(name, count, properties)]}.

    Only what the bounds pass needs: element order, counts, and each element's
    property types (to know how many bytes a vertex occupies).
    """
    if handle.readline().strip() != b"ply":
        raise USAPError("Not a PLY file (missing 'ply' magic).")

    ply_format: str | None = None
    elements: list[tuple[str, int, list[tuple[str, str]]]] = []

    while True:
        line = handle.readline()

        if not line:
            raise USAPError("Malformed PLY: header ended without 'end_header'.")

        parts = line.split()

        if not parts:
            continue

        keyword = parts[0]

        if keyword == b"end_header":
            break

        if keyword == b"format":
            ply_format = parts[1].decode()

        elif keyword == b"element":
            elements.append((parts[1].decode(), int(parts[2]), []))

        elif keyword == b"property":
            if not elements:
                raise USAPError("Malformed PLY: property before any element.")

            if parts[1] == b"list":
                elements[-1][2].append(("list", parts[2].decode()))
            else:
                elements[-1][2].append((parts[2].decode(), parts[1].decode()))

    if ply_format is None:
        raise USAPError("Malformed PLY: no format line.")

    return {"format": ply_format, "elements": elements}


def read_ply_part(path: Path) -> StreamedMeshPart:
    """
    Read face count and bounds from a PLY file without loading its faces.
    """
    with open(path, "rb") as handle:
        header = _read_ply_header(handle)
        elements = header["elements"]
        ply_format = header["format"]

        by_name = {name: (count, props) for name, count, props in elements}

        if "vertex" not in by_name or "face" not in by_name:
            raise USAPError(
                f"PLY file has no vertex/face elements: {path.name}. "
                "Register it with stream=False if it is small enough to load."
            )

        face_count = by_name["face"][0]
        vertex_count, vertex_props = by_name["vertex"]

        names = [name for name, _ in vertex_props]

        if names[:3] != ["x", "y", "z"]:
            raise USAPError(
                f"PLY vertices in {path.name} do not start with x/y/z "
                f"(found {names[:3]}); use stream=False."
            )

        if any(kind == "list" for kind, _ in vertex_props):
            raise USAPError(
                f"PLY vertex element in {path.name} has a list property, "
                "which has no fixed stride; use stream=False."
            )

        if ply_format == "ascii":
            bounds = _ply_ascii_bounds(handle, vertex_count)
        elif ply_format in ("binary_little_endian", "binary_big_endian"):
            byte_order = "<" if ply_format.endswith("little_endian") else ">"
            bounds = _ply_binary_bounds(
                handle, vertex_count, vertex_props, byte_order
            )
        else:
            raise USAPError(f"Unsupported PLY format: {ply_format!r}.")

    return StreamedMeshPart("default", face_count, *bounds)


def _ply_ascii_bounds(handle, vertex_count: int) -> tuple[float | None, ...]:
    def chunks():
        remaining = vertex_count

        while remaining > 0:
            take = min(remaining, _VERTEX_CHUNK)
            rows = []

            for _ in range(take):
                line = handle.readline()

                if not line:
                    raise USAPError("Malformed PLY: fewer vertices than declared.")

                rows.append([float(value) for value in line.split()[:3]])

            remaining -= take
            yield np.asarray(rows, dtype=np.float64)

    return _bounds_from_chunks(chunks())


def _ply_binary_bounds(
    handle,
    vertex_count: int,
    vertex_props: list[tuple[str, str]],
    byte_order: str,
) -> tuple[float | None, ...]:
    fields = []

    for index, (name, ply_type) in enumerate(vertex_props):
        scalar = _PLY_SCALAR_DTYPES.get(ply_type)

        if scalar is None:
            raise USAPError(f"Unsupported PLY property type: {ply_type!r}.")

        fields.append((name or f"field_{index}", byte_order + scalar))

    dtype = np.dtype(fields)

    def chunks():
        remaining = vertex_count

        while remaining > 0:
            take = min(remaining, _VERTEX_CHUNK)
            raw = handle.read(take * dtype.itemsize)

            if len(raw) < take * dtype.itemsize:
                raise USAPError("Malformed PLY: vertex block ends early.")

            records = np.frombuffer(raw, dtype=dtype, count=take)
            remaining -= take

            yield np.stack(
                [
                    records["x"].astype(np.float64),
                    records["y"].astype(np.float64),
                    records["z"].astype(np.float64),
                ],
                axis=1,
            )

    return _bounds_from_chunks(chunks())


def read_obj_part(path: Path) -> StreamedMeshPart:
    """
    Read face count and bounds from an OBJ file in one streaming pass.

    Refuses a file carrying more than one ``o``/``g`` group: trimesh would
    split it into several geometries, hence several asset parts with
    different part_paths, and annotations bind to those names.
    """
    minimum: np.ndarray | None = None
    maximum: np.ndarray | None = None
    face_count = 0
    group_names: set[str] = set()

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        buffer: list[list[float]] = []

        for line in handle:
            if line.startswith("v "):
                buffer.append([float(value) for value in line.split()[1:4]])

                if len(buffer) >= _VERTEX_CHUNK:
                    chunk = np.asarray(buffer, dtype=np.float64)
                    chunk_min, chunk_max = chunk.min(axis=0), chunk.max(axis=0)
                    minimum = (
                        chunk_min if minimum is None
                        else np.minimum(minimum, chunk_min)
                    )
                    maximum = (
                        chunk_max if maximum is None
                        else np.maximum(maximum, chunk_max)
                    )
                    buffer = []

            elif line.startswith("f "):
                face_count += 1

            elif line.startswith(("o ", "g ")):
                group_names.add(line[2:].strip())

    if buffer:
        chunk = np.asarray(buffer, dtype=np.float64)
        chunk_min, chunk_max = chunk.min(axis=0), chunk.max(axis=0)
        minimum = chunk_min if minimum is None else np.minimum(minimum, chunk_min)
        maximum = chunk_max if maximum is None else np.maximum(maximum, chunk_max)

    if len(group_names) > 1:
        raise USAPError(
            f"OBJ file {path.name} declares {len(group_names)} object/group "
            "names. Streaming registration treats a file as one part, while "
            "a normal load would split it into one part per group — and "
            "annotations are bound to part names. Split the file, or pass "
            "stream=False to load it in full."
        )

    bounds: tuple[float | None, ...]

    if minimum is None:
        bounds = (None,) * 6
    else:
        bounds = (
            float(minimum[0]), float(minimum[1]), float(minimum[2]),
            float(maximum[0]), float(maximum[1]), float(maximum[2]),
        )

    return StreamedMeshPart("default", face_count, *bounds)


def read_streamed_parts(path: Path) -> list[StreamedMeshPart]:
    """
    Stream the registration facts out of a mesh file, or raise if its format
    has no streaming reader.
    """
    suffix = path.suffix.lower()

    if suffix == ".ply":
        return [read_ply_part(path)]

    if suffix == ".obj":
        return [read_obj_part(path)]

    raise USAPError(
        f"No streaming reader for {suffix!r} meshes (only .ply and .obj). "
        "Pass stream=False to load the file in full."
    )

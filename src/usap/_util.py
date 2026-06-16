from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """
    Return the hex SHA-256 of a file, read in chunks.
    """
    digest = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def require_str(
    data: dict[str, Any],
    key: str,
    *,
    source: str | None = None,
) -> str:
    """
    Return data[key] when it is a non-empty string, otherwise raise ValueError.

    When `source` is given (e.g. a file path), it is included in the message.
    """
    value = data.get(key)

    if not isinstance(value, str) or not value:
        if source is not None:
            raise ValueError(f"{source}: missing required string field {key!r}")

        raise ValueError(f"Missing required string field: {key}")

    return value

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from .errors import USAPError

# Canonical stored form of a content hash: 'algorithm:digest'.
_CANONICAL_HASH_RE = re.compile(r"^([a-z0-9][a-z0-9+.-]*):([0-9a-f]+)$")

# A digest written before the canonical form existed, or by a hand-rolled
# writer: 64 hex characters is unambiguously SHA-256's output length.
_BARE_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def mint_package_iri() -> str:
    """
    Return a fresh stable identity for a package, as a UUID URN.

    A UUID is globally unique by construction, so this needs no domain,
    registry, or namespace to be valid — which is exactly why package identity
    can be settled now while the question of what namespace *concept* IRIs
    live under stays open.
    """
    return f"urn:uuid:{uuid.uuid4()}"


def canonical_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """
    Return the canonical stored content hash of a file: 'sha256:<hex>'.

    The algorithm is part of the stored value because usap_asset is unique on
    (uri, content_hash): a bare digest could not later be told apart from the
    same file hashed with a different algorithm, and changing the spelling
    afterwards would register one file as two assets.
    """
    return f"sha256:{sha256_file(path, chunk_size=chunk_size)}"


def parse_content_hash(value: str | None) -> tuple[str, str] | None:
    """
    Split a stored content hash into (algorithm, digest).

    Returns None when the value is absent or is not a recognizable digest —
    callers treat that as "no comparable hash" rather than an error, since
    content_hash is a free-text column that may hold a caller-supplied token.

    A bare 64-character hex string is read as SHA-256, so digests written
    before the canonical form still compare equal to freshly computed ones.
    """
    if value is None:
        return None

    value = value.strip()

    match = _CANONICAL_HASH_RE.match(value)

    if match is not None:
        return match.group(1), match.group(2)

    if _BARE_SHA256_RE.match(value):
        return "sha256", value.lower()

    return None


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


def _check_keys(block: dict[str, Any], known: frozenset[str], *, where: str) -> None:
    """
    Refuse keys this caller does not read.

    Values are already validated where they are used; what escaped was the key
    *name*. Anything starting with '_' is a comment and is skipped.

    Lives here rather than beside either caller: project_builder imports batch,
    so batch cannot import back from it.
    """
    unknown = sorted(
        key for key in block
        if not key.startswith("_") and key not in known
    )

    if unknown:
        raise USAPError(
            f"Unrecognised key(s) in {where}: {', '.join(repr(k) for k in unknown)}. "
            f"Known keys are: {', '.join(sorted(known))}. Nothing reads an "
            "unknown key, so leaving it would silently drop whatever it meant; "
            "prefix a key with '_' to keep it as a comment."
        )


def path_from_uri(uri: str) -> Path:
    """
    Where an asset uri points, understanding the file:// spelling.

    A stored uri is a path, not a URL -- but writers reach for file:// anyway,
    and Path("file://x.obj") is Path("file:/x.obj"): a *relative* path that
    resolves under the package directory and reports missing forever.
    Accepting the spelling costs five lines and repairs every package already
    written that way; refusing it silently does not.
    """
    # Case-insensitively, because register_asset's scheme guard is: a uri it
    # accepted as file:// must resolve as one here, or FILE://model.obj is
    # stored happily and then reports missing for the life of the package.
    if uri[:7].lower() != "file://":
        return Path(uri)

    parsed = urlparse(uri)

    # file://name.obj puts name.obj in netloc, because there is no third
    # slash -- which is exactly how a writer meaning "beside the package"
    # spells it. Only file:///abs and file://localhost/abs carry a real host.
    if parsed.netloc in ("", "localhost"):
        return Path(url2pathname(parsed.path))

    return Path(url2pathname(parsed.netloc + parsed.path))

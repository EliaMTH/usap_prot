from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import USAPPackage


@dataclass(frozen=True)
class VocabularyResult:
    by_name: dict[str, int]
    by_uri: dict[str, int]


DEFAULT_CITYGML_VOCABULARY_PATH = Path("vocabularies/citygml_3_0_mvp.json")
DEFAULT_ADE_VOCABULARY_PATH = Path("vocabularies/usap_ade_prototype.json")


def seed_vocabulary_file(
    pkg: USAPPackage,
    path: str | Path,
) -> VocabularyResult:
    """
    Seed semantic classes from an external vocabulary JSON file.

    The JSON file is the accepted concept registry for one scheme/version.
    Concepts are idempotently inserted using scheme + class_uri.
    """
    vocab_path = Path(path)

    if not vocab_path.exists():
        raise FileNotFoundError(f"Vocabulary file not found: {vocab_path}")

    data = json.loads(vocab_path.read_text(encoding="utf-8"))

    scheme = _required_str(data, "scheme", source=str(vocab_path))
    scheme_version = data.get("scheme_version")
    is_ade = bool(data.get("is_ade", False))

    concepts = data.get("concepts")

    if not isinstance(concepts, list):
        raise ValueError(
            f"Vocabulary {vocab_path} must contain a 'concepts' list."
        )

    by_name: dict[str, int] = {}
    by_uri: dict[str, int] = {}

    # First pass: create in the order given by the file.
    # Parent concepts should appear before children.
    for item in concepts:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid concept entry in {vocab_path}: {item!r}")

        local_name = _required_str(item, "local_name", source=str(vocab_path))
        class_uri = _required_str(item, "class_uri", source=str(vocab_path))

        parent_class_id = None
        parent_uri = item.get("parent_uri")
        parent_name = item.get("parent_name")

        if parent_uri is not None:
            parent_class_id = _resolve_optional_parent(
                pkg,
                parent_uri,
                scheme=scheme,
                vocab_path=vocab_path,
                child_uri=class_uri,
            )

        elif parent_name is not None:
            parent_class_id = _resolve_optional_parent(
                pkg,
                parent_name,
                scheme=scheme,
                vocab_path=vocab_path,
                child_uri=class_uri,
            )

        class_id = pkg.get_or_create_semantic_class(
            scheme=scheme,
            scheme_version=scheme_version,
            class_uri=class_uri,
            local_name=local_name,
            parent_class_id=parent_class_id,
            is_ade=is_ade,
        )

        by_name[local_name] = class_id
        by_uri[class_uri] = class_id

    return VocabularyResult(by_name=by_name, by_uri=by_uri)


def seed_default_citygml_vocabulary(pkg: USAPPackage) -> VocabularyResult:
    return seed_vocabulary_file(pkg, DEFAULT_CITYGML_VOCABULARY_PATH)


def seed_default_ade_vocabulary(pkg: USAPPackage) -> VocabularyResult:
    return seed_vocabulary_file(pkg, DEFAULT_ADE_VOCABULARY_PATH)


# Backward-compatible names used by earlier tests/examples.

def seed_citygml_basic_classes(pkg: USAPPackage) -> VocabularyResult:
    return seed_default_citygml_vocabulary(pkg)


def seed_prototype_ade_classes(pkg: USAPPackage) -> VocabularyResult:
    return seed_default_ade_vocabulary(pkg)


def _required_str(
    data: dict[str, Any],
    key: str,
    *,
    source: str,
) -> str:
    value = data.get(key)

    if not isinstance(value, str) or not value:
        raise ValueError(f"{source}: missing required string field {key!r}")

    return value


def _resolve_optional_parent(
    pkg: USAPPackage,
    parent_ref: str,
    *,
    scheme: str,
    vocab_path: Path,
    child_uri: str,
) -> int:
    try:
        # First try same-scheme resolution. This is useful for local names.
        return pkg.resolve_semantic_class(
            parent_ref,
            scheme=scheme,
        )
    except Exception:
        pass

    try:
        # Then try global resolution. This allows ADE/custom concepts
        # to inherit from CityGML class URIs.
        return pkg.resolve_semantic_class(parent_ref)
    except Exception as exc:
        raise ValueError(
            f"{vocab_path}: parent concept {parent_ref!r} for "
            f"{child_uri!r} is not registered yet. Put parent concepts "
            "before children, and load parent vocabularies before child "
            "vocabularies."
        ) from exc
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ._util import require_str
from .core import USAPPackage
from .errors import USAPAmbiguityError, USAPError


@dataclass(frozen=True)
class VocabularyResult:
    by_name: dict[str, int]
    by_uri: dict[str, int]


# Shipped inside the package (see pyproject package-data), not next to the
# repo checkout, so the default vocabularies load from a plain wheel install too.
_VOCABULARY_DIR = Path(__file__).resolve().parent / "data" / "vocabularies"

DEFAULT_CITYGML_VOCABULARY_PATH = _VOCABULARY_DIR / "citygml_3_0_mvp.json"
DEFAULT_ADE_VOCABULARY_PATH = _VOCABULARY_DIR / "usap_ade_prototype.json"


def seed_vocabulary_file(
    pkg: USAPPackage,
    path: str | Path,
) -> VocabularyResult:
    """
    Seed semantic classes from an external vocabulary JSON file.

    The JSON file is the accepted concept registry for one scheme/version.
    Concepts are idempotently inserted using class_uri (globally unique).

    This minimal format is intentionally a thin JSON form of a SKOS concept
    scheme; the fields map 1:1 to SKOS, so a vocabulary here can be generated
    from / exported to standard SKOS:
        class_uri   -> the concept IRI (a skos:Concept)
        local_name  -> skos:prefLabel
        parent_uri  -> skos:broader            (omit for top concepts)
        scheme / scheme_version -> the skos:ConceptScheme
        is_ade      -> marks an ADE/extension scheme vs a base one
    Richer per-concept metadata (definitions, units, applicable features, ...)
    is deliberately out of scope here: that is the application schema
    (e.g. the CityGML ADE XSD / SHACL), not the concept scheme.

    Minimal / no-ontology vocabularies: class_uri may be omitted, in which
    case it is derived as "{scheme}:{local_name}". parent_uri accepts either
    a class_uri or the local name of an already-registered concept (resolved
    within the same scheme first).

    Provenance (both optional, both nullable, neither populated by the shipped
    registries yet):
        source_namespace  the authority's namespace this concept comes from.
                          For a CityGML-derived concept, the XML namespace URI
                          — with local_name that is the QName the .gml uses.
                          May be given once at the top level as the default for
                          every concept in the file, and overridden per concept.
        concept_iri       the authority's own IRI for the concept, per concept,
                          when it publishes one.
    Recording these is what keeps a stable IRI *derivable* later without
    re-deciding anything, which is why the format carries them before any
    decision about IRIs has been made.

    Re-running this function on an updated file is additive and idempotent, and
    also **enriching**: a field that is still NULL on an existing concept is
    filled in, so provenance added to a registry later reaches packages that
    already exist by re-seeding rather than rebuilding. A field that already
    holds a *different* value raises instead of being silently rewritten.
    """
    vocab_path = Path(path)

    if not vocab_path.exists():
        raise FileNotFoundError(f"Vocabulary file not found: {vocab_path}")

    data = json.loads(vocab_path.read_text(encoding="utf-8"))

    scheme = require_str(data, "scheme", source=str(vocab_path))
    scheme_version = data.get("scheme_version")
    is_ade = bool(data.get("is_ade", False))

    # One namespace usually covers a whole registry, so it is a top-level
    # default; a concept may still override it (a CityGML registry spans
    # several module namespaces).
    default_source_namespace = data.get("source_namespace")

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

        local_name = require_str(item, "local_name", source=str(vocab_path))

        if item.get("class_uri") is not None:
            class_uri = require_str(item, "class_uri", source=str(vocab_path))
        else:
            # Minimal-vocabulary convenience: a concept without an explicit
            # URI gets a scheme-derived one, so local/temporary schemes only
            # need names.
            class_uri = f"{scheme}:{local_name}"

        parent_class_id = None
        parent_uri = item.get("parent_uri")

        if parent_uri is not None:
            parent_class_id = _resolve_optional_parent(
                pkg,
                parent_uri,
                scheme=scheme,
                vocab_path=vocab_path,
                child_uri=class_uri,
            )

        # create_semantic_class is itself idempotent on class_uri (the globally
        # unique key), which is what makes vocabulary seeding idempotent, and
        # backfills NULL fields, which is what makes a re-seed enriching.
        class_id = pkg.create_semantic_class(
            scheme=scheme,
            scheme_version=scheme_version,
            class_uri=class_uri,
            local_name=local_name,
            parent_class_id=parent_class_id,
            is_ade=is_ade,
            source_namespace=item.get(
                "source_namespace",
                default_source_namespace,
            ),
            concept_iri=item.get("concept_iri"),
        )

        by_name[local_name] = class_id
        by_uri[class_uri] = class_id

    return VocabularyResult(by_name=by_name, by_uri=by_uri)


def seed_default_citygml_vocabulary(pkg: USAPPackage) -> VocabularyResult:
    return seed_vocabulary_file(pkg, DEFAULT_CITYGML_VOCABULARY_PATH)


def seed_default_ade_vocabulary(pkg: USAPPackage) -> VocabularyResult:
    return seed_vocabulary_file(pkg, DEFAULT_ADE_VOCABULARY_PATH)


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
    except USAPAmbiguityError:
        # Ambiguity is definitive (and its message lists the options);
        # do not mask it as "not registered".
        raise
    except USAPError:
        pass

    try:
        # Then try global resolution. This allows ADE/custom concepts
        # to inherit from CityGML class URIs.
        return pkg.resolve_semantic_class(parent_ref)
    except USAPAmbiguityError:
        raise
    except USAPError as exc:
        raise ValueError(
            f"{vocab_path}: parent concept {parent_ref!r} for "
            f"{child_uri!r} is not registered yet. Put parent concepts "
            "before children, and load parent vocabularies before child "
            "vocabularies."
        ) from exc
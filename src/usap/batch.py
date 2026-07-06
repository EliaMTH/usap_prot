from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._util import require_str
from .core import USAPPackage
from .errors import USAPError
from .domain_vocab import seed_vocabulary_file
from .constants import normalize_element_kind


@dataclass(frozen=True)
class BatchAnnotationResult:
    annotation_id: int
    annotation_uid: str
    concept: str
    membership_count: int


@dataclass
class BatchImportResult:
    annotation_count: int = 0
    membership_count: int = 0
    annotations: list[BatchAnnotationResult] = field(default_factory=list)


def apply_annotation_batch_file(
    pkg: USAPPackage,
    path: str | Path,
    *,
    load_vocabularies: bool = True,
    replace_existing: bool = False,
) -> BatchImportResult:
    batch_path = Path(path)

    if not batch_path.exists():
        raise FileNotFoundError(f"Batch file not found: {batch_path}")

    data = json.loads(batch_path.read_text(encoding="utf-8"))

    return apply_annotation_batch(
        pkg,
        data,
        base_dir=batch_path.parent,
        load_vocabularies=load_vocabularies,
        replace_existing=replace_existing,
    )


def apply_annotation_batch(
    pkg: USAPPackage,
    data: dict[str, Any],
    *,
    base_dir: str | Path = ".",
    load_vocabularies: bool = True,
    replace_existing: bool = False,
) -> BatchImportResult:
    """
    Apply a batch of annotations.

    Batch format:

    {
      "vocabularies": ["vocabularies/citygml_3_0_mvp.json"],
      "annotations": [
        {
          "annotation_uid": "ann_001",
          "concept": "EnergyRoof",
          "city_object_uid": "building_1_roof_1",
          "label": "Energy roof annotation",
          "status": "draft",
          "confidence": 0.8,
          "attributes": {...},
          "memberships": [
            {
              "asset_part_id": 1,
              "element_kind": "point",
              "element_indices": [100, 101]
            }
          ]
        }
      ]
    }
    """
    base_path = Path(base_dir)

    vocabularies = data.get("vocabularies", [])
    annotations = data.get("annotations")

    if load_vocabularies and not isinstance(vocabularies, list):
        raise ValueError("'vocabularies' must be a list when provided.")

    if not isinstance(annotations, list):
        raise ValueError("Batch data must contain an 'annotations' list.")

    result = BatchImportResult()

    # One transaction for the whole batch, vocabularies included, so a
    # failing annotation does not leave half-seeded vocabularies behind.
    with pkg.transaction():
        if load_vocabularies:
            for vocab in vocabularies:
                if not isinstance(vocab, str):
                    raise ValueError(f"Invalid vocabulary path: {vocab!r}")

                vocab_path = Path(vocab)

                if not vocab_path.is_absolute():
                    vocab_path = base_path / vocab_path

                seed_vocabulary_file(pkg, vocab_path)

        for item in annotations:
            annotation_result = _apply_one_annotation(
                pkg,
                item,
                replace_existing=replace_existing,
            )

            result.annotations.append(annotation_result)
            result.annotation_count += 1
            result.membership_count += annotation_result.membership_count

    return result


def _apply_one_annotation(
    pkg: USAPPackage,
    item: dict[str, Any],
    *,
    replace_existing: bool,
) -> BatchAnnotationResult:
    if not isinstance(item, dict):
        raise ValueError(f"Annotation entry must be an object: {item!r}")

    annotation_uid = require_str(item, "annotation_uid")
    concept = require_str(item, "concept")

    existing = pkg.get_annotation(
        annotation_uid=annotation_uid,
        include_membership_summary=False,
    )

    if existing is not None and not replace_existing:
        raise USAPError(
            f"Annotation already exists: {annotation_uid}. "
            "Use replace_existing=True to update it."
        )

    city_object_id = item.get("city_object_id")
    city_object_uid = item.get("city_object_uid")

    if city_object_id is not None and city_object_uid is not None:
        raise USAPError(
            f"{annotation_uid}: provide city_object_id or city_object_uid, not both."
        )

    attributes = item.get("attributes")
    attributes_json = item.get("attributes_json")

    if attributes is not None and attributes_json is not None:
        raise USAPError(
            f"{annotation_uid}: provide attributes or attributes_json, not both."
        )

    if existing is None:
        annotation = pkg.create_concept_annotation(
            concept=concept,
            annotation_uid=annotation_uid,
            city_object_id=city_object_id,
            city_object_uid=city_object_uid,
            label=item.get("label"),
            status=item.get("status", "draft"),
            confidence=item.get("confidence"),
            attributes=attributes,
            attributes_json=attributes_json,
            scheme=item.get("scheme"),
        )

        annotation_id = int(annotation["annotation_id"])

    else:
        # Replace is a partial update: only fields present in the batch entry
        # are changed. Omitted fields keep their existing values (relying on
        # update_annotation's _UNSET-by-omission behavior).
        update_kwargs: dict[str, Any] = {
            "semantic_class_id": pkg.resolve_semantic_class(
                concept,
                scheme=item.get("scheme"),
            ),
        }

        if "label" in item:
            update_kwargs["label"] = item.get("label")

        if "status" in item:
            update_kwargs["status"] = item.get("status")

        if "confidence" in item:
            update_kwargs["confidence"] = item.get("confidence")

        resolved_city_object_id = None

        if city_object_id is not None:
            resolved_city_object_id = pkg.resolve_city_object(int(city_object_id))

        if city_object_uid is not None:
            resolved_city_object_id = pkg.resolve_city_object(str(city_object_uid))

        if city_object_id is not None or city_object_uid is not None:
            update_kwargs["primary_city_object_id"] = resolved_city_object_id

        if attributes is not None:
            update_kwargs["attributes_json"] = json.dumps(attributes)
        elif attributes_json is not None:
            update_kwargs["attributes_json"] = attributes_json

        updated = pkg.update_annotation(
            int(existing["annotation_id"]),
            **update_kwargs,
        )

        annotation_id = int(updated["annotation_id"])

        # Match the create path: when a city object is supplied, link it.
        if resolved_city_object_id is not None:
            pkg.link_annotation_to_object(
                annotation_id=annotation_id,
                city_object_id=resolved_city_object_id,
            )

    memberships = item.get("memberships")

    if not isinstance(memberships, list) or not memberships:
        raise ValueError(
            f"{annotation_uid}: 'memberships' must be a non-empty list."
        )

    membership_count = 0

    for membership in memberships:
        _apply_one_membership(
            pkg,
            annotation_id=annotation_id,
            annotation_uid=annotation_uid,
            membership=membership,
        )

        membership_count += 1

    return BatchAnnotationResult(
        annotation_id=annotation_id,
        annotation_uid=annotation_uid,
        concept=concept,
        membership_count=membership_count,
    )


def _apply_one_membership(
    pkg: USAPPackage,
    *,
    annotation_id: int,
    annotation_uid: str,
    membership: dict[str, Any],
) -> None:
    if not isinstance(membership, dict):
        raise ValueError(
            f"{annotation_uid}: membership must be an object: {membership!r}"
        )

    asset_part_id = membership.get("asset_part_id")
    element_indices = membership.get("element_indices")

    if not isinstance(asset_part_id, int):
        raise ValueError(f"{annotation_uid}: membership.asset_part_id must be int.")

    if "element_kind" not in membership:
        raise ValueError(f"{annotation_uid}: membership.element_kind is required.")

    element_kind = normalize_element_kind(membership["element_kind"])

    if not isinstance(element_indices, list) or not element_indices:
        raise ValueError(
            f"{annotation_uid}: membership.element_indices must be a non-empty list."
        )

    if not all(isinstance(i, int) for i in element_indices):
        raise ValueError(
            f"{annotation_uid}: membership.element_indices must contain only ints."
        )

    # Asset-part existence, element-kind match, and index-bounds are validated by
    # core.replace_annotation_membership (via _validate_membership_indices), so we
    # do not duplicate those checks here.
    pkg.attach_annotation_elements(
        annotation_id=annotation_id,
        asset_part_id=asset_part_id,
        element_kind=element_kind,
        element_indices=element_indices,
    )


from __future__ import annotations

from dataclasses import dataclass

from .core import USAPPackage


@dataclass(frozen=True)
class VocabularyResult:
    by_name: dict[str, int]
    by_uri: dict[str, int]


def seed_citygml_basic_classes(pkg: USAPPackage) -> VocabularyResult:
    """
    Seed basic CityGML-inspired semantic classes.

    This is not a full CityGML schema import.
    It creates the class references needed for prototype annotation.
    """
    by_name: dict[str, int] = {}
    by_uri: dict[str, int] = {}

    def add(
        local_name: str,
        class_uri: str,
        parent_name: str | None = None,
    ) -> int:
        parent_id = by_name[parent_name] if parent_name else None

        class_id = pkg.create_semantic_class(
            scheme="citygml",
            scheme_version="3.0",
            class_uri=class_uri,
            local_name=local_name,
            parent_class_id=parent_id,
            is_ade=False,
        )

        by_name[local_name] = class_id
        by_uri[class_uri] = class_id

        return class_id

    add("Building", "citygml-3.0:bldg:Building")
    add("BuildingPart", "citygml-3.0:bldg:BuildingPart")

    add("BoundarySurface", "citygml-3.0:bldg:BoundarySurface")
    add("RoofSurface", "citygml-3.0:bldg:RoofSurface", "BoundarySurface")
    add("WallSurface", "citygml-3.0:bldg:WallSurface", "BoundarySurface")
    add("GroundSurface", "citygml-3.0:bldg:GroundSurface", "BoundarySurface")
    add("ClosureSurface", "citygml-3.0:bldg:ClosureSurface", "BoundarySurface")
    add("OuterCeilingSurface", "citygml-3.0:bldg:OuterCeilingSurface", "BoundarySurface")
    add("OuterFloorSurface", "citygml-3.0:bldg:OuterFloorSurface", "BoundarySurface")

    add("Opening", "citygml-3.0:bldg:Opening")
    add("Window", "citygml-3.0:bldg:Window", "Opening")
    add("Door", "citygml-3.0:bldg:Door", "Opening")

    return VocabularyResult(by_name=by_name, by_uri=by_uri)


def seed_prototype_ade_classes(pkg: USAPPackage) -> VocabularyResult:
    """
    Seed prototype ADE/domain classes.

    These are intentionally stored as ADE/custom semantic classes.
    Later, when your ADE is formalized, the URIs can be replaced by the
    official ADE namespace/class URIs without changing the membership model.
    """
    by_name: dict[str, int] = {}
    by_uri: dict[str, int] = {}

    def add(local_name: str, class_uri: str) -> int:
        class_id = pkg.create_semantic_class(
            scheme="usap-ade-prototype",
            scheme_version="0.1",
            class_uri=class_uri,
            local_name=local_name,
            parent_class_id=None,
            is_ade=True,
        )

        by_name[local_name] = class_id
        by_uri[class_uri] = class_id

        return class_id

    # Shared / reusable domain concepts
    add("Facade", "usap-ade-prototype:common:Facade")
    add("ExternalSurface", "usap-ade-prototype:common:ExternalSurface")
    add("UrbanZone", "usap-ade-prototype:common:UrbanZone")
    add("BuildingElement", "usap-ade-prototype:common:BuildingElement")

    # Energy / emissions
    add("EnergyBuilding", "usap-ade-prototype:energy:EnergyBuilding")
    add("EnergyFacade", "usap-ade-prototype:energy:EnergyFacade")
    add("EnergyRoof", "usap-ade-prototype:energy:EnergyRoof")

    # Soil permeability
    add("PermeabilityExternalSurface", "usap-ade-prototype:soil:PermeabilityExternalSurface")
    add("PermeabilityUrbanZone", "usap-ade-prototype:soil:PermeabilityUrbanZone")

    # Acoustic comfort
    add("AcousticBuilding", "usap-ade-prototype:acoustic:AcousticBuilding")
    add("AcousticUrbanArea", "usap-ade-prototype:acoustic:AcousticUrbanArea")
    add("ScreeningElement", "usap-ade-prototype:acoustic:ScreeningElement")

    # Visual well-being
    add("VisualBuilding", "usap-ade-prototype:visual:VisualBuilding")
    add("VisualFacade", "usap-ade-prototype:visual:VisualFacade")

    return VocabularyResult(by_name=by_name, by_uri=by_uri)
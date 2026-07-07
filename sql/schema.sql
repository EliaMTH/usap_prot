-- NOTE:
-- Postponed / not-yet-implemented tables:
--
-- 1. Optional spatial acceleration:
--    usap_annotation_extent_rtree
--
-- 2. Optional external feature-ID bridge:
--    usap_feature_id_binding
--
-- 3. Future extension/schema tables:
--    usap_extension_registry
--    usap_attribute_schema
--    project-specific ADE tables
--
-- 4. Migration/versioning tables:
--    usap_schema_migration or similar

-- ORDER MATTERS!
-- Raw layout:
-- 1. profile / metadata tables
-- 2. usap_asset
-- 3. usap_asset_part
-- 4. usap_semantic_class
-- 5. usap_semantic_class_closure
-- 6. usap_city_object
-- 7. usap_city_object_relationship
-- 8. usap_city_object_closure
-- 9. usap_annotation
-- 10. usap_annotation_object
-- 11. usap_membership_block
-- 12. usap_value_block
-- 13. optional indexes
-- 14. optional helper tables
-- 15. GIS-facing views (attributes + features layers for QGIS/GDAL)

PRAGMA foreign_keys = ON; -- ensure key consistency between tables (i.e.: prevent operations that would break relationship between tables); need to be declare as it is off by default for backwards compatibility

-- -------------------------------------------------------------------------
-- Minimal GeoPackage core metadata
-- -------------------------------------------------------------------------

CREATE TABLE gpkg_spatial_ref_sys (
    srs_name TEXT NOT NULL,
    srs_id INTEGER NOT NULL PRIMARY KEY,
    organization TEXT NOT NULL,
    organization_coordsys_id INTEGER NOT NULL,
    definition TEXT NOT NULL,
    description TEXT
);

CREATE TABLE gpkg_contents (
    table_name TEXT NOT NULL PRIMARY KEY,
    data_type TEXT NOT NULL,
    identifier TEXT UNIQUE,
    description TEXT DEFAULT '',
    last_change DATETIME NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    min_x DOUBLE,
    min_y DOUBLE,
    max_x DOUBLE,
    max_y DOUBLE,
    srs_id INTEGER,
    CONSTRAINT fk_gc_r_srs_id
        FOREIGN KEY (srs_id)
        REFERENCES gpkg_spatial_ref_sys(srs_id)
);

CREATE TABLE gpkg_extensions (
    table_name TEXT,
    column_name TEXT,
    extension_name TEXT NOT NULL,
    definition TEXT NOT NULL,
    scope TEXT NOT NULL,
    CONSTRAINT ge_tce
        UNIQUE (table_name, column_name, extension_name)
);

CREATE TABLE gpkg_geometry_columns (
    table_name          TEXT NOT NULL,
    column_name         TEXT NOT NULL,
    geometry_type_name  TEXT NOT NULL,
    srs_id              INTEGER NOT NULL,
    z                   TINYINT NOT NULL,
    m                   TINYINT NOT NULL,

    CONSTRAINT pk_geom_cols
        PRIMARY KEY (table_name, column_name),

    CONSTRAINT fk_gc_contents
        FOREIGN KEY (table_name)
        REFERENCES gpkg_contents(table_name),

    CONSTRAINT fk_gc_srs
        FOREIGN KEY (srs_id)
        REFERENCES gpkg_spatial_ref_sys(srs_id)
);

CREATE TABLE usap_profile (
    profile_id          INTEGER PRIMARY KEY CHECK (profile_id = 1),
    profile_name        TEXT NOT NULL DEFAULT 'USAP',
    profile_version     TEXT NOT NULL,
    default_block_size  INTEGER NOT NULL DEFAULT 4096,
    default_encoding    TEXT NOT NULL DEFAULT 'u32-zlib',
    metadata_json       TEXT
);

CREATE TABLE usap_asset (
    asset_id       INTEGER PRIMARY KEY,
    uri            TEXT NOT NULL,
    asset_kind     TEXT NOT NULL,
    media_type     TEXT,
    content_hash   TEXT,
    srs_id         INTEGER,
    metadata_json  TEXT,

    UNIQUE(uri, content_hash)
);

CREATE TABLE usap_asset_part (
    asset_part_id   INTEGER PRIMARY KEY,

    asset_id        INTEGER NOT NULL -- example of a foreign_key 
        REFERENCES usap_asset(asset_id)
        ON DELETE CASCADE,

    part_path       TEXT NOT NULL,
    element_kind    INTEGER NOT NULL,
    element_count   INTEGER NOT NULL,
    index_origin    TEXT NOT NULL DEFAULT 'zero_based', -- declaring it to avoid doubts

    minx            REAL,
    miny            REAL,
    minz            REAL,
    maxx            REAL,
    maxy            REAL,
    maxz            REAL,

    metadata_json   TEXT,

    UNIQUE(asset_id, part_path, element_kind)
);

-- Derived cartographic summary: one GPKG-encoded 2D bounding-box polygon per
-- asset, the union of its parts' stored bounds. Written by
-- register_asset_part; regenerable at any time from usap_asset_part — never
-- authoritative geometry. Exposed to GIS tools via the usap_asset_extents
-- view (registered as a features layer).
CREATE TABLE usap_asset_extent (
    asset_id  INTEGER PRIMARY KEY
        REFERENCES usap_asset(asset_id)
        ON DELETE CASCADE,

    geom      BLOB NOT NULL   -- GeoPackageBinary (magic 'GP') POLYGON
);

CREATE TABLE usap_semantic_class (
    semantic_class_id  INTEGER PRIMARY KEY,
    scheme             TEXT NOT NULL,
    scheme_version     TEXT,
    class_uri          TEXT NOT NULL,
    local_name         TEXT NOT NULL,

    parent_class_id    INTEGER
        REFERENCES usap_semantic_class(semantic_class_id),

    is_ade             INTEGER NOT NULL DEFAULT 0,
    metadata_json      TEXT,

    UNIQUE(class_uri)
);

CREATE TABLE usap_semantic_class_closure (
    ancestor_class_id    INTEGER NOT NULL
        REFERENCES usap_semantic_class(semantic_class_id)
        ON DELETE CASCADE,

    descendant_class_id  INTEGER NOT NULL
        REFERENCES usap_semantic_class(semantic_class_id)
        ON DELETE CASCADE,

    depth                INTEGER NOT NULL,

    PRIMARY KEY (ancestor_class_id, descendant_class_id)
) WITHOUT ROWID;

-- The PK serves ancestor -> descendants; this serves the reverse direction,
-- used when a new class inherits all of its parent's ancestors.
CREATE INDEX usap_scc_by_descendant
ON usap_semantic_class_closure(
    descendant_class_id
);

CREATE TABLE usap_city_object (
    city_object_id     INTEGER PRIMARY KEY,
    object_uid         TEXT NOT NULL UNIQUE,

    semantic_class_id  INTEGER
        REFERENCES usap_semantic_class(semantic_class_id),

    gml_id             TEXT,

    source_asset_id    INTEGER
        REFERENCES usap_asset(asset_id)
        ON DELETE SET NULL,

    source_object_id   TEXT,

    object_status      TEXT NOT NULL DEFAULT 'accepted',

    attributes_json    TEXT
);

CREATE TABLE usap_city_object_relationship (
    relationship_id        INTEGER PRIMARY KEY,

    graph_name             TEXT NOT NULL DEFAULT 'usap_default',

    parent_city_object_id  INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    child_city_object_id   INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    relationship_type      TEXT NOT NULL,

    role                   TEXT,

    source_asset_id        INTEGER
        REFERENCES usap_asset(asset_id)
        ON DELETE SET NULL,

    source_relation_id     TEXT,

    metadata_json          TEXT
);

CREATE INDEX usap_rel_by_parent_graph
ON usap_city_object_relationship(
    graph_name,
    parent_city_object_id,
    relationship_type
);

CREATE INDEX usap_rel_by_child_graph
ON usap_city_object_relationship(
    graph_name,
    child_city_object_id,
    relationship_type
);

CREATE TABLE usap_city_object_closure (
    graph_name                  TEXT NOT NULL,

    ancestor_city_object_id     INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    descendant_city_object_id   INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    depth                       INTEGER NOT NULL,

    PRIMARY KEY (
        graph_name,
        ancestor_city_object_id,
        descendant_city_object_id
    )
) WITHOUT ROWID;

CREATE TABLE usap_annotation (
    annotation_id          INTEGER PRIMARY KEY,
    annotation_uid         TEXT NOT NULL UNIQUE,

    semantic_class_id      INTEGER NOT NULL
        REFERENCES usap_semantic_class(semantic_class_id),

    primary_city_object_id INTEGER
        REFERENCES usap_city_object(city_object_id)
        ON DELETE SET NULL,

    label                  TEXT,
    status                 TEXT NOT NULL DEFAULT 'accepted',
    confidence             REAL,
    attributes_json        TEXT,

    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Lets class-hierarchy queries start from the (small) closure and reach
-- annotations by index instead of scanning all membership blocks.
CREATE INDEX usap_annotation_by_class
ON usap_annotation(
    semantic_class_id
);

CREATE TABLE usap_annotation_object (
    annotation_id   INTEGER NOT NULL
        REFERENCES usap_annotation(annotation_id)
        ON DELETE CASCADE,

    city_object_id  INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    relation_type   TEXT NOT NULL DEFAULT 'represents',

    PRIMARY KEY(annotation_id, city_object_id, relation_type)
) WITHOUT ROWID;

CREATE TABLE usap_membership_block (
    membership_block_id INTEGER PRIMARY KEY,

    annotation_id       INTEGER NOT NULL
        REFERENCES usap_annotation(annotation_id)
        ON DELETE CASCADE,

    asset_part_id       INTEGER NOT NULL
        REFERENCES usap_asset_part(asset_part_id)
        ON DELETE CASCADE,

    element_kind        INTEGER NOT NULL,

    block_start         INTEGER NOT NULL,
    block_size          INTEGER NOT NULL,
    encoding            TEXT NOT NULL,

    element_count       INTEGER NOT NULL,
    min_element_index   INTEGER NOT NULL,
    max_element_index   INTEGER NOT NULL,

    payload             BLOB NOT NULL,

    -- The auto-index behind this constraint doubles as the annotation-first
    -- lookup index (forward queries, annotation-delete cascade); do not add
    -- an explicit index on the same columns.
    UNIQUE(annotation_id, asset_part_id, element_kind, block_start)
);

-- Serves the reverse element query (annotations_for_elements) and the
-- ON DELETE CASCADE scan from usap_asset_part.
CREATE INDEX usap_mb_by_element_block
ON usap_membership_block(
    asset_part_id,
    element_kind,
    block_start
);

-- Per-element value fields: dense scalar arrays over an asset part's
-- elements (element i's value = decoded[i - block_start]). Sibling of
-- usap_membership_block: membership stores WHICH elements are a concept,
-- value blocks store the VALUE of a property at each element. Bound to the
-- asset part only — never to a city object.
CREATE TABLE usap_value_block (
    value_block_id      INTEGER PRIMARY KEY,

    annotation_id       INTEGER NOT NULL
        REFERENCES usap_annotation(annotation_id)
        ON DELETE CASCADE,

    asset_part_id       INTEGER NOT NULL
        REFERENCES usap_asset_part(asset_part_id)
        ON DELETE CASCADE,

    element_kind        INTEGER NOT NULL,

    block_start         INTEGER NOT NULL,
    element_count       INTEGER NOT NULL,

    value_dtype         TEXT NOT NULL,   -- 'f4', 'f2', 'u1', ... little-endian

    value_min           REAL,            -- NaN-ignoring block min; NULL if all-NaN
    value_max           REAL,

    payload             BLOB NOT NULL,   -- zlib(values.tobytes())

    -- The auto-index behind this constraint doubles as the annotation-first
    -- lookup index (value readers, annotation-delete cascade); do not add
    -- an explicit index on the same columns.
    UNIQUE(annotation_id, asset_part_id, element_kind, block_start)
);

-- Serves the ON DELETE CASCADE scan from usap_asset_part (no part-level
-- value query exists yet).
CREATE INDEX usap_vb_by_part
ON usap_value_block(
    asset_part_id,
    element_kind
);

CREATE TABLE usap_edit_log (
    edit_id       INTEGER PRIMARY KEY,
    operation     TEXT NOT NULL,
    target_table  TEXT,
    target_id     INTEGER,
    details_json  TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- -------------------------------------------------------------------------
-- GIS-facing views. Registered in gpkg_contents (see geopackage.py) so
-- QGIS/GDAL can browse USAP content: three read-only 'attributes' layers
-- plus one 'features' layer of derived per-asset extent boxes. Internal
-- tables (blocks, closures, edit log) are deliberately not exposed.
-- -------------------------------------------------------------------------

-- Every view exposes its integer key as "fid": SQLite views have no rowid,
-- and OGR/QGIS reliably map a column named fid to the feature id (older
-- GDAL versions fail to open views without it).
CREATE VIEW usap_annotations_view AS
SELECT
    a.annotation_id AS fid,
    a.annotation_uid,
    sc.local_name AS concept,
    sc.class_uri AS concept_uri,
    sc.scheme,
    co.object_uid AS city_object_uid,
    a.label,
    a.status,
    a.confidence,
    (
        SELECT COALESCE(SUM(mb.element_count), 0)
        FROM usap_membership_block AS mb
        WHERE mb.annotation_id = a.annotation_id
    ) AS selected_element_count,
    (
        SELECT COUNT(DISTINCT vb.asset_part_id)
        FROM usap_value_block AS vb
        WHERE vb.annotation_id = a.annotation_id
    ) AS value_field_count,
    a.attributes_json,
    a.created_at,
    a.updated_at
FROM usap_annotation AS a
JOIN usap_semantic_class AS sc
    ON sc.semantic_class_id = a.semantic_class_id
LEFT JOIN usap_city_object AS co
    ON co.city_object_id = a.primary_city_object_id;

CREATE VIEW usap_concepts_view AS
SELECT
    sc.semantic_class_id AS fid,
    sc.scheme,
    sc.scheme_version,
    sc.class_uri,
    sc.local_name,
    sc.is_ade,
    COUNT(a.annotation_id) AS annotation_count,
    CASE WHEN COUNT(a.annotation_id) > 0 THEN 1 ELSE 0 END AS in_use
FROM usap_semantic_class AS sc
LEFT JOIN usap_annotation AS a
    ON a.semantic_class_id = sc.semantic_class_id
GROUP BY sc.semantic_class_id;

CREATE VIEW usap_city_objects_view AS
SELECT
    co.city_object_id AS fid,
    co.object_uid,
    sc.local_name AS semantic_class,
    co.object_status,
    co.gml_id,
    src.uri AS source_asset_uri
FROM usap_city_object AS co
LEFT JOIN usap_semantic_class AS sc
    ON sc.semantic_class_id = co.semantic_class_id
LEFT JOIN usap_asset AS src
    ON src.asset_id = co.source_asset_id;

CREATE VIEW usap_asset_extents AS
SELECT
    e.asset_id AS fid,
    e.geom,
    a.uri,
    a.asset_kind,
    (
        SELECT COUNT(*)
        FROM usap_asset_part AS ap
        WHERE ap.asset_id = e.asset_id
    ) AS part_count,
    (
        SELECT COALESCE(SUM(ap.element_count), 0)
        FROM usap_asset_part AS ap
        WHERE ap.asset_id = e.asset_id
    ) AS element_count
FROM usap_asset_extent AS e
JOIN usap_asset AS a
    ON a.asset_id = e.asset_id;
"""База даних SQLite Polar: схема і міграції за PRAGMA user_version."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

# Кожна міграція переводить базу з версії i на i+1. Нові зміни — тільки новими елементами списку.
MIGRATIONS = [
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE cameras (
        id                INTEGER PRIMARY KEY,
        name              TEXT NOT NULL UNIQUE,
        model             TEXT NOT NULL DEFAULT '',
        resolution_x      INTEGER NOT NULL,
        resolution_y      INTEGER NOT NULL,
        fov_x_deg         REAL NOT NULL,
        fov_y_deg         REAL NOT NULL,
        latitude          REAL,
        longitude         REAL,
        altitude_m        REAL,
        north_offset_deg  REAL NOT NULL DEFAULT 0,
        tilt_deg          REAL NOT NULL DEFAULT 0,       -- нахил камери за год. стрілкою від вертикалі
        k1 REAL NOT NULL DEFAULT 0, k2 REAL NOT NULL DEFAULT 0,
        p1 REAL NOT NULL DEFAULT 0, p2 REAL NOT NULL DEFAULT 0,
        k3 REAL NOT NULL DEFAULT 0,
        stream_url        TEXT NOT NULL DEFAULT '',
        stream_latency_ms INTEGER NOT NULL DEFAULT 0,     -- затримка відео відносно кутів датчика
        ros_namespace     TEXT NOT NULL DEFAULT '',
        panorama_path     TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE calibration (
        camera_id   INTEGER PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        center_x    REAL NOT NULL,            -- центр камери, пікселі кадру
        center_y    REAL NOT NULL,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE servo_settings (
        camera_id           INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        axis                TEXT NOT NULL CHECK (axis IN ('pan', 'tilt')),
        servo_id            INTEGER NOT NULL CHECK (servo_id BETWEEN 1 AND 253),
        direction           INTEGER NOT NULL DEFAULT 1 CHECK (direction IN (-1, 1)),
        zero_offset_deg     REAL NOT NULL DEFAULT 0,
        max_speed_deg_s     REAL NOT NULL DEFAULT 180,
        acceleration        INTEGER NOT NULL DEFAULT 50,
        torque_limit_pct    REAL NOT NULL DEFAULT 100,
        temperature_limit_c REAL NOT NULL DEFAULT 70,
        min_deg             REAL,
        max_deg             REAL,
        PRIMARY KEY (camera_id, axis)
    );

    CREATE TABLE detection_mask (
        id         INTEGER PRIMARY KEY,
        camera_id  INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        kind       TEXT NOT NULL CHECK (kind IN ('rect', 'el_min', 'el_max')),
        az_from    REAL, az_to REAL,          -- для rect; az_from > az_to = через 0/360
        el_from    REAL, el_to REAL,          -- для rect; для el_min/el_max — el_from
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX detection_mask_camera ON detection_mask(camera_id);

    CREATE TABLE map_levels (
        id         INTEGER PRIMARY KEY,
        zoom       INTEGER NOT NULL UNIQUE CHECK (zoom BETWEEN 1 AND 8),
        scale_m_per_px REAL NOT NULL,
        file_path  TEXT,                       -- GeoTIFF у EPSG:3857
        loaded_at  TEXT
    );

    CREATE TABLE targets (
        id          INTEGER PRIMARY KEY,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL,
        latitude    REAL, longitude REAL, altitude_m REAL,
        distance_m  REAL,
        error_m     REAL,                      -- оцінка похибки тріангуляції
        cameras     INTEGER NOT NULL DEFAULT 1,
        status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'lost'))
    );

    CREATE TABLE detections (
        id          INTEGER PRIMARY KEY,
        target_id   INTEGER REFERENCES targets(id) ON DELETE SET NULL,
        camera_id   INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        ts          TEXT NOT NULL,
        azimuth_deg REAL NOT NULL,
        elevation_deg REAL NOT NULL,
        size_px     REAL,
        snapshot_path TEXT
    );
    CREATE INDEX detections_ts ON detections(ts);

    CREATE TABLE events (
        id        INTEGER PRIMARY KEY,
        ts        TEXT NOT NULL DEFAULT (datetime('now')),
        camera_id INTEGER REFERENCES cameras(id) ON DELETE SET NULL,
        kind      TEXT NOT NULL,              -- mode_change, error, servo_offline, ...
        message   TEXT NOT NULL DEFAULT ''
    );
    """,
    # 2 — етап 1: шина сервоприводів камери і передавальне число
    """
    ALTER TABLE cameras ADD COLUMN servo_port TEXT NOT NULL DEFAULT '/dev/serial0';
    ALTER TABLE cameras ADD COLUMN servo_baudrate INTEGER NOT NULL DEFAULT 1000000;
    ALTER TABLE servo_settings ADD COLUMN gear_ratio REAL NOT NULL DEFAULT 1;
    """,
    # 3 — етап 2: параметри відеопотоку з форми камери
    """
    ALTER TABLE cameras ADD COLUMN stream_protocol TEXT NOT NULL DEFAULT 'RTSP';
    ALTER TABLE cameras ADD COLUMN stream_login TEXT NOT NULL DEFAULT '';
    ALTER TABLE cameras ADD COLUMN stream_password_enc TEXT NOT NULL DEFAULT '';
    ALTER TABLE cameras ADD COLUMN stream_kind TEXT NOT NULL DEFAULT 'main';
    ALTER TABLE cameras ADD COLUMN stream_codec TEXT NOT NULL DEFAULT 'H.264';
    ALTER TABLE cameras ADD COLUMN stream_res_x INTEGER;
    ALTER TABLE cameras ADD COLUMN stream_res_y INTEGER;
    ALTER TABLE cameras ADD COLUMN stream_fps REAL NOT NULL DEFAULT 25;
    ALTER TABLE cameras ADD COLUMN max_detection_latency_ms INTEGER NOT NULL DEFAULT 500;
    ALTER TABLE cameras ADD COLUMN stream_transport TEXT NOT NULL DEFAULT 'TCP';
    ALTER TABLE cameras ADD COLUMN stream_buffer_ms INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE cameras ADD COLUMN panorama_stale INTEGER NOT NULL DEFAULT 0;
    """,
    # 4 — демо окремо для кожної камери: імітатор сервоприводів і/або синтетичне відео
    """
    ALTER TABLE cameras ADD COLUMN demo_servos INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE cameras ADD COLUMN demo_video INTEGER NOT NULL DEFAULT 0;
    """,
    # 5 — етап 3: калібрування — розмір кадру, в якому задано центр, і виміряна похибка розміщення
    """
    ALTER TABLE calibration ADD COLUMN frame_w INTEGER;
    ALTER TABLE calibration ADD COLUMN frame_h INTEGER;
    ALTER TABLE calibration ADD COLUMN placement_error_deg REAL;
    """,
    # 6 — етап 7: рівні карти — зум вебкарти, джерело рівня, межі ділянки (EPSG:3857), тайли, підпис джерела
    """
    ALTER TABLE map_levels ADD COLUMN web_zoom INTEGER;
    ALTER TABLE map_levels ADD COLUMN state TEXT NOT NULL DEFAULT 'none';
    ALTER TABLE map_levels ADD COLUMN source_level INTEGER;
    ALTER TABLE map_levels ADD COLUMN min_x REAL;
    ALTER TABLE map_levels ADD COLUMN min_y REAL;
    ALTER TABLE map_levels ADD COLUMN max_x REAL;
    ALTER TABLE map_levels ADD COLUMN max_y REAL;
    ALTER TABLE map_levels ADD COLUMN tiles_dir TEXT;
    ALTER TABLE map_levels ADD COLUMN attribution TEXT NOT NULL DEFAULT '';
    ALTER TABLE map_levels ADD COLUMN warning TEXT NOT NULL DEFAULT '';
    """,
    # 7 — рівень карти = власний файл у власному масштабі (тайли в сітці пікселів файлу); рівні,
    # побудовані за сіткою вебкарти, несумісні — їх треба завантажити наново
    """
    DELETE FROM map_levels;
    DELETE FROM settings WHERE key IN ('map.main_file', 'map.base_zoom');
    """,
    # 8 — моделі камер: оптика (кути огляду, дисторсія) задається моделлю, камера посилається на модель.
    # Наявні камери: кожен різний набір (назва моделі, оптика) стає моделлю; камери привʼязуються до неї.
    """
    CREATE TABLE camera_models (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        fov_x_deg   REAL NOT NULL,
        fov_y_deg   REAL NOT NULL,
        k1 REAL NOT NULL DEFAULT 0, k2 REAL NOT NULL DEFAULT 0, p1 REAL NOT NULL DEFAULT 0,
        p2 REAL NOT NULL DEFAULT 0, k3 REAL NOT NULL DEFAULT 0,
        sensor_x    INTEGER,                    -- роздільна здатність сенсора (довідково, напр. 256×192)
        sensor_y    INTEGER,
        notes       TEXT NOT NULL DEFAULT '',
        src_key     TEXT,                       -- службове: звідки модель створена міграцією
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    ALTER TABLE cameras ADD COLUMN model_id INTEGER REFERENCES camera_models(id) ON DELETE SET NULL;
    INSERT INTO camera_models (name, fov_x_deg, fov_y_deg, k1, k2, p1, p2, k3, src_key)
    SELECT CASE WHEN (SELECT COUNT(*) FROM (SELECT 1 FROM cameras c2
                                             WHERE TRIM(c2.model) = TRIM(g.model) AND TRIM(g.model) != ''
                                             GROUP BY c2.fov_x_deg, c2.fov_y_deg, c2.k1, c2.k2, c2.p1, c2.p2, c2.k3)) > 1
                     OR TRIM(g.model) = ''
                THEN CASE WHEN TRIM(g.model) = '' THEN g.cam ELSE TRIM(g.model) || ' (' || g.cam || ')' END
                ELSE TRIM(g.model) END,
           g.fov_x_deg, g.fov_y_deg, g.k1, g.k2, g.p1, g.p2, g.k3, g.k
    FROM (SELECT model, MIN(name) AS cam, fov_x_deg, fov_y_deg, k1, k2, p1, p2, k3,
                 TRIM(model) || '|' || fov_x_deg || '|' || fov_y_deg || '|' || k1 || '|' || k2 || '|' || p1 || '|'
                 || p2 || '|' || k3 AS k
          FROM cameras GROUP BY TRIM(model), fov_x_deg, fov_y_deg, k1, k2, p1, p2, k3) g;
    UPDATE cameras SET model_id = (
        SELECT m.id FROM camera_models m WHERE m.src_key = TRIM(cameras.model) || '|' || cameras.fov_x_deg || '|'
            || cameras.fov_y_deg || '|' || cameras.k1 || '|' || cameras.k2 || '|' || cameras.p1 || '|'
            || cameras.p2 || '|' || cameras.k3);
    UPDATE cameras SET model = (SELECT name FROM camera_models WHERE id = cameras.model_id)
        WHERE model_id IS NOT NULL;
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: Union[str, Path], check_same_thread: bool = True) -> sqlite3.Connection:
    """Відкрити базу і довести схему до поточної версії."""
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"База має новішу схему ({version}) ніж програма ({SCHEMA_VERSION})")
    for i in range(version, SCHEMA_VERSION):
        with conn:
            conn.executescript(MIGRATIONS[i])
            conn.execute(f"PRAGMA user_version = {i + 1}")
    return SCHEMA_VERSION

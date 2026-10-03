#!/usr/bin/env python3

import os
import sqlite3
import time as time_module
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from .ugc_scraper import CinemaSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """
CREATE TABLE IF NOT EXISTS cinemas (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT
);

CREATE TABLE IF NOT EXISTS films (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    duration_min  INTEGER,
    release_date  TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cinema_id  INTEGER NOT NULL REFERENCES cinemas(id),
    name       TEXT NOT NULL,
    UNIQUE (cinema_id, name)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    cinema_id        INTEGER REFERENCES cinemas(id),
    mode             TEXT NOT NULL,
    status           TEXT NOT NULL,
    films_count      INTEGER,
    screenings_count INTEGER,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS screenings (
    id                 INTEGER PRIMARY KEY,
    film_id            INTEGER NOT NULL REFERENCES films(id),
    room_id            INTEGER REFERENCES rooms(id),
    screening_date     TEXT NOT NULL,
    start_at           TEXT NOT NULL,
    end_at             TEXT,
    version            TEXT,
    is_pmr             INTEGER NOT NULL DEFAULT 0,
    first_seen_run_id  INTEGER NOT NULL REFERENCES scrape_runs(id),
    last_seen_run_id   INTEGER NOT NULL REFERENCES scrape_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_screenings_film ON screenings(film_id);
CREATE INDEX IF NOT EXISTS idx_screenings_date ON screenings(screening_date);

-- Temps de pub = durée du créneau annoncé - durée du film
CREATE VIEW IF NOT EXISTS v_screening_ads AS
SELECT s.*, f.title, f.duration_min, r.name AS room_name,
       CAST(ROUND((julianday(s.end_at) - julianday(s.start_at)) * 1440) AS INTEGER) AS slot_min,
       CAST(ROUND((julianday(s.end_at) - julianday(s.start_at)) * 1440) AS INTEGER) - f.duration_min AS ad_min
FROM screenings s
JOIN films f ON f.id = s.film_id
LEFT JOIN rooms r ON r.id = s.room_id;

-- Films diffusés dans chaque salle
CREATE VIEW IF NOT EXISTS v_room_films AS
SELECT r.cinema_id, r.name AS room_name, a.film_id, a.title, a.duration_min,
       COUNT(*)                   AS screenings_count,
       MIN(a.screening_date)      AS first_date,
       MAX(a.screening_date)      AS last_date,
       ROUND(AVG(a.ad_min), 1)    AS avg_ad_min,
       MIN(a.ad_min)              AS min_ad_min,
       MAX(a.ad_min)              AS max_ad_min
FROM v_screening_ads a
JOIN rooms r ON r.id = a.room_id
GROUP BY r.id, a.film_id;

-- Part des séances de chaque film dans la semaine cinéma (mercredi -> mardi)
CREATE VIEW IF NOT EXISTS v_film_share AS
WITH weekly AS (
    SELECT date(s.screening_date,
                '-' || ((CAST(strftime('%w', s.screening_date) AS INTEGER) + 4) % 7) || ' days') AS week_start,
           s.film_id, COUNT(*) AS screenings_count
    FROM screenings s
    GROUP BY week_start, s.film_id
)
SELECT w.week_start, w.film_id, f.title, w.screenings_count,
       SUM(w.screenings_count) OVER (PARTITION BY w.week_start) AS week_total,
       ROUND(100.0 * w.screenings_count / SUM(w.screenings_count) OVER (PARTITION BY w.week_start), 1) AS share_pct
FROM weekly w
JOIN films f ON f.id = w.film_id;

-- Part de VO par film
CREATE VIEW IF NOT EXISTS v_vo_share AS
SELECT f.id AS film_id, f.title,
       COUNT(*) AS screenings_count,
       SUM(s.version LIKE 'VO%') AS vo_count,
       SUM(s.version NOT LIKE 'VO%' OR s.version IS NULL) AS vf_count,
       ROUND(100.0 * SUM(s.version LIKE 'VO%') / COUNT(*), 1) AS vo_pct
FROM screenings s
JOIN films f ON f.id = s.film_id
GROUP BY f.id;

-- Période à l'affiche de chaque film (avant-premières exclues)
CREATE VIEW IF NOT EXISTS v_film_on_screen AS
SELECT f.id AS film_id, f.title, f.release_date,
       MIN(CASE WHEN f.release_date IS NULL OR s.screening_date >= f.release_date
                THEN s.screening_date END)                         AS first_screening_date,
       MAX(s.screening_date)                                       AS last_screening_date,
       CAST(julianday(MAX(s.screening_date))
          - julianday(MIN(CASE WHEN f.release_date IS NULL OR s.screening_date >= f.release_date
                               THEN s.screening_date END)) AS INTEGER) + 1 AS days_on_screen,
       SUM(f.release_date IS NOT NULL AND s.screening_date < f.release_date) AS premiere_count,
       MAX(s.screening_date) >= date('now', 'localtime')           AS still_showing
FROM screenings s
JOIN films f ON f.id = s.film_id
GROUP BY f.id;
"""

# Actions SQLite autorisées dans la console SQL : lecture uniquement
READONLY_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def default_db_path() -> Path:
    """Chemin de la base, depuis UGC_DB_PATH (relatif à la racine du projet)."""
    path = Path(os.getenv("UGC_DB_PATH", "output/ugc_stats.db"))
    return path if path.is_absolute() else PROJECT_ROOT / path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _minutes(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


class StatsDB:
    """Base SQLite d'historisation des films et séances UGC."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def start_run(self, mode: str) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO scrape_runs (started_at, mode, status) VALUES (?, ?, 'running')",
                (_now(), mode),
            )
        return cursor.lastrowid

    def finish_run(self, run_id: int, status: str, error: Optional[str] = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE scrape_runs SET finished_at = ?, status = ?, error = ? WHERE id = ?",
                (_now(), status, error, run_id),
            )

    def save_snapshot(self, run_id: int, snapshot: CinemaSnapshot) -> int:
        """Enregistre films, salles et séances d'un snapshot. Retourne le nombre de séances."""
        now = _now()
        cinema_id = snapshot.cinema_id
        room_ids: Dict[str, int] = {}
        saved = 0

        with self.conn:
            if cinema_id is not None:
                self.conn.execute(
                    """
                    INSERT INTO cinemas (id, name, url) VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET name = excluded.name, url = excluded.url
                    """,
                    (cinema_id, snapshot.cinema_name or str(cinema_id), snapshot.url),
                )

            for film in snapshot.films.values():
                self.conn.execute(
                    """
                    INSERT INTO films (id, title, duration_min, release_date, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        duration_min = COALESCE(excluded.duration_min, films.duration_min),
                        release_date = COALESCE(excluded.release_date, films.release_date),
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        film.id,
                        film.title,
                        film.duration_min,
                        film.release_date.isoformat() if film.release_date else None,
                        now,
                        now,
                    ),
                )

            for screening in snapshot.screenings:
                if screening.showing_id is None:
                    continue

                room_id = None
                if cinema_id is not None and screening.room:
                    room_id = room_ids.get(screening.room)
                    if room_id is None:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO rooms (cinema_id, name) VALUES (?, ?)",
                            (cinema_id, screening.room),
                        )
                        room_id = self.conn.execute(
                            "SELECT id FROM rooms WHERE cinema_id = ? AND name = ?",
                            (cinema_id, screening.room),
                        ).fetchone()[0]
                        room_ids[screening.room] = room_id

                end = screening.end_datetime
                self.conn.execute(
                    """
                    INSERT INTO screenings (
                        id, film_id, room_id, screening_date, start_at, end_at,
                        version, is_pmr, first_seen_run_id, last_seen_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        film_id = excluded.film_id,
                        room_id = excluded.room_id,
                        screening_date = excluded.screening_date,
                        start_at = excluded.start_at,
                        end_at = excluded.end_at,
                        version = excluded.version,
                        is_pmr = excluded.is_pmr,
                        last_seen_run_id = excluded.last_seen_run_id
                    """,
                    (
                        screening.showing_id,
                        screening.film_id,
                        room_id,
                        screening.date.isoformat(),
                        _minutes(screening.start_datetime),
                        _minutes(end) if end else None,
                        screening.version,
                        int(screening.is_pmr),
                        run_id,
                        run_id,
                    ),
                )
                saved += 1

            self.conn.execute(
                "UPDATE scrape_runs SET cinema_id = ?, films_count = ?, screenings_count = ? WHERE id = ?",
                (cinema_id, len(snapshot.films), saved, run_id),
            )

        return saved


class ScrapeRunRecorder:
    """Enregistre un scraping en base sans jamais faire échouer le pipeline.

    Usage :
        with ScrapeRunRecorder("seances") as recorder:
            snapshot = scrapper.scrape_cinema(url)
            recorder.save(snapshot)
    """

    def __init__(self, mode: str, path: Optional[Path] = None):
        self.mode = mode
        self.path = path
        self.db: Optional[StatsDB] = None
        self.run_id: Optional[int] = None

    def __enter__(self) -> "ScrapeRunRecorder":
        try:
            self.db = StatsDB(self.path)
            self.run_id = self.db.start_run(self.mode)
        except Exception as exc:
            logger.warning("Base de statistiques indisponible: {}", exc)
            self._close()
        return self

    def save(self, snapshot: CinemaSnapshot) -> None:
        if self.db is None or self.run_id is None:
            return
        try:
            saved = self.db.save_snapshot(self.run_id, snapshot)
            self.db.finish_run(self.run_id, "success")
            logger.success("{} séance(s) enregistrée(s) dans {}",
                           saved, self.db.path)
        except Exception as exc:
            logger.warning("Enregistrement en base impossible: {}", exc)
            self._finish_failed(exc)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self._finish_failed(exc)
        self._close()
        return False

    def _finish_failed(self, exc: BaseException) -> None:
        if self.db is None or self.run_id is None:
            return
        try:
            self.db.finish_run(self.run_id, "failed", str(exc))
        except Exception:
            pass

    def _close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None


class QueryError(ValueError):
    """Erreur de requête SQL renvoyée telle quelle à l'utilisateur."""


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(
            "La base n'existe pas encore : lancez le pipeline une première fois")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _json_value(value):
    if isinstance(value, bytes):
        return value.hex()
    return value


def run_readonly_query(
    query: str,
    path: Optional[Path] = None,
    max_rows: int = 1000,
    timeout_s: float = 5.0,
) -> dict:
    """Exécute une requête en lecture seule et retourne colonnes et lignes."""
    query = query.strip()
    if not query:
        raise QueryError("Requête vide")

    conn = _connect_readonly(Path(path) if path else default_db_path())
    try:
        conn.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_OK if action in READONLY_ACTIONS else sqlite3.SQLITE_DENY
        )
        deadline = time_module.monotonic() + timeout_s
        conn.set_progress_handler(
            lambda: int(time_module.monotonic() > deadline), 10000)

        started = time_module.monotonic()
        try:
            cursor = conn.execute(query)
            columns = [col[0] for col in cursor.description or []]
            rows = cursor.fetchmany(max_rows + 1) if columns else []
        except (sqlite3.ProgrammingError, sqlite3.Warning) as exc:
            if "one statement at a time" in str(exc):
                raise QueryError(
                    "Une seule requête à la fois") from exc
            raise QueryError(str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            message = str(exc)
            if message == "interrupted":
                message = f"Requête interrompue après {timeout_s:g} s"
            elif message in ("not authorized", "authorization denied"):
                message = "Seules les requêtes en lecture (SELECT) sont autorisées"
            raise QueryError(message) from exc

        return {
            "columns": columns,
            "rows": [[_json_value(value) for value in row] for row in rows[:max_rows]],
            "row_count": min(len(rows), max_rows),
            "truncated": len(rows) > max_rows,
            "elapsed_ms": round((time_module.monotonic() - started) * 1000, 1),
        }
    finally:
        conn.close()


def describe_schema(path: Optional[Path] = None) -> List[dict]:
    """Liste les tables et vues de la base avec leurs colonnes."""
    conn = _connect_readonly(Path(path) if path else default_db_path())
    try:
        objects = conn.execute(
            """
            SELECT name, type FROM sqlite_master
            WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        return [
            {
                "name": name,
                "type": kind,
                "columns": [
                    {"name": col[1], "type": col[2]}
                    for col in conn.execute(f'PRAGMA table_info("{name}")')
                ],
            }
            for name, kind in objects
        ]
    finally:
        conn.close()

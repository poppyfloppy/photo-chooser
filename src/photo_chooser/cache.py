"""SQLite-кэш: локальные признаки, оценки обеих облачных ступеней, сборки и учёт расходов."""

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

from photo_chooser.models import Judgment, LocalFeatures, Screening

DEFAULT_CACHE_DIR = Path("~/Library/Caches/photo-chooser").expanduser()
DEFAULT_CACHE_PATH = DEFAULT_CACHE_DIR / "cache.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (uuid TEXT PRIMARY KEY, json TEXT NOT NULL, feature_print BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS screenings (
    uuid TEXT, themes_key TEXT, model TEXT, prompt_version TEXT, json TEXT NOT NULL,
    PRIMARY KEY (uuid, themes_key, model, prompt_version));
CREATE TABLE IF NOT EXISTS judgments (
    uuid TEXT, theme_key TEXT, model TEXT, prompt_version TEXT, json TEXT NOT NULL,
    PRIMARY KEY (uuid, theme_key, model, prompt_version));
CREATE TABLE IF NOT EXISTS compositions (
    theme_key TEXT, model TEXT, prompt_version TEXT, input_fp TEXT, json TEXT NOT NULL,
    PRIMARY KEY (theme_key, model, prompt_version, input_fp));
CREATE TABLE IF NOT EXISTS spend (album_uuid TEXT NOT NULL, model TEXT NOT NULL, usd REAL NOT NULL,
                                  created_at REAL NOT NULL);
"""



class Cache:
    def __init__(self, path: Path = DEFAULT_CACHE_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _write(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self._db.executemany(sql, rows)
            self._db.commit()

    def _read_one(self, sql: str, args: tuple):
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def get_features(self, uuid: str) -> LocalFeatures | None:
        row = self._read_one("SELECT json, feature_print FROM features WHERE uuid = ?", (uuid,))
        if row is None:
            return None
        return LocalFeatures(**json.loads(row[0]), feature_print=bytes(row[1]))

    def put_features(self, f: LocalFeatures) -> None:
        data = asdict(f)
        fp = data.pop("feature_print")
        self._write("INSERT OR REPLACE INTO features VALUES (?, ?, ?)", [(f.uuid, json.dumps(data), fp)])

    def get_screening(self, uuid: str, themes_key: str, model: str, prompt_version: str) -> Screening | None:
        row = self._read_one("SELECT json FROM screenings WHERE uuid = ? AND themes_key = ? AND model = ? "
                             "AND prompt_version = ?", (uuid, themes_key, model, prompt_version))
        return Screening(**json.loads(row[0])) if row else None

    def put_screenings(self, items: list[Screening], themes_key: str, model: str, prompt_version: str) -> None:
        self._write("INSERT OR REPLACE INTO screenings VALUES (?, ?, ?, ?, ?)",
                    [(s.uuid, themes_key, model, prompt_version, json.dumps(asdict(s), ensure_ascii=False))
                     for s in items])

    def get_judgment(self, uuid: str, theme_key: str, model: str, prompt_version: str) -> Judgment | None:
        row = self._read_one("SELECT json FROM judgments WHERE uuid = ? AND theme_key = ? AND model = ? "
                             "AND prompt_version = ?", (uuid, theme_key, model, prompt_version))
        return Judgment(**json.loads(row[0])) if row else None

    def put_judgments(self, items: list[Judgment], theme_key: str, model: str, prompt_version: str) -> None:
        self._write("INSERT OR REPLACE INTO judgments VALUES (?, ?, ?, ?, ?)",
                    [(j.uuid, theme_key, model, prompt_version, json.dumps(asdict(j), ensure_ascii=False))
                     for j in items])

    def get_composition(self, theme_key: str, model: str, prompt_version: str, input_fp: str) -> str | None:
        row = self._read_one("SELECT json FROM compositions WHERE theme_key = ? AND model = ? AND prompt_version = ? "
                             "AND input_fp = ?", (theme_key, model, prompt_version, input_fp))
        return row[0] if row else None

    def put_composition(self, theme_key: str, model: str, prompt_version: str, input_fp: str, json_text: str) -> None:
        self._write("INSERT OR REPLACE INTO compositions VALUES (?, ?, ?, ?, ?)",
                    [(theme_key, model, prompt_version, input_fp, json_text)])

    def add_spend(self, album_uuid: str, model: str, usd: float) -> None:
        self._write("INSERT INTO spend VALUES (?, ?, ?, ?)", [(album_uuid, model, usd, time.time())])

    def album_spend(self, album_uuid: str) -> float:
        row = self._read_one("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE album_uuid = ?", (album_uuid,))
        return float(row[0])

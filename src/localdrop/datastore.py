"""Tiny JSON document store behind /api/data (one file per project)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

DATA_FILENAME = "localdrop-data.json"


class DataStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()

    # -- internal -------------------------------------------------------
    def _load(self) -> Any:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            return {}
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            # keep the broken file for the user instead of silently losing it
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
            except OSError:
                pass
            return {}

    def _save(self, doc: Any) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: readers never see half a file
        return len(text.encode("utf-8"))

    # -- public ---------------------------------------------------------
    def read(self) -> Any:
        with self._lock:
            return self._load()

    def replace(self, doc: Any) -> int:
        with self._lock:
            return self._save(doc)

    def merge(self, patch: dict) -> int:
        with self._lock:
            doc = self._load()
            if not isinstance(doc, dict):
                doc = {}
            doc.update(patch)
            return self._save(doc)

    def set_key(self, key: str, value: Any) -> int:
        with self._lock:
            doc = self._load()
            if not isinstance(doc, dict):
                doc = {}
            doc[key] = value
            return self._save(doc)

    def delete(self, key: str | None = None) -> int:
        with self._lock:
            if key is None:
                return self._save({})
            doc = self._load()
            if isinstance(doc, dict):
                doc.pop(key, None)
                return self._save(doc)
            return self._save({})

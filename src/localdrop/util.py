"""Small helpers shared by the core and the UI."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path


def human_size(num: float) -> str:
    n = float(num)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


def human_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02d}м"
    if m:
        return f"{m}м {s:02d}с"
    return f"{s}с"


def slugify(text: str, fallback: str = "site") -> str:
    text = re.sub(r"[^\w\-]+", "-", text.strip().lower(), flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    return text[:40] or fallback


def clock(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def data_dir() -> Path:
    """Persistent app storage.

    Priority: LOCALDROP_HOME -> FLET_APP_STORAGE_DATA (set by Flet on every
    platform, including Android) -> a sensible per-OS default.
    """
    for var in ("LOCALDROP_HOME", "FLET_APP_STORAGE_DATA"):
        value = os.environ.get(var)
        if value:
            path = Path(value)
            break
    else:
        if sys.platform.startswith("win"):
            path = Path(os.environ.get("APPDATA", str(Path.home()))) / "LocalDrop"
        else:
            path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "localdrop"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_settings() -> dict:
    import json
    defaults = {"keep_awake": True}
    try:
        data = json.loads((data_dir() / "settings.json").read_text("utf-8"))
        if isinstance(data, dict):
            defaults.update(data)
    except (OSError, ValueError):
        pass
    return defaults


def save_settings(settings: dict) -> None:
    import json
    try:
        (data_dir() / "settings.json").write_text(json.dumps(settings, ensure_ascii=False), "utf-8")
    except OSError:
        pass

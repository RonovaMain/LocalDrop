"""Project library: import .html / .zip / folder, find the entry point, persist metadata."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import socket
import tarfile
import tempfile
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analyzer import analyze
from .archives import (ImportErrorLD, SUPPORTED_TEXT, base_name, detect_kind, materialize, parse_mhtml,
                       parse_webarchive, safe_extract, safe_extract_tar, saved_page_dirs)
from .datastore import DATA_FILENAME
from .util import data_dir, slugify

COPY_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__MACOSX", ".DS_Store", "Thumbs.db")
DEV_SOURCE = re.compile(r"""<script[^>]+src=["'](?:\.?/)?src/[^"']+\.(?:tsx?|jsx|vue|svelte)["']""", re.I)
BUILD_DIRS = {"dist", "build", "out", "public_html", "www"}


@dataclass
class Project:
    id: str
    name: str
    entry: str | None = None          # path of the main html, relative to files/
    port: int = 8080
    spa: bool = True
    live_reload: bool = True
    cors: bool = True
    lan: bool = True                  # False -> listen on 127.0.0.1 only
    created: float = 0.0
    updated: float = 0.0
    source: str = ""
    stats: dict = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict) -> "Project":
        known = {k: v for k, v in d.items() if k in Project.__dataclass_fields__}
        return Project(**known)


@dataclass
class ImportResult:
    project: Project
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- entry point
def find_entry(files_dir: Path) -> tuple[str | None, list[str]]:
    """Return (entry relative path or None, warnings)."""
    warnings: list[str] = []
    indexes: list[Path] = []
    htmls: list[Path] = []
    for base, dirs, files in os.walk(files_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__MACOSX")]
        for f in files:
            low = f.lower()
            if low in ("index.html", "index.htm"):
                indexes.append(Path(base) / f)
            elif low.endswith((".html", ".htm")):
                htmls.append(Path(base) / f)

    def rel(p: Path) -> str:
        return p.relative_to(files_dir).as_posix()

    def is_source(p: Path) -> bool:
        try:
            return bool(DEV_SOURCE.search(p.read_text("utf-8", errors="ignore")[:65536]))
        except OSError:
            return False

    if indexes:
        scored = []
        for p in indexes:
            r = rel(p)
            depth = r.count("/")
            score = depth * 10
            if is_source(p):
                score += 100
            if depth and p.parent.name.lower() in BUILD_DIRS:
                score -= 5
            scored.append((score, len(r), r))
        scored.sort()
        best = scored[0][2]
        if is_source(files_dir / best):
            warnings.append("Похоже, это исходники (Vite/React/Vue). Нужна собранная версия из папки dist/build.")
        elif scored[0][2].count("/") > 0:
            warnings.append(f"Точка входа найдена в подпапке: {best}")
        return best, warnings
    if htmls:
        prefer = ("home.html", "main.html", "app.html", "start.html")
        htmls.sort(key=lambda p: (rel(p).count("/"), p.name.lower() not in prefer, rel(p).lower()))
        warnings.append(f"index.html не найден, взят {rel(htmls[0])}")
        return rel(htmls[0]), warnings
    warnings.append("В проекте нет HTML-файлов: будет показан список файлов.")
    return None, warnings


# ---------------------------------------------------------------- library
class Library:
    def __init__(self, base: Path | None = None):
        self.base = Path(base) if base else data_dir()
        self.projects_dir = self.base / "projects"
        self.tmp_dir = self.base / "tmp"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._clean_tmp()

    def _clean_tmp(self) -> None:
        for p in self.tmp_dir.iterdir():
            shutil.rmtree(p, ignore_errors=True)

    # -- paths
    def project_dir(self, p: Project | str) -> Path:
        return self.projects_dir / (p if isinstance(p, str) else p.id)

    def files_dir(self, p: Project | str) -> Path:
        return self.project_dir(p) / "files"

    def serve_root(self, p: Project) -> Path:
        files = self.files_dir(p)
        if p.entry and "/" in p.entry:
            return files / posixpath.dirname(p.entry)
        return files

    def entry_name(self, p: Project) -> str | None:
        return posixpath.basename(p.entry) if p.entry else None

    def data_file(self, p: Project) -> Path:
        return self.files_dir(p) / DATA_FILENAME

    # -- CRUD
    def list(self) -> list[Project]:
        out = []
        for d in self.projects_dir.iterdir():
            meta = d / "meta.json"
            if meta.is_file():
                try:
                    out.append(Project.from_dict(json.loads(meta.read_text("utf-8"))))
                except (ValueError, TypeError, OSError):
                    continue
        out.sort(key=lambda p: p.updated, reverse=True)
        return out

    def get(self, pid: str) -> Project | None:
        try:
            return Project.from_dict(json.loads((self.project_dir(pid) / "meta.json").read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, p: Project) -> None:
        p.updated = time.time()
        d = self.project_dir(p)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(asdict(p), ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, d / "meta.json")

    def delete(self, pid: str) -> None:
        shutil.rmtree(self.project_dir(pid), ignore_errors=True)

    def rescan(self, p: Project) -> Project:
        p.stats = analyze(str(self.serve_root(p))).to_dict()
        self.save(p)
        return p

    def set_entry(self, p: Project, entry: str | None) -> None:
        entry = (entry or "").strip().strip("/") or None
        if entry:
            target = (self.files_dir(p) / entry).resolve()
            if not target.is_file() or self.files_dir(p).resolve() not in target.parents:
                raise ImportErrorLD(f"Файл «{entry}» не найден в проекте.")
        p.entry = entry
        self.rescan(p)

    # -- ports
    def suggest_port(self, start: int = 8080) -> int:
        used = {p.port for p in self.list()}
        for port in range(start, start + 500):
            if port in used:
                continue
            s = socket.socket()
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                continue
            finally:
                s.close()
            return port
        return start

    # -- import
    def import_paths(self, paths: list[str], name: str | None = None) -> ImportResult:
        paths = [str(p) for p in paths if p]
        if not paths:
            raise ImportErrorLD("Ничего не выбрано.")
        for p in paths:
            if not os.path.exists(p):
                raise ImportErrorLD(f"Путь недоступен: {p}")
        work = Path(tempfile.mkdtemp(prefix="imp-", dir=self.tmp_dir))   # unpack in a temp dir first
        final: Path | None = None
        try:
            stage = work / "files"
            stage.mkdir()
            default_name = self._stage(paths, stage)
            entry, warnings = find_entry(stage)
            pid = f"{slugify(name or default_name)}-{uuid.uuid4().hex[:6]}"
            proj = Project(id=pid, name=(name or default_name or "Сайт").strip()[:60], entry=entry,
                           port=self.suggest_port(), created=time.time(), source=os.path.basename(paths[0]))
            final = self.project_dir(pid)
            final.mkdir(parents=True)
            shutil.move(str(stage), str(final / "files"))
            proj.stats = analyze(str(self.serve_root(proj))).to_dict()
            self.save(proj)
            return ImportResult(proj, warnings)
        except Exception as e:
            if final is not None:                       # never leave a half-imported project behind
                shutil.rmtree(final, ignore_errors=True)
            if isinstance(e, ImportErrorLD):
                raise
            if isinstance(e, zipfile.BadZipFile):
                raise ImportErrorLD("Файл повреждён или это не ZIP-архив.") from e
            if isinstance(e, (tarfile.TarError, EOFError)):
                raise ImportErrorLD("Архив повреждён или не поддерживается.") from e
            if isinstance(e, PermissionError):
                raise ImportErrorLD("Нет доступа к выбранным файлам. Для папок на Android лучше использовать ZIP.") from e
            if isinstance(e, OSError):
                raise ImportErrorLD(f"Ошибка импорта: {e}") from e
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _stage(self, paths: list[str], stage: Path) -> str:
        if len(paths) == 1:
            p = Path(paths[0])
            kind = detect_kind(p)
            if kind == "dir":
                shutil.copytree(p, stage, dirs_exist_ok=True, ignore=COPY_IGNORE)
            elif kind == "zip":
                with zipfile.ZipFile(p) as zf:
                    if safe_extract(zf, stage) == 0:
                        raise ImportErrorLD("Архив пуст.")
            elif kind == "tar":
                if safe_extract_tar(p, stage) == 0:
                    raise ImportErrorLD("Архив пуст.")
            elif kind == "mhtml":
                materialize(parse_mhtml(p), stage)
            elif kind == "webarchive":
                materialize(parse_webarchive(p), stage)
            elif kind == "html":
                shutil.copy2(p, stage / p.name)
                for folder in saved_page_dirs(p):        # "Save page as -> Web page, complete"
                    shutil.copytree(folder, stage / folder.name, dirs_exist_ok=True, ignore=COPY_IGNORE)
            else:
                raise ImportErrorLD(f"Не похоже на сайт. Поддерживается: {SUPPORTED_TEXT}.")
            return p.name if kind == "dir" else base_name(p)
        # several loose files (drag-and-drop of index.html + style.css + app.js ...)
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                shutil.copytree(p, stage / p.name, dirs_exist_ok=True, ignore=COPY_IGNORE)
            else:
                shutil.copy2(p, stage / p.name)
        return Path(paths[0]).parent.name or "Сайт"

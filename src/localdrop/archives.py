"""Importers for "sites as files": zip, tar(.gz/.bz2/.xz), MHTML, Safari .webarchive. Standard library only."""

from __future__ import annotations

import email
import email.policy
import html as htmllib
import plistlib
import posixpath
import re
import tarfile
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .mime import ext_for

MAX_BYTES = 2 * 1024 ** 3
MAX_FILES = 60000

HTML_EXT = {".html", ".htm", ".xhtml"}
TAR_EXT = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz")
STRIP_SUFFIXES = TAR_EXT + (".zip", ".mhtml", ".mht", ".webarchive", ".html", ".htm", ".xhtml")
PICKER_EXTENSIONS = ["zip", "html", "htm", "mhtml", "mht", "webarchive", "tar", "gz", "tgz", "bz2", "xz", "tbz2", "txz"]
SUPPORTED_TEXT = ".html, .zip, .tar.gz, .mhtml, .webarchive или папка"


class ImportErrorLD(Exception):
    """Import failed; the message is user-facing."""


# ---------------------------------------------------------------- shared safety helpers
def clean_parts(name: str) -> list[str] | None:
    """Relative path parts; [] = ignorable entry (junk), None = unsafe (zip-slip)."""
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or parts[0] == "__MACOSX" or parts[-1] in (".DS_Store", "Thumbs.db"):
        return []
    if name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return parts


class Budget:
    """Caps the real number of files/bytes written (headers of archives can lie)."""

    def __init__(self) -> None:
        self.total = 0
        self.files = 0

    def add_file(self) -> None:
        self.files += 1
        if self.files > MAX_FILES:
            raise ImportErrorLD(f"В архиве слишком много файлов (>{MAX_FILES}).")

    def add_bytes(self, n: int) -> None:
        self.total += n
        if self.total > MAX_BYTES:
            raise ImportErrorLD("Содержимое слишком большое после распаковки (>2 ГБ).")


def _copy_limited(src, out, budget: Budget) -> None:
    while True:
        chunk = src.read(256 * 1024)
        if not chunk:
            return
        budget.add_bytes(len(chunk))
        out.write(chunk)


def _inside(dest: Path, target: Path) -> bool:
    resolved = target.resolve()
    return resolved == dest or dest in resolved.parents


# ---------------------------------------------------------------- zip
def _fix_zip_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if info.flag_bits & 0x800 or name.isascii():
        return name
    try:  # zipfile decoded as cp437; recover what Windows archivers really wrote
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """Extract with zip-slip and zip-bomb protection. Returns number of files."""
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise ImportErrorLD(f"В архиве слишком много файлов (>{MAX_FILES}).")
    dest = dest.resolve()
    budget, count = Budget(), 0
    for info in infos:
        parts = clean_parts(_fix_zip_name(info))
        if not parts:
            continue
        target = dest.joinpath(*parts)
        if not _inside(dest, target):
            continue
        if info.is_dir() or info.filename.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        budget.add_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            _copy_limited(src, out, budget)
        count += 1
    return count


# ---------------------------------------------------------------- tar
def safe_extract_tar(path: Path, dest: Path) -> int:
    """tar / tar.gz / tar.bz2 / tar.xz. Only regular files and folders are created:
    symlinks, hardlinks, devices and fifos are skipped on purpose."""
    dest = dest.resolve()
    budget, count = Budget(), 0
    with tarfile.open(path, "r:*") as tf:
        for m in tf:
            parts = clean_parts(m.name)
            if not parts:
                continue
            target = dest.joinpath(*parts)
            if not _inside(dest, target):
                continue
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():
                continue
            budget.add_file()
            src = tf.extractfile(m)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with src, open(target, "wb") as out:
                _copy_limited(src, out, budget)
            count += 1
    return count


# ---------------------------------------------------------------- saved pages (MHTML / webarchive)
@dataclass
class Resource:
    url: str
    mime: str
    data: bytes
    charset: str | None = None
    cid: str = ""


def parse_mhtml(path: Path) -> list[Resource]:
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=email.policy.default)
        out: list[Resource] = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            data = part.get_payload(decode=True)
            if data is None:
                continue
            out.append(Resource(url=str(part.get("Content-Location") or "").strip(),
                                mime=part.get_content_type(), data=data, charset=part.get_content_charset(),
                                cid=str(part.get("Content-ID") or "").strip().strip("<>")))
    except (OSError, ValueError, LookupError) as e:
        raise ImportErrorLD("Не удалось прочитать MHTML-файл.") from e
    if not out:
        raise ImportErrorLD("В MHTML-файле нет содержимого.")
    return out


def parse_webarchive(path: Path) -> list[Resource]:
    try:
        with open(path, "rb") as f:
            plist = plistlib.load(f)
        def make(d: dict) -> Resource:
            return Resource(url=str(d.get("WebResourceURL", "")), mime=str(d.get("WebResourceMIMEType", "")),
                            data=bytes(d.get("WebResourceData", b"")), charset=d.get("WebResourceTextEncodingName"))
        return [make(plist["WebMainResource"])] + [make(d) for d in plist.get("WebSubresources", [])]
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException) as e:
        raise ImportErrorLD("Не удалось прочитать файл .webarchive.") from e


_REWRITE_TYPES = {"text/html", "application/xhtml+xml", "text/css"}
_ATTR = re.compile(r"""(\b(?:src|href|poster|data-src|action)\s*=\s*)(["'])(.*?)\2""", re.I | re.S)
_CSS_URL = re.compile(r"""(url\(\s*)(["']?)(.*?)\2(\s*\))""", re.I)
_IMPORT = re.compile(r"""(@import\s+)(["'])(.*?)\2""", re.I)
_BASE_TAG = re.compile(r"""<base\b[^>]*>""", re.I)
_BASE_HREF = re.compile(r"""<base\b[^>]*\bhref\s*=\s*["']([^"']+)["']""", re.I)


def _local_name(r: Resource, used: set[str]) -> str:
    base = posixpath.basename(urllib.parse.unquote(urllib.parse.urlsplit(r.url).path)) or "resource"
    base = re.sub(r"[^\w.\-]+", "_", base)[:80].strip("._") or "resource"
    if not posixpath.splitext(base)[1]:
        base += ext_for(r.mime)
    stem, ext = posixpath.splitext(base)
    name, n = f"res/{base}", 1
    while name.lower() in used:
        n += 1
        name = f"res/{stem}_{n}{ext}"
    used.add(name.lower())
    return name


def _rewrite(text: str, base: str, here: str, lookup: dict[str, str]) -> str:
    here_dir = posixpath.dirname(here) or "."

    def local(ref: str) -> str | None:
        ref = htmllib.unescape(ref.strip())
        if not ref or ref.startswith(("#", "data:", "javascript:", "mailto:", "about:")):
            return None
        key = ref if ref.startswith("cid:") else (urllib.parse.urljoin(base, ref) if base else ref)
        for k in (key, key.split("#")[0]):
            if k in lookup:
                return posixpath.relpath(lookup[k], here_dir)
        return None

    def attr(m):
        new = local(m.group(3))
        return f"{m.group(1)}{m.group(2)}{new}{m.group(2)}" if new else m.group(0)

    def css(m):
        new = local(m.group(3))
        return f"{m.group(1)}{m.group(2)}{new}{m.group(2)}{m.group(4)}" if new else m.group(0)

    def imp(m):
        new = local(m.group(3))
        return f"{m.group(1)}{m.group(2)}{new}{m.group(2)}" if new else m.group(0)

    text = _ATTR.sub(attr, text)
    text = _CSS_URL.sub(css, text)
    return _IMPORT.sub(imp, text)


def materialize(resources: list[Resource], dest: Path) -> int:
    """Write a saved page as a normal folder: index.html + res/<files>, references rewritten."""
    main = next((r for r in resources if r.mime in ("text/html", "application/xhtml+xml")), None)
    if main is None:
        raise ImportErrorLD("В сохранённой странице нет HTML.")
    used: set[str] = {"index.html"}
    paths = {id(main): "index.html"}
    for r in resources:
        if r is not main:
            paths[id(r)] = _local_name(r, used)
    lookup: dict[str, str] = {}
    for r in resources:
        if r.url:
            lookup.setdefault(r.url, paths[id(r)])
        if r.cid:
            lookup.setdefault("cid:" + r.cid, paths[id(r)])
    dest.mkdir(parents=True, exist_ok=True)
    budget = Budget()
    for r in resources:
        data, rel = r.data, paths[id(r)]
        if r.mime in _REWRITE_TYPES:
            try:
                text = data.decode(r.charset or "utf-8", "replace")
            except LookupError:
                text = data.decode("utf-8", "replace")
            base = r.url
            m = _BASE_HREF.search(text)
            if m and base:
                base = urllib.parse.urljoin(base, m.group(1))
            text = _rewrite(_BASE_TAG.sub("", text) if r is main else text, base, rel, lookup)
            data = text.encode("utf-8")   # the server always sends charset=utf-8
        budget.add_file()
        budget.add_bytes(len(data))
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return len(resources)


# ---------------------------------------------------------------- detection
def detect_kind(p: Path) -> str:
    """dir | html | zip | tar | mhtml | webarchive | unknown  (extension first, then content)."""
    if p.is_dir():
        return "dir"
    name, ext = p.name.lower(), p.suffix.lower()
    if ext in HTML_EXT:
        return "html"
    if ext == ".zip":
        return "zip"
    if name.endswith(TAR_EXT):
        return "tar"
    if ext in (".mhtml", ".mht"):
        return "mhtml"
    if ext == ".webarchive":
        return "webarchive"
    try:
        if zipfile.is_zipfile(p):
            return "zip"
        if tarfile.is_tarfile(p):
            return "tar"
        with open(p, "rb") as f:
            head = f.read(4096)
    except OSError:
        return "unknown"
    if head.startswith(b"bplist00"):
        return "webarchive"
    low = head.lower()
    if b"multipart/related" in low and b"mime-version" in low:
        return "mhtml"
    if re.match(rb"\s*(<!doctype html|<html)", head, re.I):
        return "html"
    return "unknown"


def base_name(p: Path) -> str:
    low = p.name.lower()
    for suf in sorted(STRIP_SUFFIXES, key=len, reverse=True):
        if low.endswith(suf) and len(p.name) > len(suf):
            return p.name[: -len(suf)]
    return p.stem or p.name


def saved_page_dirs(html: Path) -> list[Path]:
    """Folders a browser creates next to "Save page as -> Web page, complete"."""
    stem = html.stem
    names = (f"{stem}_files", f"{stem}.files", f"{stem}_файлы")
    return [html.with_name(n) for n in names if html.with_name(n).is_dir()]

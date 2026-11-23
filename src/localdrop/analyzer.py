"""Project parser: size, file counts by type, empty `href="#"` links, TODO comments."""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field

from .datastore import DATA_FILENAME

CATEGORY_BY_EXT = {
    **{e: "html" for e in (".html", ".htm")},
    **{e: "js" for e in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")},
    ".css": "css",
    **{e: "media" for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp",
                            ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".flac", ".mp4", ".m4v", ".webm",
                            ".ogv", ".mov")},
    **{e: "fonts" for e in (".woff", ".woff2", ".ttf", ".otf", ".eot")},
    ".json": "json", ".webmanifest": "json", ".wasm": "wasm",
}
CATEGORIES = ("html", "js", "css", "media", "fonts", "json", "wasm", "other")

SCAN_HREF_EXT = {".html", ".htm", ".js", ".mjs", ".jsx", ".tsx", ".vue", ".svelte"}
SCAN_TODO_EXT = {".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".vue", ".svelte"}
SKIP_DIRS = {".git", "__MACOSX"}
MAX_TEXT = 3 * 1024 * 1024
MAX_SAMPLES = 25

_HREF = re.compile(r"""\bhref\s*=\s*(["'])#\1""", re.I)
_TODO = re.compile(r"\bTODO\b")
_HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_BLOCK = re.compile(r"/\*(.*?)\*/", re.S)
_LINE = re.compile(r"(?<![:\w\"'`\\])//(.*)$", re.M)
_INLINE = re.compile(r"<(script|style)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)


@dataclass
class Stats:
    total_bytes: int = 0
    file_count: int = 0
    by_type: dict = field(default_factory=lambda: {c: {"count": 0, "bytes": 0} for c in CATEGORIES})
    empty_links: int = 0
    empty_link_files: list = field(default_factory=list)   # [[file, count], ...]
    todo_count: int = 0
    todo_samples: list = field(default_factory=list)       # [{"file","line","text"}, ...]
    biggest: list = field(default_factory=list)            # [[file, bytes], ...]
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _comment_spans(text: str, ext: str):
    """Yield (offset, comment_text) for every comment in a file."""
    def js_css(src: str, base: int, js: bool):
        for m in _BLOCK.finditer(src):
            yield base + m.start(1), m.group(1)
        if js:
            for m in _LINE.finditer(src):
                yield base + m.start(1), m.group(1)

    if ext == ".css":
        yield from js_css(text, 0, False)
    elif ext in (".html", ".htm", ".vue", ".svelte"):
        for m in _HTML_COMMENT.finditer(text):
            yield m.start(1), m.group(1)
        for m in _INLINE.finditer(text):
            yield from js_css(m.group(2), m.start(2), m.group(1).lower() == "script")
    else:
        yield from js_css(text, 0, True)


def analyze(root: str, max_seconds: float = 10.0) -> Stats:
    st = Stats()
    deadline = time.monotonic() + max_seconds
    sizes: list[tuple[int, str]] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        vendor = "node_modules" in base.replace("\\", "/").split("/")
        for name in files:
            if name == DATA_FILENAME:
                continue
            full = os.path.join(base, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            ext = os.path.splitext(name)[1].lower()
            cat = CATEGORY_BY_EXT.get(ext, "other")
            st.file_count += 1
            st.total_bytes += size
            st.by_type[cat]["count"] += 1
            st.by_type[cat]["bytes"] += size
            sizes.append((size, rel))
            if vendor or ".min." in name.lower() or size > MAX_TEXT:
                continue
            if ext not in SCAN_HREF_EXT and ext not in SCAN_TODO_EXT:
                continue
            if time.monotonic() > deadline:
                st.truncated = True
                continue
            try:
                with open(full, "rb") as f:
                    text = f.read(MAX_TEXT).decode("utf-8", errors="replace")
            except OSError:
                continue
            if ext in SCAN_HREF_EXT:
                n = len(_HREF.findall(text))
                if n:
                    st.empty_links += n
                    st.empty_link_files.append([rel, n])
            if ext in SCAN_TODO_EXT and "TODO" in text:
                for off, comment in _comment_spans(text, ext):
                    for m in _TODO.finditer(comment):
                        st.todo_count += 1
                        if len(st.todo_samples) < MAX_SAMPLES:
                            line = text.count("\n", 0, off + m.start()) + 1
                            snippet = comment[m.start():].splitlines()[0].strip()[:100]
                            st.todo_samples.append({"file": rel, "line": line, "text": snippet})
    st.empty_link_files.sort(key=lambda x: -x[1])
    st.empty_link_files = st.empty_link_files[:15]
    sizes.sort(reverse=True)
    st.biggest = [[rel, size] for size, rel in sizes[:5]]
    return st

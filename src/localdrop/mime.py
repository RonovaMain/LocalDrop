"""MIME types for everything a modern frontend project may reference."""

from __future__ import annotations

import mimetypes
import os

# Explicit table: system mimetypes databases differ a lot between Windows,
# Linux and Android and often miss .mjs / .wasm / fonts / webmanifest.
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".cjs": "text/javascript; charset=utf-8",
    ".jsx": "text/javascript; charset=utf-8",
    ".ts": "text/javascript; charset=utf-8",
    ".tsx": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
    ".wasm": "application/wasm",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".bin": "application/octet-stream",
}

TEXT_LIKE = {".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css",
             ".json", ".map", ".txt", ".md", ".csv", ".xml", ".svg", ".webmanifest"}


def guess_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _TYPES:
        return _TYPES[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def is_html(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".html", ".htm")

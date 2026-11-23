"""The HTTP server: static files, SPA fallback, CORS, Range, live reload, /api/data."""

from __future__ import annotations

import email.utils
import errno
import html
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import __version__
from .datastore import DataStore
from .livereload import CLIENT_JS, ChangeWatcher, inject
from .mime import guess_type, is_html

SYS = "/__localdrop/"
MAX_BODY = 10 * 1024 * 1024
INJECT_LIMIT = 20 * 1024 * 1024
CHUNK = 64 * 1024
_NET_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout)


class ServerStartError(Exception):
    """Raised with a message that is safe to show to the user."""


@dataclass
class LogEntry:
    ts: float
    method: str
    path: str
    status: int
    size: int
    ms: float
    client: str
    note: str = ""


class ServerStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = time.time()
        self.requests = 0
        self.bytes_out = 0
        self.errors = 0
        self.clients: set[str] = set()
        self.recent: deque[float] = deque(maxlen=200)

    def record(self, e: LogEntry) -> None:
        with self._lock:
            self.requests += 1
            self.bytes_out += e.size
            if e.status >= 400:
                self.errors += 1
            self.clients.add(e.client)
            self.recent.append(e.ts)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            rps = sum(1 for t in self.recent if now - t <= 10) / 10.0
            return {"uptime": now - self.started, "requests": self.requests, "bytes": self.bytes_out,
                    "errors": self.errors, "clients": len(self.clients), "rps": rps}


_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         "<title>{title}</title><style>body{{font:16px system-ui,sans-serif;background:#0b0f1a;color:#e6e9f2;"
         "margin:0;padding:24px;max-width:760px;margin-inline:auto}}a{{color:#8ab4ff;text-decoration:none}}"
         "li{{padding:6px 0;list-style:none;border-bottom:1px solid #1d2438}}h1{{font-size:20px}}"
         "small{{color:#7d869f}}</style>{body}")


class _Httpd(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    # On Windows SO_REUSEADDR lets two sockets own one port; keep it POSIX-only.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):  # noqa: D401 - silence resets
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalDrop/" + __version__
    sys_version = ""
    timeout = 30

    # ------------------------------------------------------------ plumbing
    def setup(self) -> None:
        super().setup()
        self.server.track(self.connection, True)

    def finish(self) -> None:
        self.server.track(self.connection, False)
        try:
            super().finish()
        except Exception:
            pass

    def log_message(self, *a) -> None:  # we do our own logging
        pass

    def log_error(self, *a) -> None:
        pass

    def send_response(self, code, message=None) -> None:
        self._status = int(code)
        super().send_response(code, message)

    def end_headers(self) -> None:
        if self.server.cors:
            h = self.headers
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", h.get("Access-Control-Request-Headers") or "*")
            self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges, ETag")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
            if h.get("Access-Control-Request-Private-Network"):
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self._headers_sent = True
        super().end_headers()

    # ------------------------------------------------------------ dispatch
    def do_GET(self): self._handle("GET")
    def do_HEAD(self): self._handle("HEAD")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_PATCH(self): self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")
    def do_OPTIONS(self): self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        self._t0 = time.perf_counter()
        self._status = 0
        self._sent = 0
        self._note = ""
        self._silent = False
        self._headers_sent = False
        try:
            if self.server.stopping.is_set():
                self.close_connection = True
                return
            self._route(method)
        except _NET_ERRORS:
            self.close_connection = True
            self._note = "обрыв соединения"
        except Exception as exc:  # never kill the handler thread
            self._note = f"{type(exc).__name__}: {exc}"
            self.close_connection = True
            if not self._headers_sent:
                try:
                    self._error(500, "Internal error")
                except Exception:
                    pass
        finally:
            if not self._silent:
                self.server.emit(LogEntry(time.time(), method, self.path, self._status, self._sent,
                                          (time.perf_counter() - self._t0) * 1000,
                                          self.client_address[0], self._note))

    def _route(self, method: str) -> None:
        parts = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parts.path) or "/"
        query = urllib.parse.parse_qs(parts.query)
        if method == "OPTIONS":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS")
            self.end_headers()
            return
        if path.startswith(SYS):
            self._silent = True
            return self._system(path[len(SYS):])
        if path == "/api/data" or path.startswith("/api/data/"):
            return self._data_api(method, path[len("/api/data"):].strip("/"), query)
        if method not in ("GET", "HEAD"):
            self.close_connection = True
            return self._error(405, "Method not allowed", allow="GET, HEAD, OPTIONS")
        self._static(method, path, parts.query)

    # ------------------------------------------------------------ system
    def _system(self, name: str) -> None:
        if name == "livereload.js":
            return self._bytes("GET", 200, CLIENT_JS.encode(), "text/javascript; charset=utf-8")
        if name == "info":
            s = self.server
            return self._json(200, {"app": "LocalDrop", "version": __version__, "name": s.name,
                                    "live_reload": s.live_reload, "spa": s.spa, "boot": s.boot_id})
        if name == "events" and self.server.live_reload:
            return self._events()
        self._error(404, "Not found")

    def _events(self) -> None:
        srv = self.server
        w = srv.watcher
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        srv.sse(+1)
        try:
            self.wfile.write(f"retry: 1500\nevent: hello\ndata: {srv.boot_id}\n\n".encode())
            last, beat = w.version, time.time()
            while not srv.stopping.is_set():
                ver, kind = w.wait(last, 1.0)
                if ver != last:
                    last = ver
                    self.wfile.write(f"event: {kind}\ndata: {ver}\n\n".encode())
                    beat = time.time()
                elif time.time() - beat > 15:
                    self.wfile.write(b": ping\n\n")
                    beat = time.time()
        finally:
            srv.sse(-1)

    # ------------------------------------------------------------ /api/data
    def _data_api(self, method: str, key: str, query: dict) -> None:
        store = self.server.store
        if method in ("GET", "HEAD"):
            doc = store.read()
            if key:
                if isinstance(doc, dict) and key in doc:
                    return self._json(200, doc[key], head=method == "HEAD")
                return self._json(404, {"error": "key not found"}, head=method == "HEAD")
            return self._json(200, doc, head=method == "HEAD")
        if method in ("POST", "PUT", "PATCH"):
            body = self._read_body()
            if body is None:
                return
            try:
                value = json.loads(body.decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "body is not valid JSON"})
            merge = method == "PATCH" or query.get("merge", [""])[0].lower() in ("1", "true", "yes")
            if key:
                size = store.set_key(key, value)
            elif merge and isinstance(value, dict):
                size = store.merge(value)
            else:
                size = store.replace(value)
            return self._json(200, {"ok": True, "bytes": size})
        if method == "DELETE":
            self._drain_small()
            size = store.delete(key or None)
            return self._json(200, {"ok": True, "bytes": size})
        self._error(405, "Method not allowed")

    def _read_body(self) -> bytes | None:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            self.close_connection = True
            self._json(411, {"error": "Content-Length required"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._json(400, {"error": "bad Content-Length"})
            return None
        if length > MAX_BODY:
            self.close_connection = True
            self._json(413, {"error": f"body larger than {MAX_BODY // (1024 * 1024)} MB"})
            return None
        return self.rfile.read(length) if length else b""

    def _drain_small(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= MAX_BODY:
            self.rfile.read(n)

    # ------------------------------------------------------------ static
    def _safe_join(self, urlpath: str) -> str | None:
        parts: list[str] = []
        for seg in urlpath.replace("\\", "/").split("/"):
            if seg in ("", "."):
                continue
            if seg == ".." or "\x00" in seg or (os.name == "nt" and ":" in seg):
                return None
            parts.append(seg)
        full = os.path.join(self.server.root, *parts) if parts else self.server.root
        real = os.path.realpath(full)
        try:
            if os.path.commonpath([real, self.server.root_real]) != self.server.root_real:
                return None
        except ValueError:
            return None
        return real

    def _entry_abs(self) -> str | None:
        e = self.server.entry
        if not e:
            return None
        p = os.path.join(self.server.root_real, e)
        return p if os.path.isfile(p) else None

    def _static(self, method: str, urlpath: str, rawquery: str) -> None:
        fs = self._safe_join(urlpath)
        if fs is None:
            return self._error(403, "Forbidden")
        if os.path.isdir(fs):
            if not urlpath.endswith("/"):
                return self._redirect(urlpath + "/" + (("?" + rawquery) if rawquery else ""))
            for name in ("index.html", "index.htm"):
                idx = os.path.join(fs, name)
                if os.path.isfile(idx):
                    return self._file(method, idx)
            if urlpath == "/" and self._entry_abs():
                return self._file(method, self._entry_abs())
            return self._listing(method, fs, urlpath)
        if os.path.isfile(fs):
            return self._file(method, fs)
        if not os.path.splitext(fs)[1]:  # clean URLs: /about -> about.html
            for suffix in (".html", ".htm"):
                if os.path.isfile(fs + suffix):
                    return self._file(method, fs + suffix)
        if self.server.spa and not urlpath.startswith("/api/") and self._wants_html(urlpath):
            entry = self._entry_abs()
            if entry:
                return self._file(method, entry)
        page404 = os.path.join(self.server.root_real, "404.html")
        if os.path.isfile(page404):
            return self._file(method, page404, status=404)
        self._error(404, "Not found")

    def _wants_html(self, urlpath: str) -> bool:
        return "text/html" in (self.headers.get("Accept") or "") or not os.path.splitext(urlpath)[1]

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", urllib.parse.quote(location, safe="/?=&%:@+,;~"))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_modified(self, etag: str, mtime: float) -> bool:
        inm = self.headers.get("If-None-Match")
        if inm:
            return etag in [t.strip() for t in inm.split(",")] or inm.strip() == "*"
        ims = self.headers.get("If-Modified-Since")
        if ims:
            try:
                return int(mtime) <= int(email.utils.parsedate_to_datetime(ims).timestamp())
            except (TypeError, ValueError):
                return False
        return False

    def _range(self, size: int):
        h = self.headers.get("Range")
        if not h or not h.startswith("bytes="):
            return None
        spec = h[6:].strip()
        m = re.fullmatch(r"(\d*)-(\d*)", spec)
        if not m or ("," in spec) or (m.group(1) == "" and m.group(2) == ""):
            return None
        a, b = m.groups()
        if a == "":
            n = int(b)
            if n == 0:
                return "bad"
            return max(size - n, 0), size - 1
        start = int(a)
        end = int(b) if b else size - 1
        if start >= size:
            return "bad"
        end = min(end, size - 1)
        return None if end < start else (start, end)

    def _file(self, method: str, path: str, status: int = 200) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return self._error(404, "Not found")
        ctype = guess_type(path)
        inject_js = self.server.live_reload and is_html(path) and st.st_size <= INJECT_LIMIT
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}{"-lr" if inject_js else ""}"'
        lastmod = email.utils.formatdate(st.st_mtime, usegmt=True)
        common = {"ETag": etag, "Last-Modified": lastmod, "Cache-Control": "no-cache"}
        if status == 200 and self._not_modified(etag, st.st_mtime):
            self.send_response(304)
            for k, v in common.items():
                self.send_header(k, v)
            self.end_headers()
            return
        if inject_js:
            with open(path, "rb") as f:
                data = inject(f.read())
            return self._bytes(method, status, data, ctype, common)
        size, start, end, code = st.st_size, 0, st.st_size - 1, status
        rng = self._range(size) if status == 200 else None
        if rng == "bad":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng:
            start, end = rng
            code = 206
        length = end - start + 1 if size else 0
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        for k, v in common.items():
            self.send_header(k, v)
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if method == "HEAD" or not length:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    self.close_connection = True  # file shrank under us
                    break
                self.wfile.write(chunk)
                self._sent += len(chunk)
                remaining -= len(chunk)

    # ------------------------------------------------------------ responses
    def _bytes(self, method: str, status: int, data: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)
            self._sent += len(data)

    def _json(self, status: int, obj, head: bool = False) -> None:
        self._bytes("HEAD" if head else "GET", status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _error(self, status: int, msg: str, allow: str | None = None) -> None:
        wants_json = (self.path or "").startswith("/api/")
        extra = {"Allow": allow} if allow else None
        if wants_json:
            body = json.dumps({"error": msg}).encode()
            return self._bytes("GET", status, body, "application/json; charset=utf-8", extra)
        page = _PAGE.format(title=f"{status}", body=f"<h1>{status} — {html.escape(msg)}</h1>"
                            f"<p><small>LocalDrop · {html.escape(self.path[:200])}</small></p>")
        self._bytes("GET", status, page.encode(), "text/html; charset=utf-8", extra)

    def _listing(self, method: str, fs: str, urlpath: str) -> None:
        try:
            names = sorted(os.listdir(fs), key=lambda n: (not os.path.isdir(os.path.join(fs, n)), n.lower()))
        except OSError:
            return self._error(403, "Forbidden")
        rows = []
        if urlpath != "/":
            rows.append("<li><a href='../'>⬑ ..</a></li>")
        for n in names[:2000]:
            d = os.path.isdir(os.path.join(fs, n))
            href = urllib.parse.quote(n) + ("/" if d else "")
            rows.append(f"<li><a href='{href}'>{'📁 ' if d else ''}{html.escape(n)}{'/' if d else ''}</a></li>")
        body = f"<h1>Index of {html.escape(urlpath)}</h1><ul style='padding:0'>{''.join(rows)}</ul>"
        self._bytes(method, 200, _PAGE.format(title="Index", body=body).encode(), "text/html; charset=utf-8")


class SiteServer:
    """One running site. Thread-safe; `on_log` is called from worker threads."""

    def __init__(self, root: str, entry: str | None, port: int, *, name: str = "", host: str = "0.0.0.0",
                 spa: bool = True, live_reload: bool = True, cors: bool = True,
                 data_file: str | None = None, on_log: Callable[[LogEntry], None] | None = None):
        self.root = os.path.abspath(root)
        self.entry = entry
        self.port = port
        self.name = name
        self.host = host
        self.spa = spa
        self.live_reload = live_reload
        self.cors = cors
        self.stats = ServerStats()
        self.store = DataStore(data_file or os.path.join(self.root, "localdrop-data.json"))
        self.on_log = on_log
        self._httpd: _Httpd | None = None
        self._thread: threading.Thread | None = None
        self._watcher: ChangeWatcher | None = None
        self._sse = 0

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def sse_clients(self) -> int:
        return self._sse

    def start(self) -> None:
        if self._httpd:
            return
        if not os.path.isdir(self.root):
            raise ServerStartError("Папка проекта не найдена")
        try:
            httpd = _Httpd((self.host, self.port), Handler)
        except OSError as e:
            code = e.errno
            if code in (errno.EADDRINUSE, 10048):
                raise ServerStartError(f"Порт {self.port} уже занят. Выберите другой порт.") from e
            if code in (errno.EACCES, 10013):
                raise ServerStartError(f"Нет прав на порт {self.port}. Используйте порт от 1024.") from e
            raise ServerStartError(f"Не удалось запустить сервер: {e}") from e
        self.port = httpd.server_address[1]
        self.stats = ServerStats()
        self._watcher = ChangeWatcher(self.root)
        # attributes the handler reads
        httpd.root, httpd.root_real = self.root, os.path.realpath(self.root)
        httpd.entry, httpd.spa, httpd.cors = self.entry, self.spa, self.cors
        httpd.live_reload, httpd.name, httpd.store = self.live_reload, self.name, self.store
        httpd.watcher, httpd.boot_id = self._watcher, uuid.uuid4().hex[:8]
        httpd.stopping = threading.Event()
        httpd._conns, httpd._conn_lock = set(), threading.Lock()
        httpd.track = self._track
        httpd.emit = self._emit
        httpd.sse = self._count_sse
        self._httpd = httpd
        if self.live_reload:
            self._watcher.start()
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3},
                                        name=f"ld-http-{self.port}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if not httpd:
            return
        httpd.stopping.set()
        if self._watcher:
            self._watcher.stop()
        httpd.shutdown()
        httpd.server_close()
        # keep-alive connections would otherwise keep answering after "Stop"
        with httpd._conn_lock:
            conns = list(httpd._conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    # -- callbacks used by handlers
    def _track(self, conn, add: bool) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        with httpd._conn_lock:
            (httpd._conns.add if add else httpd._conns.discard)(conn)

    def _emit(self, entry: LogEntry) -> None:
        self.stats.record(entry)
        if self.on_log:
            try:
                self.on_log(entry)
            except Exception:
                pass

    def _count_sse(self, delta: int) -> None:
        self._sse = max(0, self._sse + delta)

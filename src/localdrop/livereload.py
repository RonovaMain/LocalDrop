"""File watcher (polling, works on Android/Windows/Linux alike) + browser client."""

from __future__ import annotations

import os
import re
import threading

from .datastore import DATA_FILENAME

IGNORE_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".idea", ".vscode"}
IGNORE_FILES = {DATA_FILENAME, DATA_FILENAME + ".tmp", DATA_FILENAME + ".corrupt", ".DS_Store"}
IGNORE_SUFFIXES = (".tmp", ".swp", ".part", "~")
MAX_FILES = 30000

SNIPPET = b'<script src="/__localdrop/livereload.js"></script>'

CLIENT_JS = r"""(function () {
  if (window.__localdropLR) return;
  window.__localdropLR = true;
  var boot = null;
  function refreshCss() {
    var stamp = Date.now();
    document.querySelectorAll('link[rel~="stylesheet"]').forEach(function (link) {
      try {
        var url = new URL(link.href, location.href);
        if (url.host !== location.host) return;
        url.searchParams.set('__ld', stamp);
        var fresh = link.cloneNode();
        fresh.href = url.href;
        fresh.onload = function () { link.remove(); };
        link.parentNode.insertBefore(fresh, link.nextSibling);
      } catch (e) {}
    });
  }
  var es = new EventSource('/__localdrop/events');
  es.addEventListener('hello', function (e) {
    if (boot && boot !== e.data) { location.reload(); return; }
    boot = e.data;
  });
  es.addEventListener('reload', function () { location.reload(); });
  es.addEventListener('css', refreshCss);
})();
"""

_BODY_END = re.compile(rb"</body\s*>", re.I)


def inject(data: bytes) -> bytes:
    matches = list(_BODY_END.finditer(data))
    if matches:
        i = matches[-1].start()
        return data[:i] + SNIPPET + data[i:]
    return data + SNIPPET


def _scan(root: str) -> dict[str, tuple[int, int]]:
    snap: dict[str, tuple[int, int]] = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for name in files:
            if name in IGNORE_FILES or name.endswith(IGNORE_SUFFIXES):
                continue
            full = os.path.join(base, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            snap[os.path.relpath(full, root).replace(os.sep, "/")] = (st.st_mtime_ns, st.st_size)
            if len(snap) >= MAX_FILES:
                return snap
    return snap


class ChangeWatcher:
    def __init__(self, root: str, interval: float = 0.8):
        self.root = root
        self.interval = interval
        self.version = 0
        self.kind = "reload"
        self.changed: list[str] = []
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ld-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def wait(self, since: int, timeout: float) -> tuple[int, str]:
        with self._cond:
            if self.version == since:
                self._cond.wait(timeout)
            return self.version, self.kind

    def _loop(self) -> None:
        old = _scan(self.root)
        while not self._stop.wait(self.interval):
            new = _scan(self.root)
            if new == old:
                continue
            # debounce: an unzip or a "save all" touches many files at once
            if self._stop.wait(0.25):
                return
            new = _scan(self.root)
            changed = sorted({p for p in set(old) | set(new) if old.get(p) != new.get(p)})
            old = new
            if not changed:
                continue
            kind = "css" if all(p.lower().endswith(".css") for p in changed) else "reload"
            with self._cond:
                self.version += 1
                self.kind = kind
                self.changed = changed
                self._cond.notify_all()

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localdrop.analyzer import analyze  # noqa: E402
from localdrop.projects import ImportErrorLD, Library, find_entry, safe_extract  # noqa: E402
from localdrop.server import ServerStartError, SiteServer  # noqa: E402


def make_site(root: Path):
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><html><body><a href="#">x</a><!-- TODO: fix -->'
        '<script type="module" src="/assets/app.mjs"></script></body></html>', "utf-8")
    (root / "assets" / "app.mjs").write_text("// TODO later\nexport const a = 1; // see http://x.y/z\n", "utf-8")
    (root / "assets" / "style.css").write_text("/* TODO: theme */ body{color:red}", "utf-8")
    (root / "assets" / "m.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (root / "assets" / "f.woff2").write_bytes(b"wOF2" + b"0" * 100)
    (root / "assets" / "video.mp4").write_bytes(bytes(range(256)) * 40)
    (root / "about.html").write_text("<html><body>about</body></html>", "utf-8")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "site"
        make_site(self.root)
        self.logs = []
        self.srv = SiteServer(str(self.root), "index.html", 0, host="127.0.0.1", on_log=self.logs.append)
        self.srv.start()

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_index_injected_and_cors(self):
        r, data = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"/__localdrop/livereload.js", data)
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "*")

    def test_mime_types(self):
        for path, ctype in [("/assets/app.mjs", "text/javascript"), ("/assets/style.css", "text/css"),
                            ("/assets/m.wasm", "application/wasm"), ("/assets/f.woff2", "font/woff2")]:
            r, _ = self.req("GET", path)
            self.assertEqual(r.status, 200, path)
            self.assertTrue(r.getheader("Content-Type").startswith(ctype), path)

    def test_spa_fallback_and_404(self):
        r, data = self.req("GET", "/some/route", headers={"Accept": "text/html"})
        self.assertEqual(r.status, 200)
        self.assertIn(b"<html", data)
        r, _ = self.req("GET", "/missing.js")
        self.assertEqual(r.status, 404)
        r, data = self.req("GET", "/about")  # clean url
        self.assertEqual(r.status, 200)
        self.assertIn(b"about", data)

    def test_range(self):
        r, data = self.req("GET", "/assets/video.mp4", headers={"Range": "bytes=10-19"})
        self.assertEqual(r.status, 206)
        self.assertEqual(data, bytes(range(10, 20)))
        self.assertEqual(r.getheader("Content-Range"), "bytes 10-19/10240")
        r, data = self.req("GET", "/assets/video.mp4", headers={"Range": "bytes=-5"})
        self.assertEqual(len(data), 5)
        r, _ = self.req("GET", "/assets/video.mp4", headers={"Range": "bytes=99999-"})
        self.assertEqual(r.status, 416)

    def test_etag_304(self):
        r, _ = self.req("GET", "/assets/style.css")
        r2, _ = self.req("GET", "/assets/style.css", headers={"If-None-Match": r.getheader("ETag")})
        self.assertEqual(r2.status, 304)

    def test_traversal_blocked(self):
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("nope")
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
            r, data = self.req("GET", path)
            self.assertNotEqual(data, b"nope", path)
            self.assertIn(r.status, (403, 404, 301, 200), path)
            self.assertNotIn(b"nope", data)

    def test_preflight(self):
        r, _ = self.req("OPTIONS", "/api/data", headers={"Origin": "http://x", "Access-Control-Request-Headers": "content-type",
                                                          "Access-Control-Request-Method": "POST"})
        self.assertEqual(r.status, 204)
        self.assertEqual(r.getheader("Access-Control-Allow-Headers"), "content-type")

    def test_api_data(self):
        r, data = self.req("GET", "/api/data")
        self.assertEqual(json.loads(data), {})
        r, _ = self.req("POST", "/api/data", body=json.dumps({"todos": ["a"], "n": 1}),
                        headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 200)
        r, data = self.req("GET", "/api/data")
        self.assertEqual(json.loads(data), {"todos": ["a"], "n": 1})
        self.req("POST", "/api/data/theme", body='"dark"')
        r, data = self.req("GET", "/api/data/theme")
        self.assertEqual(json.loads(data), "dark")
        self.req("PATCH", "/api/data", body='{"n": 2}')
        r, data = self.req("GET", "/api/data")
        self.assertEqual(json.loads(data), {"todos": ["a"], "n": 2, "theme": "dark"})
        r, _ = self.req("POST", "/api/data", body="{bad")
        self.assertEqual(r.status, 400)
        self.assertTrue((self.root / "localdrop-data.json").is_file())
        self.req("DELETE", "/api/data")
        r, data = self.req("GET", "/api/data")
        self.assertEqual(json.loads(data), {})

    def test_post_to_static_405(self):
        r, _ = self.req("POST", "/index.html", body="x")
        self.assertEqual(r.status, 405)

    def test_logging(self):
        self.req("GET", "/assets/style.css")
        self.req("GET", "/nope.png")
        time.sleep(0.1)
        statuses = [e.status for e in self.logs]
        self.assertIn(200, statuses)
        self.assertIn(404, statuses)
        snap = self.srv.stats.snapshot()
        self.assertGreaterEqual(snap["requests"], 2)
        self.assertGreaterEqual(snap["errors"], 1)

    def test_live_reload_events(self):
        got = []

        def listen():
            c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=8)
            c.request("GET", "/__localdrop/events")
            r = c.getresponse()
            buf = b""
            end = time.time() + 6
            while time.time() < end:
                line = r.fp.readline()
                if not line:
                    break
                buf += line
                if b"event: reload" in buf or b"event: css" in buf:
                    break
            got.append(buf)
            c.close()

        t = threading.Thread(target=listen)
        t.start()
        time.sleep(1.2)
        (self.root / "new.html").write_text("<p>hi</p>")
        t.join(8)
        self.assertTrue(got and b"event: hello" in got[0])
        self.assertIn(b"event: reload", got[0])

    def test_css_only_change_sends_css(self):
        got = []

        def listen():
            c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=8)
            c.request("GET", "/__localdrop/events")
            r = c.getresponse()
            buf = b""
            end = time.time() + 6
            while time.time() < end:
                line = r.fp.readline()
                if not line:
                    break
                buf += line
                if b"event: css" in buf or b"event: reload" in buf:
                    break
            got.append(buf)
            c.close()

        t = threading.Thread(target=listen)
        t.start()
        time.sleep(1.2)
        (self.root / "assets" / "style.css").write_text("body{color:blue}")
        t.join(8)
        self.assertIn(b"event: css", got[0])

    def test_stop_closes_keepalive(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=3)
        c.request("GET", "/assets/style.css")
        c.getresponse().read()
        port = self.srv.port
        self.srv.stop()
        with self.assertRaises(Exception):
            c.request("GET", "/assets/style.css")
            c.getresponse().read()
        c.close()
        s2 = SiteServer(str(self.root), "index.html", port, host="127.0.0.1")
        s2.start()  # port is free again immediately
        s2.stop()

    def test_port_in_use(self):
        s2 = SiteServer(str(self.root), "index.html", self.srv.port, host="127.0.0.1")
        with self.assertRaises(ServerStartError):
            s2.start()


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.lib = Library(self.base / "lib")

    def tearDown(self):
        self.tmp.cleanup()

    def zip_of(self, files: dict, name="site.zip") -> str:
        p = self.base / name
        with zipfile.ZipFile(p, "w") as zf:
            for k, v in files.items():
                zf.writestr(k, v)
        return str(p)

    def test_zip_with_wrapper_folder(self):
        z = self.zip_of({"mysite/index.html": "<h1>x</h1>", "mysite/css/a.css": "a{}", "__MACOSX/._x": "junk"})
        res = self.lib.import_paths([z])
        p = res.project
        self.assertEqual(p.entry, "mysite/index.html")
        self.assertEqual(self.lib.serve_root(p), self.lib.files_dir(p) / "mysite")
        self.assertEqual(p.stats["file_count"], 2)
        self.assertFalse((self.lib.files_dir(p) / "__MACOSX").exists())
        self.assertEqual(self.lib.get(p.id).name, p.name)

    def test_zip_slip_skipped(self):
        z = self.zip_of({"index.html": "ok", "../evil.txt": "x", "a/../../evil2.txt": "y", "/abs.txt": "z"})
        res = self.lib.import_paths([z])
        self.assertFalse((self.base / "evil.txt").exists())
        self.assertFalse((self.base / "lib" / "evil.txt").exists())
        self.assertFalse((self.base / "lib" / "projects" / "evil2.txt").exists())
        self.assertEqual(res.project.stats["file_count"], 1)

    def test_prefers_dist_over_vite_source(self):
        z = self.zip_of({"index.html": '<script type="module" src="/src/main.tsx"></script>',
                         "dist/index.html": "<h1>built</h1>", "dist/assets/a.js": "1"})
        res = self.lib.import_paths([z])
        self.assertEqual(res.project.entry, "dist/index.html")

    def test_source_only_warns(self):
        z = self.zip_of({"index.html": '<script type="module" src="/src/main.jsx"></script>'})
        res = self.lib.import_paths([z])
        self.assertTrue(any("исходники" in w for w in res.warnings))

    def test_single_html_and_loose_files(self):
        h = self.base / "page.html"
        h.write_text("<p>hi</p>")
        res = self.lib.import_paths([str(h)])
        self.assertEqual(res.project.entry, "page.html")
        css = self.base / "s.css"
        css.write_text("a{}")
        res2 = self.lib.import_paths([str(h), str(css)])
        self.assertEqual(res2.project.stats["file_count"], 2)

    def test_folder_import_skips_node_modules(self):
        d = self.base / "proj"
        (d / "node_modules" / "x").mkdir(parents=True)
        (d / "node_modules" / "x" / "a.js").write_text("1")
        (d / "index.html").write_text("<p>1</p>")
        res = self.lib.import_paths([str(d)])
        self.assertEqual(res.project.stats["file_count"], 1)

    def test_bad_zip_and_missing(self):
        bad = self.base / "bad.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(ImportErrorLD):
            self.lib.import_paths([str(bad)])

    def test_set_entry(self):
        z = self.zip_of({"index.html": "a", "other.html": "b"})
        p = self.lib.import_paths([z]).project
        self.lib.set_entry(p, "other.html")
        self.assertEqual(self.lib.get(p.id).entry, "other.html")
        with self.assertRaises(ImportErrorLD):
            self.lib.set_entry(p, "../../etc/passwd")

    def test_unique_ports(self):
        a = self.lib.import_paths([self.zip_of({"index.html": "a"}, "a.zip")]).project
        b = self.lib.import_paths([self.zip_of({"index.html": "b"}, "b.zip")]).project
        self.assertNotEqual(a.port, b.port)

    def test_cp866_names(self):
        # Windows Explorer on a Russian system stores names in cp866 without the UTF-8 flag
        p = self.base / "ru.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("AAAAA/AAAA.html", "<p>ok</p>")
        raw = p.read_bytes().replace(b"AAAAA/AAAA.html", "Папка/файл.html".encode("cp866"))
        p.write_bytes(raw)
        res = self.lib.import_paths([str(p)])
        self.assertEqual(res.project.entry, "Папка/файл.html")


class AnalyzerTests(unittest.TestCase):
    def test_counts(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_site(root)
            (root / "localdrop-data.json").write_text("{}")
            s = analyze(str(root))
            self.assertEqual(s.file_count, 7)
            self.assertEqual(s.by_type["html"]["count"], 2)
            self.assertEqual(s.by_type["js"]["count"], 1)
            self.assertEqual(s.by_type["css"]["count"], 1)
            self.assertEqual(s.by_type["media"]["count"], 1)
            self.assertEqual(s.by_type["fonts"]["count"], 1)
            self.assertEqual(s.by_type["wasm"]["count"], 1)
            self.assertEqual(s.empty_links, 1)
            self.assertEqual(s.todo_count, 3)  # html comment, // in mjs, /* */ in css
            self.assertTrue(all("line" in x for x in s.todo_samples))


if __name__ == "__main__":
    unittest.main()

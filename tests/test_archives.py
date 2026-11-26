import io
import plistlib
import sys
import tarfile
import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localdrop.archives import (base_name, clean_parts, detect_kind, materialize, parse_mhtml,  # noqa: E402
                                parse_webarchive)
from localdrop.projects import ImportErrorLD, Library  # noqa: E402

HTML = ('<!doctype html><html><head><base href="https://example.com/app/">'
        '<link rel="stylesheet" href="css/site.css"><link rel="icon" href="cid:ico@x"></head>'
        '<body><img src="https://example.com/img/logo.png"><a href="#">x</a>'
        '<script src="//example.com/app/main.js"></script></body></html>')
CSS = "body{background:url('../../img/bg.png')} @import 'other.css';"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def make_mhtml() -> bytes:
    msg = EmailMessage()
    msg["MIME-Version"] = "1.0"
    msg.set_content("x")
    msg.make_related()
    msg.clear_content() if False else None
    msg.get_payload()[0].set_content(HTML, subtype="html")
    msg.get_payload()[0]["Content-Location"] = "https://example.com/app/index.html"
    for url, ctype, data, cid in [
        ("https://example.com/app/css/site.css", "text/css", CSS.encode(), None),
        ("https://example.com/app/css/other.css", "text/css", b"p{}", None),
        ("https://example.com/img/logo.png", "image/png", PNG, None),
        ("https://example.com/img/bg.png", "image/png", PNG + b"1", None),
        ("https://example.com/app/main.js", "text/javascript", b"console.log(1)", None),
        ("https://example.com/favicon", "image/x-icon", b"ICO", "<ico@x>"),
    ]:
        maintype, subtype = ctype.split("/")
        msg.add_related(data, maintype=maintype, subtype=subtype, cid=cid) if cid else msg.add_related(
            data, maintype=maintype, subtype=subtype)
        part = msg.get_payload()[-1]
        part["Content-Location"] = url
    return msg.as_bytes()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.lib = Library(self.base / "lib")

    def tearDown(self):
        self.tmp.cleanup()

    def files(self, project):
        root = self.lib.files_dir(project)
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class TarTests(Base):
    def make_tar(self, mode, name, members):
        p = self.base / name
        with tarfile.open(p, mode) as tf:
            for k, v in members.items():
                if v is None:
                    info = tarfile.TarInfo(k); info.type = tarfile.DIRTYPE; tf.addfile(info)
                elif isinstance(v, tuple):          # symlink
                    info = tarfile.TarInfo(k); info.type = tarfile.SYMTYPE; info.linkname = v[0]; tf.addfile(info)
                else:
                    info = tarfile.TarInfo(k); info.size = len(v); tf.addfile(info, io.BytesIO(v))
        return str(p)

    def test_all_tar_flavours(self):
        for mode, name in [("w", "a.tar"), ("w:gz", "a.tar.gz"), ("w:gz", "a.tgz"), ("w:bz2", "a.tar.bz2"), ("w:xz", "a.tar.xz")]:
            z = self.make_tar(mode, name, {"site/index.html": b"<h1>x</h1>", "site/css/a.css": b"a{}"})
            res = self.lib.import_paths([z])
            self.assertEqual(res.project.entry, "site/index.html", name)
            self.assertEqual(res.project.stats["file_count"], 2, name)
            self.assertEqual(res.project.name, "a", name)

    def test_unsafe_members_skipped(self):
        z = self.make_tar("w:gz", "evil.tar.gz", {"index.html": b"ok", "../evil.txt": b"x", "/abs.txt": b"y",
                                                  "a/../../evil2.txt": b"z", "link": ("/etc/passwd",)})
        res = self.lib.import_paths([z])
        self.assertEqual(self.files(res.project), ["index.html"])
        self.assertFalse((self.base / "evil.txt").exists())

    def test_sniff_without_extension(self):
        z = self.make_tar("w:gz", "noext", {"index.html": b"hi"})
        self.assertEqual(detect_kind(Path(z)), "tar")
        self.assertEqual(self.lib.import_paths([z]).project.entry, "index.html")

    def test_corrupt_tar(self):
        bad = self.base / "bad.tar.gz"
        bad.write_bytes(b"\x1f\x8b" + b"garbage" * 10)
        with self.assertRaises(ImportErrorLD):
            self.lib.import_paths([str(bad)])
        self.assertEqual(self.lib.list(), [])


class SavedPageTests(Base):
    def check_materialized(self, project):
        files = self.files(project)
        self.assertIn("index.html", files)
        self.assertTrue(any(f.startswith("res/") and f.endswith(".css") for f in files))
        html = (self.lib.files_dir(project) / "index.html").read_text("utf-8")
        self.assertNotIn("<base", html)
        self.assertNotIn("example.com", html)
        self.assertNotIn("cid:", html)
        self.assertIn('href="#"', html)                      # in-page anchors untouched
        css_name = next(f for f in files if f.endswith("site.css"))
        self.assertIn(f'href="{css_name}"', html)
        css = (self.lib.files_dir(project) / css_name).read_text("utf-8")
        self.assertNotIn("img/bg", css)
        self.assertIn("url('bg.png')", css)                   # css and image live in the same res/ folder
        self.assertIn("@import 'other.css'", css)

    def test_mhtml(self):
        p = self.base / "page.mhtml"
        p.write_bytes(make_mhtml())
        self.assertEqual(detect_kind(p), "mhtml")
        res = self.lib.import_paths([str(p)])
        self.assertEqual(res.project.entry, "index.html")
        self.check_materialized(res.project)
        self.assertEqual(res.project.name, "page")

    def test_mhtml_detected_by_content(self):
        p = self.base / "saved.txt"
        p.write_bytes(make_mhtml())
        self.assertEqual(detect_kind(p), "mhtml")

    def test_webarchive(self):
        def res(url, mime, data):
            return {"WebResourceURL": url, "WebResourceMIMEType": mime, "WebResourceData": data,
                    "WebResourceTextEncodingName": "UTF-8"}
        plist = {"WebMainResource": res("https://example.com/app/index.html", "text/html", HTML.encode()),
                 "WebSubresources": [res("https://example.com/app/css/site.css", "text/css", CSS.encode()),
                                     res("https://example.com/app/css/other.css", "text/css", b"p{}"),
                                     res("https://example.com/img/logo.png", "image/png", PNG),
                                     res("https://example.com/img/bg.png", "image/png", PNG + b"1"),
                                     res("https://example.com/app/main.js", "text/javascript", b"1")]}
        p = self.base / "Safari.webarchive"
        p.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_BINARY))
        self.assertEqual(detect_kind(p), "webarchive")
        project = self.lib.import_paths([str(p)]).project
        html = (self.lib.files_dir(project) / "index.html").read_text("utf-8")
        self.assertNotIn("example.com/img", html)
        self.assertTrue(any(f.endswith("logo.png") for f in self.files(project)))

    def test_non_utf8_page_is_reencoded(self):
        res = [__import__("localdrop.archives", fromlist=["Resource"]).Resource(
            url="http://a/", mime="text/html", data="<p>Привет</p>".encode("cp1251"), charset="windows-1251")]
        dest = self.base / "out"
        materialize(res, dest)
        self.assertIn("Привет", (dest / "index.html").read_text("utf-8"))

    def test_saved_page_with_files_folder(self):
        src = self.base / "src"
        (src / "My page_files").mkdir(parents=True)
        (src / "My page.html").write_text('<img src="My page_files/a.png">')
        (src / "My page_files" / "a.png").write_bytes(PNG)
        (src / "Other_files").mkdir()
        (src / "Other_files" / "x.png").write_bytes(PNG)
        project = self.lib.import_paths([str(src / "My page.html")]).project
        self.assertEqual(self.files(project), ["My page.html", "My page_files/a.png"])
        self.assertEqual(project.name, "My page")

    def test_garbage_mhtml(self):
        bad = self.base / "x.mhtml"
        bad.write_bytes(b"\x00\x01\x02 not mhtml")
        with self.assertRaises(ImportErrorLD):
            self.lib.import_paths([str(bad)])
        self.assertEqual(self.lib.list(), [])


class MiscTests(Base):
    def test_unknown_single_file_rejected_and_nothing_left(self):
        f = self.base / "photo.png"
        f.write_bytes(PNG)
        with self.assertRaises(ImportErrorLD):
            self.lib.import_paths([str(f)])
        self.assertEqual(self.lib.list(), [])
        self.assertEqual(list((self.base / "lib" / "projects").iterdir()), [])

    def test_failed_import_leaves_no_project_dir(self):
        z = self.base / "bad.zip"
        z.write_bytes(b"PK\x03\x04broken")
        with self.assertRaises(ImportErrorLD):
            self.lib.import_paths([str(z)])
        self.assertEqual(list((self.base / "lib" / "projects").iterdir()), [])

    def test_helpers(self):
        self.assertEqual(clean_parts("a/b.txt"), ["a", "b.txt"])
        self.assertIsNone(clean_parts("../x"))
        self.assertIsNone(clean_parts("C:/x"))
        self.assertEqual(clean_parts("__MACOSX/x"), [])
        for name, expect in [("site.tar.gz", "site"), ("A.TGZ", "A"), ("p.mhtml", "p"), ("s.webarchive", "s"), ("x.zip", "x")]:
            self.assertEqual(base_name(Path(name)), expect)

    def test_parse_helpers_exposed(self):
        self.assertTrue(callable(parse_mhtml) and callable(parse_webarchive))


if __name__ == "__main__":
    unittest.main()

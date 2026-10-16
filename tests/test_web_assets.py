from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cater import web, web_assets
from cater.runtime import Runtime
from cater.web_assets import MANIFEST_NAME, WebAssetsError, asset_path, capture_web_assets


def write_manifest(root: Path, manifest: dict) -> None:
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


def write_bundle(root: Path, version: str = "A") -> dict[str, bytes]:
    files = {
        "/index.html": ("<!doctype html><html><head>"
                        '<script type="module" src="/assets/main.js"></script>'
                        '<link rel="stylesheet" href="./assets/main.css?build=1">'
                        '<link rel="modulepreload" href="/assets/lazy.js">'
                        '<link rel="icon" href="/favicon.svg">'
                        f"</head><body>{version}</body></html>").encode(),
        "/favicon.svg": f"<svg><!--{version}--></svg>".encode(),
        "/assets/main.js": f"import('./lazy.js'); /* {version} */".encode(),
        "/assets/main.css": f"/* {version} */ body {{color: green}}".encode(),
        "/assets/lazy.js": f"export default '{version}'".encode(),
        "/assets/nested/image.jpg": b"\xff\xd8" + version.encode(),
    }
    for name, body in files.items():
        path = root / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    write_manifest(root, {
        "index.html": {"file": "assets/main.js", "src": "index.html", "isEntry": True,
                       "css": ["assets/main.css"], "dynamicImports": ["src/lazy.ts"]},
        "src/lazy.ts": {"file": "assets/lazy.js", "src": "src/lazy.ts", "isDynamicEntry": True,
                        "assets": ["assets/nested/image.jpg"]},
        "src/image.jpg": {"file": "assets/nested/image.jpg", "src": "src/image.jpg"},
    })
    return files


class WebAssetSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "dist"
        self.files = write_bundle(self.root)

    def test_only_selected_files_are_captured_and_mapping_is_read_only(self):
        (self.root / "private.txt").write_bytes(b"not public")
        (self.root / ".vite").mkdir()
        (self.root / ".vite" / "manifest.json").write_bytes(b"{}")
        snapshot = capture_web_assets(self.root)
        self.assertEqual(dict(snapshot.files), self.files)
        with self.assertRaises(TypeError):
            snapshot.files["/new"] = b"no"

    def test_version_is_reproducible_and_covers_names_and_every_file_body(self):
        first = capture_web_assets(self.root)
        independent = hashlib.sha256()
        all_files = {**self.files, "/" + MANIFEST_NAME: (self.root / MANIFEST_NAME).read_bytes()}
        for name, body in sorted(all_files.items()):
            encoded = name.encode("utf-8")
            independent.update(len(encoded).to_bytes(4, "big"))
            independent.update(encoded)
            independent.update(len(body).to_bytes(8, "big"))
            independent.update(body)
        self.assertEqual(first.version, independent.hexdigest())
        self.assertEqual(first.version, capture_web_assets(self.root).version)
        for name in self.files:
            with self.subTest(name=name):
                path = self.root / name.lstrip("/")
                path.write_bytes(self.files[name] + b" ")
                self.assertNotEqual(first.version, capture_web_assets(self.root).version)
                path.write_bytes(self.files[name])
        source = self.root / "assets/nested/image.jpg"
        source.rename(source.with_name("other.jpg"))
        manifest = json.loads((self.root / MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["src/lazy.ts"]["assets"] = ["assets/nested/other.jpg"]
        manifest["src/image.jpg"]["file"] = "assets/nested/other.jpg"
        write_manifest(self.root, manifest)
        self.assertNotEqual(first.version, capture_web_assets(self.root).version)

    def test_images_larger_than_one_mib_and_exact_file_limit_are_supported(self):
        path = self.root / "assets/nested/image.jpg"
        for size in (1024 * 1024 + 1, web_assets.MAX_FILE_BYTES):
            with self.subTest(size=size):
                body = b"x" * size
                path.write_bytes(body)
                self.assertEqual(capture_web_assets(self.root).files["/assets/nested/image.jpg"], body)

    def test_missing_source_and_partial_entry_resources_fail_before_any_runtime_action(self):
        for name in (*self.files, "/" + MANIFEST_NAME):
            with self.subTest(name=name):
                path = self.root / name.lstrip("/")
                original = path.read_bytes()
                path.unlink()
                factory = Mock(side_effect=AssertionError("runtime opened before capture"))
                with self.assertRaises(WebAssetsError), patch.object(web.ActivityRunner, "recover_interrupted") as recover:
                    web.make_handler(self.base / "unused.db", static_dir=self.root, runtime_factory=factory)
                factory.assert_not_called()
                recover.assert_not_called()
                self.assertFalse((self.base / "unused.db").exists())
                path.write_bytes(original)
        with self.assertRaises(WebAssetsError):
            capture_web_assets(self.base / "missing")

    def test_optional_assets_and_favicon_can_be_absent_without_entry_references(self):
        shutil.rmtree(self.root)
        self.root.mkdir()
        (self.root / "index.html").write_bytes(b"<main>legacy fixture</main>")
        self.assertEqual(set(capture_web_assets(self.root).files), {"/index.html"})

    def test_entry_urls_are_checked_locally_without_external_requests(self):
        references = ("https://example.invalid/main.js", "//example.invalid/main.js", "file:///x.js",
                      "data:text/javascript,1", "/assets/missing.js", "/assets/../private.js",
                      "/assets/%2e%2e/main.js", "/assets/%252e%252e/main.js", "/assets\\main.js", "")
        for reference in references:
            for template in ('<script src="{}"></script>', '<link rel="stylesheet" href="{}">'):
                with self.subTest(reference=reference, template=template):
                    (self.root / "index.html").write_text(template.format(reference), encoding="utf-8")
                    with patch("urllib.request.urlopen", side_effect=AssertionError("external request")), \
                            self.assertRaises(WebAssetsError):
                        capture_web_assets(self.root)
        (self.root / "index.html").write_text('<base href="/other/">', encoding="utf-8")
        with self.assertRaises(WebAssetsError):
            capture_web_assets(self.root)
        for markup in ('<script src="/assets/missing.js" src="/assets/main.js"></script>',
                       '<link rel="stylesheet" href="/assets/missing.css" href="/assets/main.css">'):
            with self.subTest(markup=markup):
                (self.root / "index.html").write_text(markup, encoding="utf-8")
                with self.assertRaisesRegex(WebAssetsError, "ambiguous"):
                    capture_web_assets(self.root)
        (self.root / "index.html").write_bytes(b"\xff")
        with self.assertRaises(WebAssetsError):
            capture_web_assets(self.root)

    def test_unsafe_url_paths_are_rejected_without_normalizing(self):
        for path in ("/assets/../index.html", "/assets/%2e%2e/index.html", "/assets/%252e%252e/a",
                     "/assets/%2Fmain.js", "/assets/%5cmain.js", "/assets/a%00", "/assets/a%ff",
                     "/assets/a%", "/assets//main.js", "/assets/main.js/", "/assets/C:secret",
                     "/assets/CON", "/assets/a.", "/assets/a ", "/private.txt", "assets/main.js"):
            with self.subTest(path=path):
                self.assertIsNone(asset_path(path))
        self.assertEqual(asset_path("/assets/%6dain.js"), "/assets/main.js")

    def test_root_traversal_and_unsafe_asset_names_are_rejected(self):
        with self.assertRaises(WebAssetsError):
            capture_web_assets(self.root / ".." / "dist")
        for name in ("encoded%20.js", "fragment#.js"):
            path = self.root / "assets" / name
            path.write_bytes(b"bad")
            with self.subTest(name=name), self.assertRaises(WebAssetsError):
                capture_web_assets(self.root)
            path.unlink()

    def test_file_count_limit_includes_index_and_favicon(self):
        remaining = web_assets.MAX_FILES - len(self.files) - 1
        for number in range(remaining):
            (self.root / "assets" / f"extra-{number}.txt").write_bytes(b"")
        self.assertEqual(len(capture_web_assets(self.root).files) + 1, web_assets.MAX_FILES)
        (self.root / "assets" / "one-too-many.txt").write_bytes(b"")
        with self.assertRaisesRegex(WebAssetsError, "file count"):
            capture_web_assets(self.root)

    def test_file_size_limit_rejects_before_reading(self):
        with (self.root / "assets/nested/image.jpg").open("wb") as stream:
            stream.truncate(web_assets.MAX_FILE_BYTES + 1)
        with patch.object(web_assets, "_read_asset") as read, self.assertRaisesRegex(WebAssetsError, "file exceeds"):
            capture_web_assets(self.root)
        read.assert_not_called()

    def test_total_byte_limit_is_exact_and_rejects_before_reading(self):
        current = sum(map(len, self.files.values())) + (self.root / MANIFEST_NAME).stat().st_size
        for name, size in (("large-a.bin", web_assets.MAX_FILE_BYTES),
                           ("large-b.bin", web_assets.MAX_TOTAL_BYTES - current - web_assets.MAX_FILE_BYTES)):
            with (self.root / "assets" / name).open("wb") as stream:
                stream.truncate(size)
        snapshot = capture_web_assets(self.root)
        self.assertEqual(sum(map(len, snapshot.files.values())) + (self.root / MANIFEST_NAME).stat().st_size,
                         web_assets.MAX_TOTAL_BYTES)
        (self.root / "assets" / "extra.bin").write_bytes(b"x")
        with patch.object(web_assets, "_read_asset") as read, self.assertRaisesRegex(WebAssetsError, "total byte"):
            capture_web_assets(self.root)
        read.assert_not_called()

    def test_directory_entry_and_depth_limits_are_bounded(self):
        with patch.object(web_assets, "MAX_ENTRIES", len(self.files) + 3):
            (self.root / "assets/empty").mkdir()
            with self.assertRaisesRegex(WebAssetsError, "entry count"):
                capture_web_assets(self.root)
        with patch.object(web_assets, "MAX_DEPTH", 2):
            with self.assertRaisesRegex(WebAssetsError, "depth"):
                capture_web_assets(self.root)

    def test_hardlinks_are_rejected(self):
        os.link(self.root / "assets/main.js", self.base / "alias.js")
        with self.assertRaisesRegex(WebAssetsError, "hard-linked"):
            capture_web_assets(self.root)

    def test_real_symlink_file_is_rejected(self):
        link = self.root / "assets/link.js"
        try:
            link.symlink_to(self.root / "assets/main.js")
        except OSError as exc:
            self.skipTest(f"symlink privilege unavailable: {exc}")
        with self.assertRaisesRegex(WebAssetsError, "symlink|reparse"):
            capture_web_assets(self.root)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_real_junctions_at_root_ancestor_and_nested_directory_are_rejected(self):
        import _winapi
        for location in ("root", "ancestor", "nested"):
            with self.subTest(location=location):
                link = self.base / ("junction-" + location)
                _winapi.CreateJunction(str(self.root if location == "root" else self.base), str(link))
                try:
                    target = link if location == "root" else link / "dist"
                    if location == "nested":
                        os.rmdir(link)
                        link = self.root / "assets/junction"
                        _winapi.CreateJunction(str(self.base), str(link))
                        target = self.root
                    with self.assertRaisesRegex(WebAssetsError, "reparse"):
                        capture_web_assets(target)
                finally:
                    os.rmdir(link)

    def test_symlinks_reparse_points_and_special_files_are_rejected_without_privileges(self):
        original = Path.lstat
        target = self.root / "assets/main.js"
        for mode, attributes in ((stat.S_IFLNK | 0o600, 0), (stat.S_IFREG | 0o600, 0x400),
                                 (stat.S_IFIFO | 0o600, 0), (stat.S_IFSOCK | 0o600, 0)):
            def fake_lstat(path):
                if path == target:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_nlink=1)
                return original(path)
            with self.subTest(mode=mode, attributes=attributes), patch.object(Path, "lstat", fake_lstat), \
                    self.assertRaises(WebAssetsError):
                capture_web_assets(self.root)

    def test_changes_between_inventory_and_read_or_during_capture_fail_closed(self):
        original = web_assets._read_asset
        for mutation in ("same-size", "delete", "replace", "add", "directory", "already-read"):
            write_bundle(self.root)
            mutated = False

            def raced_read(path, expected):
                nonlocal mutated
                body = original(path, expected)
                if not mutated and (mutation != "already-read" or path.name == "index.html"):
                    mutated = True
                    target = self.root / "assets/main.js"
                    if mutation in {"same-size", "already-read"}:
                        target.write_bytes(b"x" * target.stat().st_size)
                    elif mutation == "delete":
                        target.unlink()
                    elif mutation == "replace":
                        replacement = self.root / "replacement.js"
                        replacement.write_bytes(target.read_bytes())
                        os.replace(replacement, target)
                    elif mutation == "add":
                        (self.root / "assets/new.js").write_bytes(b"new")
                    else:
                        (self.root / "assets/nested").rename(self.root / "assets/moved")
                return body

            with self.subTest(mutation=mutation), patch.object(web_assets, "_read_asset", raced_read), \
                    self.assertRaises(WebAssetsError):
                capture_web_assets(self.root)
            shutil.rmtree(self.root)

    def test_in_place_change_during_open_file_read_is_rejected(self):
        original = web_assets._open

        def raced_open(path, flags):
            fd = original(path, flags)
            if path.name == "main.js":
                with path.open("ab") as stream:
                    stream.write(b" changed")
            return fd

        with patch.object(web_assets, "_open", raced_open), self.assertRaisesRegex(WebAssetsError, "changed"):
            capture_web_assets(self.root)

    def test_file_mutation_inside_read_is_rejected(self):
        original = os.fdopen
        target = self.root / "assets/main.js"

        class RacingStream:
            def __init__(self, fd, *args, **kwargs):
                self.stream = original(fd, *args, **kwargs)
                self.race = os.path.samestat(os.fstat(fd), target.stat())

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def read(self, count):
                body = self.stream.read(count)
                if self.race:
                    with target.open("ab") as writer:
                        writer.write(b" grew during read")
                return body

        with patch.object(web_assets.os, "fdopen", RacingStream), self.assertRaisesRegex(WebAssetsError, "while reading"):
            capture_web_assets(self.root)

    def test_concurrent_directory_replacement_during_capture_fails(self):
        original = web_assets._read_asset
        started, release = threading.Event(), threading.Event()
        swapped = self.base / "old-dist"

        def paused_read(path, expected):
            if path.name == "lazy.js":
                started.set()
                if not release.wait(5):
                    raise AssertionError("replacement was not released")
            return original(path, expected)

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(web_assets, "_read_asset", paused_read):
            result = pool.submit(capture_web_assets, self.root)
            try:
                self.assertTrue(started.wait(5))
                self.root.rename(swapped)
                write_bundle(self.root, "B")
            finally:
                release.set()
            with self.assertRaises(WebAssetsError):
                result.result(timeout=5)

    def test_packaged_workroom_is_preferred_and_development_is_fallback(self):
        package = self.base / "package/cater"
        package.mkdir(parents=True)
        packaged = package / "static/workroom"
        development = package.parent / "web/dist"
        with patch.object(web, "__file__", str(package / "web.py")):
            self.assertIsNone(web._workroom_dist())
            write_bundle(development, "dev")
            self.assertEqual(web._workroom_dist(), development.resolve())
            write_bundle(packaged, "packaged")
            self.assertEqual(web._workroom_dist(), packaged.resolve())
            manifest = (packaged / MANIFEST_NAME).read_bytes()
            (packaged / MANIFEST_NAME).unlink()
            with self.assertRaisesRegex(WebAssetsError, "requires a build manifest"):
                capture_web_assets(web._workroom_dist())
            (packaged / MANIFEST_NAME).write_bytes(manifest)
            (packaged / "index.html").unlink()
            self.assertEqual(web._workroom_dist(), packaged.resolve())
            with self.assertRaises(WebAssetsError):
                capture_web_assets(web._workroom_dist())


class WebAssetManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "dist"
        self.files = write_bundle(self.root)
        self.manifest = json.loads((self.root / MANIFEST_NAME).read_text(encoding="utf-8"))

    def test_external_scripts_styles_and_preloads_require_manifest(self):
        (self.root / MANIFEST_NAME).unlink()
        for markup in ('<script src="/assets/main.js"></script>',
                       '<link rel="stylesheet" href="/assets/main.css">',
                       '<link rel="modulepreload" href="/assets/lazy.js">'):
            with self.subTest(markup=markup):
                (self.root / "index.html").write_text(markup, encoding="utf-8")
                with self.assertRaisesRegex(WebAssetsError, "requires a build manifest"):
                    capture_web_assets(self.root)
        (self.root / "index.html").write_text('<script>window.local = true</script>', encoding="utf-8")
        capture_web_assets(self.root)

    def test_manifest_json_must_be_utf8_unambiguous_and_bounded(self):
        for raw in (b"\xff", b"{", b"[]", b"null", b'{} trailing',
                    b'{"x":{},"x":{}}', b'{"x":{"file":"a","file":"b"}}',
                    b'{"x":{"file":NaN}}', b'{"x":{"file":Infinity}}',
                    b"[" * 2000 + b"0" + b"]" * 2000,
                    b" " * (web_assets.MAX_MANIFEST_BYTES + 1)):
            with self.subTest(prefix=raw[:60]):
                (self.root / MANIFEST_NAME).write_bytes(raw)
                with self.assertRaises(WebAssetsError):
                    capture_web_assets(self.root)

    def test_manifest_byte_limit_is_exact_and_rejected_before_reading(self):
        path = self.root / MANIFEST_NAME
        raw = path.read_bytes()
        path.write_bytes(raw + b" " * (web_assets.MAX_MANIFEST_BYTES - len(raw)))
        capture_web_assets(self.root)
        with path.open("ab") as stream:
            stream.write(b" ")
        with patch.object(web_assets, "_read_asset") as read, self.assertRaisesRegex(WebAssetsError, "manifest exceeds byte"):
            capture_web_assets(self.root)
        read.assert_not_called()

    def test_manifest_links_are_rejected_like_public_assets(self):
        path = self.root / MANIFEST_NAME
        alias = self.root.parent / "manifest-alias.json"
        os.link(path, alias)
        with self.assertRaisesRegex(WebAssetsError, "hard-linked"):
            capture_web_assets(self.root)
        alias.unlink()
        original = Path.lstat

        def linked_manifest(selected):
            if selected == path:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_file_attributes=0x400, st_nlink=1)
            return original(selected)

        with patch.object(Path, "lstat", linked_manifest), self.assertRaisesRegex(WebAssetsError, "reparse"):
            capture_web_assets(self.root)

    def test_manifest_schema_and_dependency_types_are_checked(self):
        changes = ({"file": None}, {"file": 1}, {"css": "assets/main.css"},
                   {"assets": [None]}, {"imports": [7]}, {"dynamicImports": {}},
                   {"isEntry": 1}, {"isDynamicEntry": "true"}, {"name": []}, {"src": {}},
                   {"unknown": True}, {"imports": ["missing"]}, {"dynamicImports": ["missing"]},
                   {"assets": ["assets/main.js"] * (web_assets.MAX_FILES + 1)})
        for change in changes:
            with self.subTest(change=change):
                write_manifest(self.root, {**self.manifest, "index.html": {**self.manifest["index.html"], **change}})
                with self.assertRaises(WebAssetsError):
                    capture_web_assets(self.root)
        for manifest in ({"x": []}, {"": {"file": "assets/main.js"}},
                         {str(i): {"file": "assets/main.js"} for i in range(web_assets.MAX_FILES + 1)}):
            write_manifest(self.root, manifest)
            with self.assertRaises(WebAssetsError):
                capture_web_assets(self.root)

    def test_manifest_cannot_expand_the_public_file_allowlist_or_decode_urls(self):
        (self.root / "private.txt").write_bytes(b"private root content")
        unsafe = ("private.txt", "index.html", MANIFEST_NAME, "favicon.svg", "/assets/main.js",
                  "https://example.invalid/a.js", "//example.invalid/a.js", "file:///a.js",
                  "assets/../private.txt", "assets/%2e%2e/private.txt", "assets/%6dain.js",
                  "assets/main.js?x=1", "assets/main.js#x", "assets\\main.js", "assets//main.js",
                  "assets/main.js/", "assets/CON", "assets/a\x00", "assets/" + "a/" * 32 + "x.js")
        original = web_assets._read_asset
        for field in ("file", "css", "assets"):
            for path in unsafe:
                with self.subTest(field=field, path=path):
                    chunk = {**self.manifest["index.html"], field: path if field == "file" else [path]}
                    write_manifest(self.root, {**self.manifest, "index.html": chunk})
                    with patch.object(web_assets, "_read_asset", wraps=original) as read, self.assertRaises(WebAssetsError):
                        capture_web_assets(self.root)
                    self.assertNotIn(self.root / "private.txt", [call.args[0] for call in read.call_args_list])

    def test_transitive_static_dynamic_css_and_assets_are_all_required(self):
        (self.root / "assets/shared.js").write_bytes(b"export const shared = 1")
        (self.root / "assets/deep.js").write_bytes(b"export default 2")
        (self.root / "assets/deep.css").write_bytes(b"body {}")
        self.manifest["index.html"]["imports"] = ["_shared.js"]
        self.manifest["src/lazy.ts"]["dynamicImports"] = ["src/deep.ts"]
        self.manifest["_shared.js"] = {"file": "assets/shared.js", "imports": ["index.html"]}
        self.manifest["src/deep.ts"] = {"file": "assets/deep.js", "imports": ["_shared.js"],
                                       "css": ["assets/deep.css"], "assets": ["assets/nested/image.jpg"]}
        write_manifest(self.root, self.manifest)
        capture_web_assets(self.root)  # Cycles and CSS loaded only by dynamic chunks are valid.
        for name in ("shared.js", "deep.js", "deep.css", "nested/image.jpg"):
            path = self.root / "assets" / name
            body = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaisesRegex(WebAssetsError, "missing resource"):
                capture_web_assets(self.root)
            if name == "deep.js":
                factory = Mock(side_effect=AssertionError("runtime opened before manifest validation"))
                with patch.object(web.ActivityRunner, "recover_interrupted") as recover, self.assertRaises(WebAssetsError):
                    web.make_handler(self.root.parent / "unused.db", static_dir=self.root, runtime_factory=factory)
                factory.assert_not_called()
                recover.assert_not_called()
                self.assertFalse((self.root.parent / "unused.db").exists())
            path.write_bytes(body)
        self.manifest["src/deep.ts"]["dynamicImports"] = ["nonexistent-source.ts"]
        write_manifest(self.root, self.manifest)
        with self.assertRaisesRegex(WebAssetsError, "missing chunk"):
            capture_web_assets(self.root)

    def test_even_unreferenced_manifest_entries_are_validated(self):
        for chunk in ({"file": "assets/missing.js"},
                      {"file": "assets/main.js", "imports": ["missing"]}):
            write_manifest(self.root, {**self.manifest, "unused.ts": chunk})
            with self.assertRaises(WebAssetsError):
                capture_web_assets(self.root)

    def test_html_scripts_styles_and_preloads_must_match_the_correct_entry(self):
        (self.root / "assets/unrelated.css").write_bytes(b"unrelated {}")
        (self.root / "assets/unrelated.js").write_bytes(b"export default 1")
        self.manifest["other.html"] = {"file": "assets/unrelated.js", "isEntry": True,
                                       "css": ["assets/unrelated.css"]}
        write_manifest(self.root, self.manifest)
        markup = self.files["/index.html"].decode()
        for changed in (markup.replace("/assets/main.js", "/assets/lazy.js"),
                        markup.replace("/assets/main.js", "/assets/unrelated.js"),
                        markup.replace("./assets/main.css?build=1", "/assets/unrelated.css"),
                        markup.replace('<link rel="stylesheet" href="./assets/main.css?build=1">', ""),
                        markup.replace("/assets/lazy.js", "/assets/unrelated.js")):
            (self.root / "index.html").write_text(changed, encoding="utf-8")
            with self.subTest(markup=changed), self.assertRaises(WebAssetsError):
                capture_web_assets(self.root)

    def test_static_import_css_and_css_only_entry_are_supported(self):
        self.manifest["index.html"].pop("css")
        self.manifest["index.html"]["imports"] = ["_shared.js"]
        self.manifest["_shared.js"] = {"file": "assets/lazy.js", "css": ["assets/main.css"]}
        write_manifest(self.root, self.manifest)
        capture_web_assets(self.root)
        (self.root / "index.html").write_text('<link rel="stylesheet" href="/assets/main.css">', encoding="utf-8")
        write_manifest(self.root, {"index.html": {"file": "assets/main.css", "isEntry": True}})
        capture_web_assets(self.root)

    def test_manifest_is_private_but_changes_the_version_and_is_race_checked(self):
        first = capture_web_assets(self.root)
        self.manifest["index.html"]["name"] = "private-source-metadata"
        write_manifest(self.root, self.manifest)
        second = capture_web_assets(self.root)
        self.assertEqual(first.files, second.files)
        self.assertNotEqual(first.version, second.version)
        self.assertNotIn("/" + MANIFEST_NAME, second.files)
        self.assertIsNone(asset_path("/" + MANIFEST_NAME))
        original = web_assets._read_asset

        def raced_read(path, expected):
            body = original(path, expected)
            if path.name == MANIFEST_NAME:
                path.write_bytes(body + b" ")
            return body

        with patch.object(web_assets, "_read_asset", raced_read), self.assertRaisesRegex(WebAssetsError, "changed"):
            capture_web_assets(self.root)


class WebAssetHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "dist"
        self.files = write_bundle(self.root)
        self.db = self.base / "test.db"

    def start_server(self, **options):
        handler = web.make_handler(self.db, static_dir=self.root, **options)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_same_handler_survives_source_replacement_and_deletion_second_handler_gets_new_bytes(self):
        server = self.start_server()
        first = self.request(server, "/")
        old = self.base / "old-dist"
        self.root.rename(old)
        changed = write_bundle(self.root, "B")
        (self.root / "assets/new.js").write_bytes(b"new module")
        second = self.start_server()
        second_version = self.request(second, "/")[1]["X-Cater-UI-Version"]
        self.assertNotEqual(first[1]["X-Cater-UI-Version"], second_version)
        for name, body in self.files.items():
            with self.subTest(name=name):
                self.assertEqual(self.request(server, name)[2], body)
                self.assertEqual(self.request(second, name)[2], changed[name])
        self.assertEqual(self.request(server, "/assets/new.js")[0], 404)
        self.assertEqual(self.request(second, "/assets/new.js")[2], b"new module")
        shutil.rmtree(old)
        shutil.rmtree(self.root)
        # Any accidental request-time filesystem access must fail this test.
        with patch.object(Path, "read_bytes", side_effect=AssertionError("live read")), \
                patch.object(web_assets, "_inventory", side_effect=AssertionError("live scan")):
            for name, body in self.files.items():
                self.assertEqual(self.request(server, name)[2], body)
                self.assertEqual(self.request(second, name)[2], changed[name])
        self.assertEqual(self.request(server, "/")[1]["X-Cater-UI-Version"], first[1]["X-Cater-UI-Version"])

    def test_concurrent_http_reads_keep_one_version_while_source_changes(self):
        server = self.start_server()
        version = self.request(server, "/")[1]["X-Cater-UI-Version"]
        gate = threading.Barrier(5)

        def read_many():
            gate.wait(timeout=5)
            for _ in range(5):
                for name, body in self.files.items():
                    status, headers, received = self.request(server, name)
                    self.assertEqual((status, received), (200, body))
                    self.assertEqual(headers["X-Cater-UI-Version"], version)

        with ThreadPoolExecutor(max_workers=4) as pool:
            readers = [pool.submit(read_many) for _ in range(4)]
            gate.wait(timeout=5)
            shutil.rmtree(self.root)
            write_bundle(self.root, "B")
            shutil.rmtree(self.root)
            for reader in readers:
                reader.result(timeout=10)

    def test_version_headers_cache_content_types_and_unknown_paths(self):
        server = self.start_server()
        version = capture_web_assets(self.root).version
        for name in ("/", *self.files):
            status, headers, body = self.request(server, name + "?v=ignored")
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Cater-UI-Version"], version)
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable"
                             if name.startswith("/assets/") else "no-cache")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("text/html", self.request(server, "/")[1]["Content-Type"])
        self.assertIn("image/jpeg", self.request(server, "/assets/nested/image.jpg")[1]["Content-Type"])
        self.assertEqual(self.request(server, "/assets/%6dain.js")[2], self.files["/assets/main.js"])
        for method in ("HEAD", "OPTIONS", "PUT"):
            with self.subTest(method=method):
                status, headers, _ = self.request(server, "/assets/main.js", method=method)
                self.assertEqual((status, headers["Cache-Control"]), (501, "no-store"))
        (self.root / "private.txt").write_bytes(b"secret")
        (self.root / "assets/new.js").write_bytes(b"not captured")
        for path in ("/assets/new.js", "/private.txt", "/missing", "/assets/../index.html",
                     "/assets/%2e%2e/index.html", "/assets/%252e%252e/index.html", "/assets/%2fmain.js",
                     "/assets/%5cmain.js", "/assets/main.js/", "/assets/main.js;bad", "/assets/a%00",
                     "/assets/a%ff", "/assets/a%", "/assets//main.js", "/index.html/", "/favicon.svg/"):
            with self.subTest(path=path):
                status, headers, body = self.request(server, path)
                self.assertEqual(status, 404)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("X-Cater-UI-Version", headers)
                self.assertEqual(body, b"not found")

    def test_ordinary_api_and_access_controls_remain_usable(self):
        factory = lambda: Runtime(self.db, drivers={})
        server = self.start_server(access_token="fixture-token", runtime_factory=factory)
        self.assertEqual(self.request(server, "/")[0], 200)
        status, headers, _ = self.request(server, "/api/tasks")
        self.assertEqual((status, headers["Cache-Control"]), (401, "no-store"))
        auth = {"X-Cater-Access-Token": "fixture-token", "Content-Type": "application/json"}
        status, headers, body = self.request(server, "/api/tasks", "POST", b'{"goal":"Snapshot API fixture"}', auth)
        self.assertEqual(status, 201)
        task = json.loads(body)
        self.assertNotIn("X-Cater-UI-Version", headers)
        status, _, body = self.request(server, "/api/tasks", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual([row["id"] for row in json.loads(body)], [task["task"]["id"]])
        status, headers, _ = self.request(server, "/api/tasks", "POST", b"not-json", auth)
        self.assertEqual((status, headers["Cache-Control"]), (400, "no-store"))
        status, headers, _ = self.request(server, "/api/tasks", "POST", b"{}", {**auth, "Origin": "https://foreign.invalid"})
        self.assertEqual((status, headers["Cache-Control"]), (403, "no-store"))
        status, headers, _ = self.request(server, "/api/missing", headers=auth)
        self.assertEqual((status, headers["Cache-Control"]), (404, "no-store"))

    def test_manifest_metadata_is_not_served_by_static_or_api_routes(self):
        manifest = json.loads((self.root / MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["index.html"]["name"] = "private-source-metadata"
        write_manifest(self.root, manifest)
        server = self.start_server(runtime_factory=lambda: Runtime(self.db, drivers={}))
        for path in ("/" + MANIFEST_NAME, "/%77orkroom-manifest.json", "/api/" + MANIFEST_NAME,
                     "/assets/../" + MANIFEST_NAME, "/assets/%2e%2e/" + MANIFEST_NAME):
            with self.subTest(path=path):
                status, headers, body = self.request(server, path)
                self.assertEqual((status, headers["Cache-Control"]), (404, "no-store"))
                self.assertNotIn(b"private-source-metadata", body)
        for path in ("/", "/api/tasks"):
            status, _, body = self.request(server, path)
            self.assertEqual(status, 200)
            self.assertNotIn(b"private-source-metadata", body)


if __name__ == "__main__":
    unittest.main()

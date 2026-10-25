"""Offline member previews: exact DB snapshots, bounded bytes and HTTP scope."""

import copy
from contextlib import closing
import hashlib
import http.client
import io
import json
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
import zipfile

from cater import archive_members, team_archive
from cater.artifact_blobs import put_blob
from cater.runtime import Runtime
from cater.workspace_archive import ArchiveError
import test_archive_members as member_fixtures
from test_runtime import start_test_web
import test_team_review_cycle as reviews


LIMIT = 65536
UNICODE_PATH = "docs/\u4e2d\u6587-\U0001f680.txt"
TEXT = "PUBLISHED_ONLY\n\u4e2d\u6587\U0001f680\t\r\n\f"


def expected_preview(artifact, path, body, kind="utf8"):
    entry = next(e for e in artifact["archive"]["files"] if e["path"] == path)
    return {"status": "ok", "artifact_seq": artifact["seq"],
            "archive_sha256": artifact["content_sha256"], "path": path,
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "executable": entry["executable"], "content_kind": kind,
            "content": body.decode("utf-8") if kind == "utf8" else None}


class ArchiveMemberPreviewTests(unittest.TestCase):
    def setUp(self):
        self.f = member_fixtures.ArchiveMembersTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def store(self, bodies, **options):
        blob, manifest = self.f.make_zip(bodies, **options)
        return {**self.f.store(blob, manifest), "seq": 17}

    def test_unicode_empty_binary_and_executable_identity(self):
        bodies = {UNICODE_PATH: TEXT.encode(), "empty": b"", "run": b"exit 0\n",
                  "binary": b"\x00\xff", "page.html": b'<script>alert("x")</script>'}
        artifact = self.store(bodies)
        before = copy.deepcopy(artifact)
        changes = self.f.log.db.total_changes
        for path, body in bodies.items():
            with self.subTest(path=path):
                self.assertEqual(team_archive.preview_member(self.f.log, artifact, path),
                                 expected_preview(artifact, path, body,
                                                  "binary" if path == "binary" else "utf8"))
        self.assertEqual(artifact, before)
        self.assertEqual(self.f.log.db.total_changes, changes)
        self.assertEqual(self.f.log.count(), 0)

    def test_strict_utf8_and_every_disallowed_c0_are_binary(self):
        invalid = [b"\xff", b"\xc0\xaf", b"\xe2\x82", b"\xed\xa0\x80",
                   b"\xf4\x90\x80\x80", b"\x80"]
        invalid += [b"before" + bytes([n]) + b"after" for n in range(32)
                    if n not in (9, 10, 12, 13)]
        bodies = {f"bad-{i:02}.txt": body for i, body in enumerate(invalid)}
        bodies["valid.txt"] = TEXT.encode() + b"\x7f"
        artifact = self.store(bodies)
        for path, body in bodies.items():
            with self.subTest(path=path, body=body):
                self.assertEqual(team_archive.preview_member(self.f.log, artifact, path),
                                 expected_preview(artifact, path, body,
                                                  "utf8" if path == "valid.txt" else "binary"))

    def test_exact_64kib_boundary_is_bytes_and_only_selected_body_is_read(self):
        self.assertEqual(team_archive.MEMBER_PREVIEW_BYTES, LIMIT)
        bodies = {"ascii": b"x" * LIMIT, "emoji": ("\U0001f680" * (LIMIT // 4)).encode(),
                  "chinese": ("\u4e2d" * (LIMIT // 3)).encode() + b"x",
                  "too-large": b"x" * (LIMIT + 1)}
        artifact = self.store(bodies, compression=zipfile.ZIP_DEFLATED)
        for path in ("ascii", "emoji", "chinese"):
            reads = []
            original = zipfile.ZipExtFile.read

            def read(stream, size=-1):
                reads.append((stream.name, size))
                return original(stream, size)

            with self.subTest(path=path), patch.object(zipfile.ZipExtFile, "read", new=read):
                self.assertEqual(len(bodies[path]), LIMIT)
                self.assertEqual(team_archive.preview_member(self.f.log, artifact, path),
                                 expected_preview(artifact, path, bodies[path]))
                self.assertEqual(reads, [(path, LIMIT + 1)])

    def test_oversize_and_unlisted_fail_before_blob_or_reader(self):
        artifact = self.store({"ascii": b"x" * (LIMIT + 1),
                               "emoji": ("\U0001f680" * (LIMIT // 4) + "x").encode()})
        for path, code in (("ascii", "too_large"), ("emoji", "too_large"),
                           ("missing", "not_found"), ("ASCII", "not_found")):
            with self.subTest(path=path), patch.object(archive_members, "read_blob") as blob, \
                    patch.object(archive_members, "read_members") as reader:
                with self.assertRaises(team_archive.MemberPreviewError) as caught:
                    team_archive.preview_member(self.f.log, artifact, path)
                self.assertEqual(caught.exception.code, code)
                blob.assert_not_called()
                reader.assert_not_called()

    def test_noncanonical_paths_and_invalid_scalars_fail_before_blob(self):
        artifact = self.store({"run": b"exit 0\n"})
        for path in (None, True, 1, [], {}, "", "../run", "a/../run", "./run", "/run",
                     "a//run", "run/", "a\\run", "C:/run", "run:stream", ".env",
                     "run\x00", "run\n", "\ud800", "\uff0e\uff0e/run"):
            with self.subTest(path=repr(path)), patch.object(archive_members, "read_blob") as blob:
                with self.assertRaises(ValueError):
                    team_archive.preview_member(self.f.log, artifact, path)
                blob.assert_not_called()

    def test_complete_manifest_is_validated_before_blob(self):
        artifact = self.store({"a": b"ok", "z": b"other"})
        for field, value in (("bytes", True), ("sha256", "bad"), ("executable", 1),
                             ("path", "../private")):
            invalid = copy.deepcopy(artifact)
            invalid["archive"]["files"][-1][field] = value
            with self.subTest(field=field), patch.object(archive_members, "read_blob") as blob:
                with self.assertRaises(ArchiveError):
                    team_archive.preview_member(self.f.log, invalid, "a")
                blob.assert_not_called()


class ArchiveMemberPreviewWebTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.bind_workspace()
        self.files = {UNICODE_PATH: TEXT.encode(), "empty.txt": b"", "binary.bin": b"\xff\x00",
                      "page.html": b'<script>alert("PUBLISHED_ONLY")</script>',
                      "docs/a+b &q='x'.txt": b"literal query punctuation",
                      "boundary.txt": ("\U0001f680" * (LIMIT // 4)).encode(),
                      "large.txt": ("\U0001f680" * (LIMIT // 4) + "x").encode()}
        for name, body in self.files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        published, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "filename": "project.zip", "summary": "Offline preview snapshot", "paths": list(self.files)}))
        self.assertEqual(published["status"], "ok", published)
        self.artifact, = self.f.rt.task(self.f.task).deliverables
        self.seq = published["artifact_seq"]
        self.blob = team_archive.artifact_bytes(self.f.rt.log, self.artifact)
        self.other = self.f.rt.create_task("Other fixture task")
        self.base = start_test_web(self, str(self.f.root / "test.db"),
            access_token="fixture-only-access", runtime_factory=lambda: Runtime(self.f.root / "test.db",
                roles_path=self.f.roles, drivers={"team_test": self.f.driver}, close_drivers_on_close=False))

    def get(self, *, path=UNICODE_PATH, seq=None, task=None, query=None,
            token="fixture-only-access", host=None, fragment="", status=200, download=False):
        target = f"/api/tasks/{task or self.f.task}/artifacts/{self.seq if seq is None else seq}"
        if not download:
            target += "/file?" + (urlencode({"path": path}) if query is None else query) + fragment
        connection = http.client.HTTPConnection("127.0.0.1", urlsplit(self.base).port, timeout=5)
        self.addCleanup(connection.close)
        headers = {"X-Cater-Access-Token": token} if token else {}
        if host:
            headers["Host"] = host
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        self.assertEqual(response.status, status, body)
        content_type = response.headers.get_content_type()
        if status == 200:
            self.assertEqual(content_type, "application/zip" if download else "application/json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(int(response.headers["Content-Length"]), len(body))
        if status != 200:
            for secret in ("PUBLISHED_ONLY", str(self.project), str(self.f.root), "INTERNAL_SECRET"):
                self.assertNotIn(secret, body.decode("utf-8"))
        return json.loads(body) if content_type == "application/json" else body

    def fault_publication(self, **changes):
        event, = self.f.events("deliverable.published")[:1]
        return self.f.rt.log.emit(type=event.type, actor=event.actor, task_id=self.f.task,
                                 payload={**copy.deepcopy(event.payload), **changes}).seq

    def test_json_identity_unicode_empty_binary_and_inert_injection_content(self):
        for path, body in self.files.items():
            if path == "large.txt":
                continue
            with self.subTest(path=path):
                self.assertEqual(self.get(path=path), expected_preview(
                    self.artifact, path, body, "binary" if path == "binary.bin" else "utf8"))

    def test_authentication_host_and_task_scope_apply_to_preview_and_download(self):
        for token in (None, "wrong"):
            with self.subTest(token=token):
                self.get(token=token, status=401)
                self.get(token=token, download=True, status=401)
                self.get(token=token, host="cater.attacker.example", status=401)
        self.assertEqual(self.get(host="localhost")["status"], "ok")
        self.get(task=self.other, status=404)
        self.get(task="missing-task", status=404)
        self.get(seq=99999999, status=404)
        self.get(seq=self.f.publish("plain text"), status=404)
        self.get(task=self.other, download=True, status=404)
        self.assertEqual(self.get(download=True), self.blob)
        self.assertEqual(hashlib.sha256(self.blob).hexdigest(), self.artifact["content_sha256"])

    def test_unauthenticated_loopback_server_rejects_foreign_host(self):
        self.base = start_test_web(self, str(self.f.root / "test.db"))
        self.get(token=None, host="cater.attacker.example", status=403)

    def test_missing_or_duplicate_host_is_rejected_even_with_token(self):
        for hosts in ([], ["localhost", "localhost"], ["localhost", "cater.attacker.example"]):
            with self.subTest(hosts=hosts):
                connection = http.client.HTTPConnection("127.0.0.1", urlsplit(self.base).port, timeout=5)
                self.addCleanup(connection.close)
                connection.putrequest("GET", f"/api/tasks/{self.f.task}/artifacts/{self.seq}/file?path=empty.txt",
                                      skip_host=True)
                for host in hosts:
                    connection.putheader("Host", host)
                connection.putheader("X-Cater-Access-Token", "fixture-only-access")
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.close()

    def test_sequences_query_shapes_and_raw_fragments_are_strict(self):
        for seq in ("0", "-1", "1.0", "1/else", "9" * 19, "%EF%BC%91", self.artifact["content_sha256"]):
            with self.subTest(seq=seq):
                self.get(seq=seq, status=400)
        valid = urlencode({"path": UNICODE_PATH})
        for query in ("", "path", "path=", valid + "&" + valid, valid + "&path=empty.txt",
                      valid + "&unknown=x", valid + "&unknown=", valid + "&token=fixture-only-access",
                      valid + "&path=empty.txt&path=binary.bin", "Path=empty.txt",
                      "path=empty.txt&%70ath=empty.txt", "path=%FF", "path=%C0%AF",
                      "path=%ED%A0%80", "path=%F4%90%80%80"):
            with self.subTest(query=query):
                self.get(query=query, status=400)
        self.get(query=valid, fragment="#fragment", status=400)
        self.get(query=valid + "&access_token=fixture-only-access", token=None, status=401)

    def test_paths_are_exact_and_query_injection_cannot_select_another_member(self):
        for path in ("../empty.txt", "./empty.txt", "docs/../empty.txt", "/empty.txt", "docs//x",
                     "docs\\x", "C:/private", "empty.txt/", "empty.txt\x00", "empty.txt\n",
                     "empty.txt:stream", ".env", "\uff0e\uff0e/empty.txt", "empty.txt?path=binary.bin"):
            with self.subTest(path=path):
                self.get(path=path, status=400)
        for path in ("EMPTY.txt", "missing", "empty.txt&path=binary.bin",
                     "empty.txt#fragment", "%2e%2e/empty.txt"):
            with self.subTest(path=path):
                self.get(path=path, status=404)

    def test_unlisted_and_oversize_preflight_do_not_read_the_blob(self):
        with patch.object(archive_members, "read_blob") as blob:
            self.get(path="missing", status=404)
            result = self.get(path="large.txt", status=413)
            self.assertEqual(result["code"], "preview_too_large")
            blob.assert_not_called()

    def test_http_responses_release_snapshot_before_send_and_allow_independent_commit(self):
        db_path = self.f.root / "test.db"
        with self.f.rt.log.atomic():
            self.f.rt.log.db.execute("CREATE TABLE preview_send_probe (commits INTEGER NOT NULL)")
            self.f.rt.log.db.execute("INSERT INTO preview_send_probe VALUES (0)")
        missing_blob = self.fault_publication(content_sha256="0" * 64)
        self.base, server, _ = start_test_web(self, str(db_path), return_server=True,
            access_token="fixture-only-access", recover_on_startup=False,
            runtime_factory=lambda: Runtime(db_path, roles_path=self.f.roles,
                drivers={"team_test": self.f.driver}, close_drivers_on_close=False))
        handler_class = server.RequestHandlerClass
        original_runtime = handler_class._runtime
        original_send = handler_class.send_response
        sent, failures = [], []

        def runtime(handler):
            rt = original_runtime(handler)
            handler.preview_test_runtime = rt
            return rt

        def send_response(handler, code, message=None):
            try:
                rt = handler.preview_test_runtime
                self.assertFalse(rt.log.db.in_transaction)
                # Commit on another connection while the response's Runtime is still open.
                with closing(sqlite3.connect(db_path, timeout=0)) as writer:
                    before, = writer.execute("SELECT commits FROM preview_send_probe").fetchone()
                    writer.execute("UPDATE preview_send_probe SET commits = commits + 1")
                    writer.commit()
                with closing(sqlite3.connect(db_path, timeout=0)) as reader:
                    after, = reader.execute("SELECT commits FROM preview_send_probe").fetchone()
                    self.assertEqual(after, before + 1)
                self.assertFalse(rt.log.db.in_transaction)
                sent.append(int(code))
            except Exception as exc:
                # Surface server-thread assertions on the requesting test thread.
                failures.append(f"{handler.path}: {type(exc).__name__}: {exc}")
            return original_send(handler, code, message)

        cases = [
            ("text", 200, {}),
            ("binary", 200, {"path": "binary.bin"}),
            ("download", 200, {"download": True}),
            ("missing_task", 404, {"task": "missing-task"}),
            ("missing_member", 404, {"path": "missing"}),
            ("missing_task_download", 404, {"task": "missing-task", "download": True}),
            ("missing_seq", 404, {"seq": 99999999}),
            ("missing_seq_download", 404, {"seq": 99999999, "download": True}),
            ("oversize", 413, {"path": "large.txt"}),
            ("missing_blob", 409, {"seq": missing_blob}),
            ("missing_blob_download", 409, {"seq": missing_blob, "download": True}),
        ]
        calls = len(self.f.driver.calls)
        with patch.object(handler_class, "_runtime", new=runtime), \
                patch.object(handler_class, "send_response", new=send_response), \
                patch.object(self.f.driver, "tool_turn", side_effect=AssertionError("driver called")):
            for name, status, options in cases:
                with self.subTest(response=name):
                    result = self.get(status=status, **options)
                    self.assertEqual(failures, [])
                    if name == "text":
                        self.assertEqual(result, expected_preview(self.artifact, UNICODE_PATH, TEXT.encode()))
                    elif name == "binary":
                        self.assertEqual(result, expected_preview(
                            self.artifact, "binary.bin", self.files["binary.bin"], "binary"))
                    elif name == "download":
                        self.assertEqual(result, self.blob)
        self.assertEqual(sent, [status for _, status, _ in cases])
        self.assertEqual(self.f.rt.log.db.execute("SELECT commits FROM preview_send_probe").fetchone()[0],
                         len(cases))
        self.assertEqual(len(self.f.driver.calls), calls)

    def test_missing_and_tampered_blob_never_fall_back_to_local_files(self):
        with patch.object(archive_members, "read_blob", side_effect=OSError("INTERNAL_SECRET")):
            self.get(status=409)
        self.get(seq=self.fault_publication(content_sha256="0" * 64), status=409)
        with self.f.rt.log.atomic():
            self.f.rt.log.db.execute("DROP TRIGGER cater_artifact_blobs_no_update")
            self.f.rt.log.db.execute("UPDATE artifact_blobs SET content = ? WHERE sha256 = ?",
                                     (b"x" * len(self.blob), self.artifact["content_sha256"]))
        self.get(status=409)
        self.get(download=True, status=409)
        self.assertTrue((self.project / UNICODE_PATH).exists())
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_member_sha_crc_and_nonzip_use_real_shared_reader(self):
        manifest = copy.deepcopy(self.artifact["archive"])
        next(e for e in manifest["files"] if e["path"] == UNICODE_PATH)["sha256"] = "0" * 64
        self.get(seq=self.fault_publication(archive=manifest), status=409)
        damaged = bytearray(self.blob)
        with zipfile.ZipFile(io.BytesIO(self.blob)) as archive:
            info = archive.getinfo(UNICODE_PATH)
            offset = info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
        damaged[offset] ^= 1
        for body in (bytes(damaged), b"not a ZIP: INTERNAL_SECRET", self.blob[:-10]):
            seq = self.fault_publication(content_sha256=put_blob(self.f.rt.log, body), content_bytes=len(body))
            with self.subTest(size=len(body)):
                self.get(seq=seq, status=409)

    def test_snapshot_survives_workspace_changes_and_reads_have_no_side_effects(self):
        (self.project / UNICODE_PATH).write_bytes(b"CHANGED_WORKSPACE")
        (self.project / "empty.txt").unlink()
        self.project.rename(self.project.with_name("old-workspace-moved"))
        before = self.f.rt.log.read()
        publications = copy.deepcopy(self.f.rt.task(self.f.task).deliverables)
        calls = len(self.f.driver.calls)
        with patch.object(self.f.driver, "tool_turn", side_effect=AssertionError("driver called")), \
                patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("extract")), \
                patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("extract")):
            self.assertEqual(self.get(), expected_preview(self.artifact, UNICODE_PATH, TEXT.encode()))
            self.assertEqual(self.get(path="empty.txt")["content"], "")
            self.assertEqual(self.get(download=True), self.blob)
            self.get(path="missing", status=404)
            self.get(path="large.txt", status=413)
        self.assertEqual(self.f.rt.log.read(), before)
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, publications)
        self.assertEqual(len(self.f.driver.calls), calls)
        self.assertTrue(self.f.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()

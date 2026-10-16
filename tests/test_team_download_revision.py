"""Version-bound downloads through the real handler, with isolated event logs."""

import hashlib
import io
import json
import unittest
import zipfile
from unittest.mock import patch

import cater.web as web
import test_delivery as legacy_fixture
import test_team_web as fixture
from cater.artifacts import normalize_deliverables
from cater.delivery import DeliveryService
from cater.events import Actor
from cater.team import HOST, TeamService


class TeamDownloadRevisionTests(unittest.TestCase):
    factory = fixture.TeamWebTests.factory
    request = fixture.TeamWebTests.request

    def setUp(self):
        fixture.TeamWebTests.setUp(self)
        self.model = self.enterContext(patch.object(self.driver, "tool_turn", side_effect=AssertionError("model call")))
        self.complete = self.enterContext(patch.object(self.driver, "complete", side_effect=AssertionError("model call")))
        with self.factory() as rt:
            rt.log.db.execute("PRAGMA journal_mode=WAL")
            self.task = rt.create_task("Original download objective")
            rt.team.enable(self.task)
        self.artifact = self.publish("Delivered \u4e2d\u6587\n\U0001f642\r\n")

    def publish(self, body="Later bytes", **changes):
        artifact, = normalize_deliverables([{
            "filename": "note.txt", "title": "Note", "summary": "Fixture", "format": "text", "content": body,
        }])
        with self.factory() as rt:
            return rt.log.emit(type="deliverable.published", task_id=self.task, actor=rt._leader_actor(),
                               payload={**artifact, "source": "team", **changes})

    @staticmethod
    def ref(event):
        return {"seq": event.seq, **{k: event.payload[k] for k in ("filename", "content_sha256")}}

    def finish(self, refs=None, *, include_artifacts=True, actor=HOST):
        payload = {"summary": "Fixture ready", "verification": "Offline fixture only"}
        if include_artifacts:
            payload["artifacts"] = [self.ref(self.artifact)] if refs is None else refs
        with self.factory() as rt:
            return rt.log.emit(type="team.objective_completed", task_id=self.task, actor=actor, payload=payload)

    def state(self):
        with self.factory() as rt:
            chain = rt.verify_chain()
            self.assertTrue(chain.ok)
            return rt.log.read(), chain

    def download(self, query="", *, expected=200, task=None):
        before = self.state()
        raw = self.request(f"/api/tasks/{task or self.task}/delivery{query}", expected=expected)
        self.assertEqual(self.state(), before)
        self.model.assert_not_called()
        self.complete.assert_not_called()
        self.start.assert_not_called()
        return raw

    def assert_zip(self, raw, artifact, *, objective="Original download objective"):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            body = archive.read(artifact.payload["filename"])
            self.assertEqual(body, artifact.payload["content"].encode("utf-8"))
            manifest = json.loads(archive.read("__cater__/manifest.json"))
            self.assertEqual(manifest["objective"], objective)
            self.assertEqual(manifest["files"], [{"filename": artifact.payload["filename"], "seq": artifact.seq,
                                                "sha256": hashlib.sha256(body).hexdigest()}])
            self.assertEqual(set(archive.namelist()), {artifact.payload["filename"], "__cater__/manifest.json"})

    def test_expected_revision_exports_frozen_utf8_bytes_and_is_read_only(self):
        delivery = self.finish()
        self.publish("Unaccepted replacement")
        with patch.object(DeliveryService, "archive", side_effect=AssertionError("legacy download")):
            self.assert_zip(self.download(f"?delivery_seq={delivery.seq}"), self.artifact)
            self.assert_zip(self.download(), self.artifact)

    def test_service_rejects_invalid_expected_revisions(self):
        delivery = self.finish()
        before = self.state()
        with self.factory() as rt:
            for value in (True, False, 0, -1, "1", "", 1.0, [], {}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    rt.team.archive(self.task, expected_delivery_seq=value)
            self.assert_zip(rt.team.archive(self.task, expected_delivery_seq=delivery.seq), self.artifact)
        self.assertEqual(self.state(), before)

    def test_running_task_and_nonmatching_revision_conflict(self):
        self.download("?delivery_seq=1", expected=409)
        with self.factory() as rt, self.assertRaises(ValueError):
            rt.team.archive(self.task, expected_delivery_seq=1)
        delivery = self.finish()
        for seq in (self.artifact.seq, delivery.seq + 1000):
            with self.subTest(seq=seq):
                self.download(f"?delivery_seq={seq}", expected=409)

    def test_objective_switch_revokes_expected_download_without_candidate_fallback(self):
        delivery = self.finish()
        with self.factory() as rt:
            rt.log.emit(type="team.objective_updated", actor=HOST, task_id=self.task, payload={"goal": "New objective"})
        latest = self.publish("New candidate")
        self.download(f"?delivery_seq={delivery.seq}", expected=409)
        self.assert_zip(self.download(), latest, objective="New objective")
        with self.factory() as rt, self.assertRaises(ValueError):
            rt.team.archive(self.task, expected_delivery_seq=delivery.seq)

    def test_new_completion_revokes_old_expected_download(self):
        first = self.finish()
        latest = self.publish("New delivery")
        second = self.finish([self.ref(latest)])
        self.download(f"?delivery_seq={first.seq}", expected=409)
        self.assert_zip(self.download(f"?delivery_seq={second.seq}"), latest)
        with self.factory() as rt, self.assertRaises(ValueError):
            rt.team.archive(self.task, expected_delivery_seq=first.seq)

    def test_untrusted_completion_cannot_change_expected_revision(self):
        delivery = self.finish()
        fake = self.finish(actor=Actor.agent("untrusted", "fixture"))
        self.assert_zip(self.download(f"?delivery_seq={delivery.seq}"), self.artifact)
        self.download(f"?delivery_seq={fake.seq}", expected=409)

    def test_http_rejects_empty_duplicate_and_noninteger_parameters(self):
        delivery = self.finish()
        invalid = ("", "0", "-1", "+1", "%2B1", "1.0", "1e0", "NaN", "abc", "%201", "1%20",
                   "%EF%BC%91", "%D9%A1", "%00", "9" * 5000)
        for value in invalid:
            with self.subTest(value=value[:30]):
                self.download(f"?delivery_seq={value}", expected=400)
        for query in ("?delivery_seq", f"?delivery_seq={delivery.seq}&delivery_seq={delivery.seq}",
                      f"?delivery_seq=&delivery_seq={delivery.seq}", f"?delivery_seq={delivery.seq}&delivery_seq"):
            with self.subTest(query=query):
                self.download(query, expected=400)
        self.assert_zip(self.download(f"?other=&delivery_seq={delivery.seq}"), self.artifact)

    def test_missing_task_is_404_including_invalid_parameter(self):
        for query in ("", "?delivery_seq=1", "?delivery_seq=", "?delivery_seq=0&delivery_seq=2"):
            with self.subTest(query=query):
                self.download(query, task="missing-task", expected=404)

    def test_legacy_flow_never_ignores_delivery_seq(self):
        with self.factory() as rt:
            legacy = rt.create_task("Legacy download")
        with patch.object(DeliveryService, "archive", side_effect=AssertionError("legacy archive called")), \
                patch.object(TeamService, "archive", side_effect=AssertionError("native archive called")):
            for query in ("?delivery_seq=1", "?delivery_seq=", "?delivery_seq=0", "?delivery_seq=1&delivery_seq=2"):
                with self.subTest(query=query):
                    self.download(query, task=legacy, expected=400)

    def test_legacy_unparameterized_download_keeps_existing_archive_route(self):
        with self.factory() as rt:
            legacy = rt.create_task("Legacy download")
        with patch.object(DeliveryService, "archive", return_value=b"legacy-zip") as archive, \
                patch.object(TeamService, "archive", side_effect=AssertionError("native archive called")):
            self.assertEqual(self.download(task=legacy), b"legacy-zip")
        self.assertEqual(archive.call_args.args, (legacy,))
        self.assertEqual(archive.call_args.kwargs, {})

    def test_accepted_legacy_unparameterized_download_exports_real_zip_without_model_calls(self):
        legacy = legacy_fixture.DeliveryTests()
        self.addCleanup(legacy.doCleanups)
        legacy.setUp()
        view = legacy.prepare()
        self.assertEqual(view["status"], "accepted")

        def factory():
            return legacy_fixture.Runtime(legacy.db, roles_path=legacy.roles, drivers=legacy.drivers,
                                          close_drivers_on_close=False)

        with patch.object(web, "ActivityRunner", return_value=self.runner):
            handler = web.make_handler(legacy.db, runtime_factory=factory)
        with patch.object(self, "factory", factory), patch.object(self, "handler", handler), \
                patch.object(legacy.worker, "complete", side_effect=AssertionError("legacy model call")), \
                patch.object(legacy.final, "complete", side_effect=AssertionError("legacy model call")), \
                patch.object(TeamService, "archive", side_effect=AssertionError("native archive called")):
            raw = self.download(task=legacy.task)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(archive.read("work-1/result.md"),
                             "# \u79bb\u7ebf\u6587\u6863\u6210\u679c\n\n\u4fdd\u7559\u9010\u6761\u590d\u67e5\u4f9d\u636e\u3002\n".encode("utf-8"))
            self.assertEqual(json.loads(archive.read("manifest.json"))["digest"], view["digest"])
            self.assertIn("handoff.md", archive.namelist())

    def test_native_running_and_old_completion_without_manifest_remain_compatible(self):
        self.assert_zip(self.download(), self.artifact)
        latest = self.publish("Legacy native bytes")
        delivery = self.finish(include_artifacts=False)
        self.assert_zip(self.download(), latest)
        self.download(f"?delivery_seq={delivery.seq}", expected=409)

    def test_missing_snapshot_reference_never_uses_later_same_name_file(self):
        self.finish()
        self.publish("Latest file cannot replace missing snapshot")
        for seq in (self.artifact.seq - 1, 999999):
            stale = self.finish([{**self.ref(self.artifact), "seq": seq}])
            for query in ("", f"?delivery_seq={stale.seq}"):
                with self.subTest(seq=seq, query=query):
                    self.download(query, expected=409)

    def test_frozen_body_hash_is_checked_even_when_both_metadata_hashes_agree(self):
        for changes in ({"content_sha256": "0" * 64}, {"content": "Changed body, original metadata"}):
            with self.subTest(changes=changes):
                forged = self.publish("Expected body", **changes)
                delivery = self.finish([self.ref(forged)])
                self.publish("New valid body must not replace forged snapshot")
                for query in ("", f"?delivery_seq={delivery.seq}"):
                    self.download(query, expected=409)

    def test_frozen_missing_or_nontext_body_never_falls_back_to_local_or_latest_file(self):
        for content in (None, 123):
            with self.subTest(content=content):
                broken = self.publish()
                with self.factory() as rt:
                    payload = {**broken.payload, "content": content}
                    if content is None:
                        del payload["content"]
                    missing = rt.log.emit(type="deliverable.published", task_id=self.task, actor=rt._leader_actor(), payload=payload)
                delivery = self.finish([self.ref(missing)])
                self.publish("Valid replacement")
                self.download(f"?delivery_seq={delivery.seq}", expected=409)
                self.download(expected=409)

    def test_malformed_empty_duplicate_and_mismatched_manifests_are_not_packaged(self):
        ref = self.ref(self.artifact)
        manifests = ([], {}, "bad", [None], [{}], [ref, ref], [{**ref, "seq": True}],
                     [{**ref, "seq": [ref["seq"]]}], [{**ref, "filename": "other.txt"}],
                     [{**ref, "content_sha256": "0" * 64}], [{"seq": ref["seq"], "filename": "note.txt"}])
        for refs in manifests:
            with self.subTest(refs=refs):
                delivery = self.finish(refs)
                self.download(f"?delivery_seq={delivery.seq}", expected=409)
                self.download(expected=409)
        delivery = self.finish()
        self.assert_zip(self.download(f"?delivery_seq={delivery.seq}"), self.artifact)

    def assert_snapshot_race(self, *, new_completion):
        delivery = self.finish()
        before, _ = self.state()
        appended = []
        original_enabled = web._team_enabled

        def switch_after_existence_check(rt, task):
            self.assertTrue(rt.log.db.in_transaction)
            with self.factory() as writer:
                appended.append(writer.log.emit(type="team.objective_updated", task_id=task, actor=HOST,
                                               payload={"goal": "Concurrent objective"}))
            if new_completion:
                latest = self.publish("Concurrent new delivery")
                appended.extend([latest, self.finish([self.ref(latest)])])
            return original_enabled(rt, task)

        original_archive = TeamService.archive

        def archive_in_snapshot(service, task, **kwargs):
            self.assertTrue(service.rt.log.db.in_transaction)
            self.assertEqual(service.view(task)["revision"], delivery.seq)
            return original_archive(service, task, **kwargs)

        with patch.object(web, "_team_enabled", switch_after_existence_check), \
                patch.object(TeamService, "archive", archive_in_snapshot):
            raw = self.request(f"/api/tasks/{self.task}/delivery?delivery_seq={delivery.seq}")
        self.assert_zip(raw, self.artifact)
        self.assertEqual(self.state()[0], before + appended)
        self.download(f"?delivery_seq={delivery.seq}", expected=409)
        self.model.assert_not_called()
        self.complete.assert_not_called()
        self.start.assert_not_called()

    def test_http_snapshot_covers_objective_switch_between_detection_and_archive(self):
        self.assert_snapshot_race(new_completion=False)

    def test_http_snapshot_covers_new_completion_between_detection_and_archive(self):
        self.assert_snapshot_race(new_completion=True)

    def test_http_snapshot_remains_active_during_zip_writes(self):
        delivery = self.finish()
        original_archive = TeamService.archive
        original_write = zipfile.ZipFile.writestr

        def archive_with_concurrent_completion(service, task, **kwargs):
            def write(archive, name, *args, **options):
                if name == "note.txt":
                    self.assertTrue(service.rt.log.db.in_transaction)
                    with self.factory() as writer:
                        writer.log.emit(type="team.objective_updated", task_id=task, actor=HOST,
                                        payload={"goal": "Changed during ZIP write"})
                    latest = self.publish("New file during ZIP write")
                    self.finish([self.ref(latest)])
                    self.assertEqual(service.view(task)["revision"], delivery.seq)
                return original_write(archive, name, *args, **options)

            with patch.object(zipfile.ZipFile, "writestr", write):
                return original_archive(service, task, **kwargs)

        with patch.object(TeamService, "archive", archive_with_concurrent_completion):
            raw = self.request(f"/api/tasks/{self.task}/delivery?delivery_seq={delivery.seq}")
        self.assert_zip(raw, self.artifact)
        self.download(f"?delivery_seq={delivery.seq}", expected=409)

    def test_read_snapshot_is_released_before_network_headers_or_body(self):
        delivery = self.finish()
        with self.factory() as rt:
            rt.log.db.execute("PRAGMA journal_mode=DELETE")
        readers = []
        original_archive = TeamService.archive
        original_headers = self.handler.end_headers

        def build(service, task, **kwargs):
            readers.append(service.rt)
            return original_archive(service, task, **kwargs)

        def send(handler):
            self.assertFalse(readers[-1].log.db.in_transaction)
            with self.factory() as writer:
                writer.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "Change during network send"})
            return original_headers(handler)

        with patch.object(TeamService, "archive", build), patch.object(self.handler, "end_headers", send):
            raw = self.request(f"/api/tasks/{self.task}/delivery?delivery_seq={delivery.seq}")
        self.assert_zip(raw, self.artifact)
        self.download(f"?delivery_seq={delivery.seq}", expected=409)


if __name__ == "__main__":
    unittest.main()

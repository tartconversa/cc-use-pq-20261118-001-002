import unittest
from tests.common import ServiceCase
from meridian.demo import decision, observation, query
from meridian.errors import MeridianError


NOV = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
       "regions": [], "topics": [], "audiences": []}


class RegionCorrectionTests(ServiceCase):
    def correct(self, source_id, region, effective_at, basis="Masthead moved to new distribution area",
                **extra):
        return self.service.sources.correct_region(
            {"source_id": source_id, "region": region, "effective_at": effective_at,
             "basis": basis, **extra})

    def publish(self, spec):
        preview = self.service.releases.preview(spec)
        return self.service.releases.publish("release", spec, preview["token"])

    def test_correction_requires_effective_time_and_basis(self):
        with self.assertRaises(MeridianError) as caught:
            self.service.sources.correct_region(
                {"source_id": "north-a", "region": "South",
                 "effective_at": "2026-11-15T00:00:00+00:00"})
        self.assertEqual(caught.exception.code, "invalid_region_correction")
        with self.assertRaises(MeridianError) as caught:
            self.service.sources.correct_region(
                {"source_id": "north-a", "region": "South", "basis": "evidence",
                 "effective_at": "2026-11-15T00:00:00"})
        self.assertEqual(caught.exception.code, "invalid_time")

    def test_correction_is_idempotent_and_conflicts_are_rejected(self):
        first = self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        second = self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        with self.assertRaises(MeridianError) as caught:
            self.correct("north-a", "East", "2026-11-15T00:00:00+00:00")
        self.assertEqual(caught.exception.code, "region_correction_conflict")
        with self.assertRaises(MeridianError) as caught:
            self.correct("north-a", "South", "2026-11-20T00:00:00+00:00")
        self.assertEqual(caught.exception.code, "region_correction_noop")

    def test_correction_records_effective_time_and_evidence(self):
        result = self.correct("north-a", "South", "2026-11-15T00:00:00+00:00",
                              basis="Publisher distribution audit dated 2026-11-10",
                              reason="Verified serving South distribution from mid-November")
        self.assertEqual(result["effective_at"], "2026-11-15T00:00:00+00:00")
        self.assertEqual(result["basis"], "Publisher distribution audit dated 2026-11-10")
        history = self.service.provenance.region_history("north-a")
        self.assertTrue(history["corrected"])
        self.assertEqual(history["registered_region"], "North")
        self.assertEqual([segment["region"] for segment in history["segments"]], ["North", "South"])

    def test_old_articles_stay_with_region_in_force_when_published(self):
        self.accepted("before", "north-a", "before")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.code("after")
        # Correction recorded after both articles, but effective only from mid-November.
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        distribution = self.service.analytics.report(NOV | {"region_basis": "distribution"})
        regions = {row["region"]: row["story_count"] for row in distribution["regions"]}
        self.assertEqual(regions, {"North": 1, "South": 1})
        registered = self.service.analytics.report(NOV)
        self.assertEqual({row["region"]: row["story_count"] for row in registered["regions"]},
                         {"North": 2})

    def test_late_entry_does_not_replace_publication_time(self):
        # Article published before the effective point but only received afterwards.
        late = observation("late", "north-a", "late", "2026-11-05T10:00:00+00:00")
        late["received_at"] = "2026-12-05T00:00:00+00:00"
        self.service.observations.ingest(late)
        self.code("late")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        distribution = self.service.analytics.report(NOV | {"region_basis": "distribution"})
        self.assertEqual(distribution["regions"][0]["region"], "North")
        self.assertEqual(distribution["region_attribution"]["late_count"], 1)
        self.assertEqual(distribution["region_attribution"]["backfilled_count"], 1)
        self.assertEqual(self.service.sources.region_at(
            "north-a", "2026-11-05T10:00:00+00:00")["region"], "North")

    def test_reports_explain_both_region_bases(self):
        self.accepted("before", "north-a", "before")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.code("after")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        report = self.service.analytics.report(NOV | {"region_basis": "distribution"})
        self.assertEqual(report["region_basis"], "distribution")
        self.assertIn("registered", report["region_bases"])
        self.assertIn("distribution", report["region_bases"])
        self.assertEqual(report["region_bases"]["registered"]["story_count"], 2)
        self.assertEqual(report["region_bases"]["distribution"]["story_count"], 2)
        self.assertEqual(report["region_attribution"]["changed_count"], 1)
        moved = report["region_attribution"]["changed"][0]
        self.assertEqual((moved["registered_region"], moved["distribution_region"]),
                         ("North", "South"))

    def test_reconciliation_quantifies_attribution_not_trend(self):
        self.accepted("before", "north-a", "before")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.code("after")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        result = self.service.analytics.reconciliation(NOV | {"region_basis": "distribution"})
        rows = {row["region"]: row for row in result["regions"]}
        self.assertEqual(rows["North"]["registered_story_count"], 2)
        self.assertEqual(rows["North"]["distribution_story_count"], 1)
        self.assertEqual(rows["South"]["registered_story_count"], 0)
        self.assertEqual(rows["South"]["distribution_story_count"], 1)
        self.assertEqual(rows["North"]["story_delta_explained_by_attribution"], -1)
        self.assertEqual(rows["North"]["story_delta_unexplained"], 0)
        self.assertEqual(rows["South"]["story_delta_explained_by_attribution"], 1)
        self.assertEqual(result["attribution_moved_count"], 1)
        self.assertEqual(result["collection_frames"], [])

    def test_reconciliation_reports_crossing_window_without_double_counting(self):
        self.accepted("before", "north-a", "before")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.code("after")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        self.service.frames.open("frame", "north-a", NOV["start"], NOV["end"], 2)
        self.service.frames.close("frame")
        result = self.service.analytics.reconciliation(NOV)
        frame = result["collection_frames"][0]
        self.assertEqual(frame["expected_count"], 2)
        self.assertEqual(frame["region_counts"], {"North": 1, "South": 1})
        self.assertEqual(sum(frame["region_counts"].values()), frame["expected_count"])

    def test_frozen_release_keeps_old_basis_and_new_release_uses_new_basis(self):
        self.accepted("before", "north-a", "before")
        frozen = self.publish(NOV)
        self.assertEqual(frozen["region_basis"], "registered")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.code("after")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        self.assertEqual(self.service.releases.describe("release")["report"]["story_count"], 1)
        comparison = self.service.releases.compare("release")
        self.assertEqual(comparison["region_basis"], "registered")
        self.assertEqual(comparison["current_region_basis"], "registered")
        preview = self.service.releases.preview(NOV | {"region_basis": "distribution"})
        fresh = self.service.releases.publish("release-2", NOV | {"region_basis": "distribution"},
                                              preview["token"])
        self.assertEqual(fresh["region_basis"], "distribution")
        self.assertEqual({row["region"]: row["story_count"] for row in fresh["report"]["regions"]},
                         {"North": 1, "South": 1})
        with self.assertRaises(MeridianError) as caught:
            self.service.releases.publish("release-3", NOV, preview["token"])
        self.assertEqual(caught.exception.code, "stale_preview")

    def test_changes_endpoint_explains_correction_origin(self):
        self.accepted("before", "north-a", "before")
        self.publish(NOV)
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.code("after")
        changes = self.service.provenance.release_changes("release")
        self.assertEqual(changes["region_basis"], "registered")
        self.assertEqual([row["change"] for row in changes["changed_stories"]], ["added"])

    def test_correction_does_not_rewrite_observation_source_identity(self):
        self.accepted("a", "north-a", "a")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        item = self.service.observations.describe("local-fixture", "a")
        self.assertEqual(item["data"]["source_id"], "north-a")
        lineage = self.service.observations.lineage("local-fixture", "a")
        self.assertEqual(len(lineage), 1)
        changed = observation("a", "north-b", "a")
        with self.assertRaises(MeridianError) as caught:
            self.service.observations.ingest(changed, expected_revision=1)
        self.assertEqual(caught.exception.code, "source_identity_conflict")

    def test_retraction_after_correction_reinterprets_current_denominator(self):
        self.accepted("before", "north-a", "before")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.code("after")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        self.service.observations.retract("local-fixture", "after", "withdrawn",
                                          expected_revision=1)
        report = self.service.analytics.report(NOV | {"region_basis": "distribution"})
        self.assertEqual({row["region"]: row["story_count"] for row in report["regions"]},
                         {"North": 1})
        self.assertEqual(report["excluded"], {"retracted": 1})
        contribution = self.service.provenance.source_contribution(
            NOV | {"region_basis": "distribution"}, "north-a")
        self.assertEqual(contribution["categories"], {"eligible": 1, "retracted": 1})
        self.assertEqual(contribution["attributed_region_counts"], {"North": 1, "South": 1})

    def test_region_attribution_survives_reopen(self):
        self.accepted("before", "north-a", "before")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        before = self.service.analytics.report(NOV | {"region_basis": "distribution"})
        self.service.close()
        from meridian.service import MeridianService
        self.service = MeridianService(self.path)
        after = self.service.analytics.report(NOV | {"region_basis": "distribution"})
        self.assertEqual(before["stories"], after["stories"])
        self.assertEqual(before["region_basis"], after["region_basis"])
        self.assertTrue(self.service.audit.run()["ok"])

    def test_invalid_region_basis_rejected(self):
        with self.assertRaises(MeridianError) as caught:
            self.service.analytics.report(NOV | {"region_basis": "guess"})
        self.assertEqual(caught.exception.code, "invalid_region_basis")

    def test_audit_reconciles_region_partition_and_frame_coverage(self):
        self.accepted("before", "north-a", "before")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.code("after")
        self.correct("north-a", "South", "2026-11-15T00:00:00+00:00")
        self.service.frames.open("frame", "north-a", NOV["start"], NOV["end"], 2)
        self.service.frames.close("frame")
        self.assertTrue(self.service.audit.run()["ok"])
        self.service.store.db.execute(
            "DELETE FROM frame_members WHERE frame_id='frame' AND external_id='after'")
        issues = [item["kind"] for item in self.service.audit.run()["issues"]]
        self.assertIn("frame_members", issues)
        self.assertIn("frame_coverage_count", issues)

    def test_api_exposes_region_correction_and_basis(self):
        from fastapi.testclient import TestClient
        from meridian.api import create_app
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(str(Path(directory) / "api.sqlite3"))
            client = TestClient(app)
            try:
                client.post("/sources", json={
                    "source_id": "a", "region": "North", "outlet": "A",
                    "topics": ["technology"], "audiences": ["general"]})
                corrected = client.post("/sources/region-corrections", json={
                    "source_id": "a", "region": "South",
                    "effective_at": "2026-11-15T00:00:00+00:00", "basis": "Publisher audit"})
                self.assertEqual(corrected.status_code, 200)
                regions = client.get("/sources/a/regions").json()
                self.assertEqual([segment["region"] for segment in regions["segments"]],
                                 ["North", "South"])
                at = client.get("/sources/a/region-at",
                                params={"at": "2026-11-01T00:00:00+00:00"}).json()
                self.assertEqual(at["region"], "North")
                bad = client.post("/sources/region-corrections", json={
                    "source_id": "a", "region": "South",
                    "effective_at": "2026-11-15T00:00:00+00:00", "basis": "Publisher audit"})
                self.assertTrue(bad.json()["duplicate"])
                report = client.post("/reports", json=NOV | {"region_basis": "distribution"}).json()
                self.assertEqual(report["region_basis"], "distribution")
                self.assertIn("registered", report["region_bases"])
                reconciliation = client.post(
                    "/reports/region-reconciliation", json=NOV).json()
                self.assertEqual(reconciliation["attribution_moved_count"], 0)
            finally:
                app.state.service.close()


class FrameCrossingTests(ServiceCase):
    def test_frame_keeps_expected_count_and_splits_members_across_correction(self):
        self.add("before", "north-a", "before")
        self.service.observations.ingest(observation(
            "after", "north-a", "after", "2026-11-20T10:00:00+00:00"))
        self.service.sources.correct_region(
            {"source_id": "north-a", "region": "South",
             "effective_at": "2026-11-15T00:00:00+00:00", "basis": "Distribution audit"})
        self.service.frames.open("frame", "north-a", NOV["start"], NOV["end"], 2)
        closed = self.service.frames.close("frame")
        self.assertEqual(closed["actual_count"], 2)
        self.assertEqual(closed["expected_count"], 2)
        self.assertTrue(closed["crosses_region_correction"])
        self.assertEqual(closed["sealed_region_counts"], {"North": 1, "South": 1})
        # The window still owes its original expected count as one contract, not 2+2.
        self.assertEqual(sum(closed["sealed_region_counts"].values()), closed["expected_count"])
        coverage = self.service.frames.quality(
            NOV | {"regions": ["North", "South"], "region_basis": "distribution"})
        self.assertIn("frame", coverage["crossing_frames"])

    def test_frame_quality_still_checks_open_window_in_either_region(self):
        self.accepted("before", "north-a", "before")
        self.service.sources.correct_region(
            {"source_id": "north-a", "region": "South",
             "effective_at": "2026-11-15T00:00:00+00:00", "basis": "Distribution audit"})
        self.service.frames.open("frame", "north-a", NOV["start"], NOV["end"], 1)
        quality = self.service.frames.quality(
            NOV | {"regions": ["South"], "region_basis": "distribution"})
        self.assertFalse(quality["passed"])
        self.assertEqual([item["reason"] for item in quality["problems"]], ["open"])

    def test_frame_close_requires_original_expected_count(self):
        self.add("before", "north-a", "before")
        self.service.frames.open("frame", "north-a", NOV["start"], NOV["end"], 2)
        with self.assertRaises(MeridianError) as caught:
            self.service.frames.close("frame")
        self.assertEqual(caught.exception.code, "frame_incomplete")


if __name__ == "__main__":
    unittest.main()

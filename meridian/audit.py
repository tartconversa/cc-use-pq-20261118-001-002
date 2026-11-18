"""检查跨表不变量并追溯报告证据。"""
from collections import Counter
from .contracts import digest
from .storage import decode


class IntegrityAudit:
    def __init__(self, store):
        self.store = store

    def run(self):
        issues = []
        observations = self.store.all("SELECT * FROM observations ORDER BY origin,external_id,revision")
        grouped = {}
        for row in observations:
            key = (row["origin"], row["external_id"])
            grouped.setdefault(key, []).append(row)
            if digest(decode(row["payload"])) != row["payload_hash"]:
                issues.append({"kind": "observation_hash", "key": key, "revision": row["revision"]})
        for key, revisions in grouped.items():
            numbers = [row["revision"] for row in revisions]
            if numbers != list(range(1, max(numbers) + 1)):
                issues.append({"kind": "revision_gap", "key": key})
            head = self.store.one("SELECT revision FROM observation_heads WHERE origin=? AND external_id=?", key)
            if head is None or head["revision"] != max(numbers):
                issues.append({"kind": "head_mismatch", "key": key})
        for row in self.store.all("SELECT * FROM decisions"):
            parent = self.store.one("SELECT 1 FROM observations WHERE origin=? AND external_id=? AND revision=?",
                                    (row["origin"], row["external_id"], row["revision"]))
            if parent is None:
                issues.append({"kind": "orphan_decision", "decision_id": row["decision_id"]})
        for row in self.store.all("SELECT * FROM batches"):
            counts = Counter(item["status"] for item in self.store.all(
                "SELECT status FROM batch_rows WHERE batch_id=?", (row["batch_id"],)))
            if counts["accepted"] != row["accepted"] or counts["duplicate"] != row["duplicate_count"] or counts["rejected"] != row["rejected"]:
                issues.append({"kind": "batch_count", "batch_id": row["batch_id"]})
            if sum(counts.values()) != row["cursor"]:
                issues.append({"kind": "batch_cursor", "batch_id": row["batch_id"]})
        for row in self.store.all("SELECT * FROM releases"):
            report = decode(row["report_json"])
            if report["query_hash"] != row["query_hash"] or report["event_head"] != row["event_head"]:
                issues.append({"kind": "release_metadata", "release_id": row["release_id"]})
            if report["story_count"] != len(report["stories"]):
                issues.append({"kind": "release_count", "release_id": row["release_id"]})
            if sum(item["observation_count"] for item in report["stories"]) != report["eligible_observations"]:
                issues.append({"kind": "eligible_reconciliation", "release_id": row["release_id"]})
            if report.get("region_basis", "registered") != row["region_basis"]:
                issues.append({"kind": "release_region_basis", "release_id": row["release_id"]})
            for region in report.get("regions", []):
                total = sum(item["observation_count"] for item in report["stories"]
                            if item["region"] == region["region"])
                if total != region["observation_count"]:
                    issues.append({"kind": "release_region_reconciliation",
                                   "release_id": row["release_id"], "region": region["region"]})
        for row in self.store.all("SELECT * FROM collection_frames ORDER BY frame_id"):
            members = self.store.all("SELECT origin,external_id,revision FROM frame_members WHERE frame_id=?",
                                     (row["frame_id"],))
            if row["state"] == "closed" and len(members) != row["actual_count"]:
                issues.append({"kind": "frame_members", "frame_id": row["frame_id"]})
            distinct = len({(item["origin"], item["external_id"]) for item in members})
            if distinct != len(members):
                issues.append({"kind": "frame_duplicate_members", "frame_id": row["frame_id"]})
            regions = Counter()
            for member in members:
                parent = self.store.one(
                    "SELECT published_at FROM observations WHERE origin=? AND external_id=? AND revision=?",
                    (member["origin"], member["external_id"], member["revision"]))
                if parent is None:
                    issues.append({"kind": "frame_member_missing", "frame_id": row["frame_id"]})
                    continue
                regions[self._region_at(row["source_id"], parent["published_at"])] += 1
            if sum(regions.values()) != len(members):
                issues.append({"kind": "frame_region_partition", "frame_id": row["frame_id"]})
            if row["state"] == "closed" and len(members) != row["expected_count"]:
                issues.append({"kind": "frame_coverage_count", "frame_id": row["frame_id"]})
        previous = {}
        last_region = {}
        for row in self.store.all("SELECT * FROM source_region_corrections "
                                  "ORDER BY source_id,effective_at,correction_id"):
            source = self.store.one("SELECT source_id FROM sources WHERE source_id=?", (row["source_id"],))
            if source is None:
                issues.append({"kind": "orphan_region_correction",
                               "correction_id": row["correction_id"]})
            key = (row["source_id"], row["effective_at"])
            if key in previous:
                issues.append({"kind": "duplicate_region_effective_at",
                               "source_id": row["source_id"], "effective_at": row["effective_at"]})
            if not row["basis"].strip() or not row["effective_at"] or not row["region"].strip():
                issues.append({"kind": "region_correction_evidence",
                               "correction_id": row["correction_id"]})
            if last_region.get(row["source_id"]) == row["region"]:
                issues.append({"kind": "region_correction_noop",
                               "correction_id": row["correction_id"]})
            previous[key] = True
            last_region[row["source_id"]] = row["region"]
        for row in self.store.all("SELECT * FROM releases"):
            if row["region_basis"] not in {"registered", "distribution"}:
                issues.append({"kind": "release_region_basis", "release_id": row["release_id"]})
        return {"ok": not issues, "issues": issues, "event_head": self.store.head(),
                "observation_revisions": len(observations), "logical_observations": len(grouped)}

    def _region_at(self, source_id, published_at):
        region = self.store.one("SELECT region FROM sources WHERE source_id=?", (source_id,))["region"]
        for row in self.store.all(
                "SELECT region FROM source_region_corrections WHERE source_id=? AND effective_at<=? "
                "ORDER BY effective_at, correction_id", (source_id, published_at)):
            region = row["region"]
        return region

    def events(self, after=0, limit=100):
        from .errors import require
        require(type(after) is int and after >= 0, "invalid_cursor", "after must be nonnegative")
        require(type(limit) is int and 1 <= limit <= 1000, "invalid_limit", "limit must be 1 to 1000")
        rows = self.store.all("SELECT * FROM events WHERE event_no>? ORDER BY event_no LIMIT ?", (after, limit))
        return [dict(row) for row in rows]

    def summary(self):
        audit = self.run()
        kinds = {}
        for row in self.store.all("SELECT kind,COUNT(*) AS count FROM events GROUP BY kind ORDER BY kind"):
            kinds[row["kind"]] = row["count"]
        audit["event_kinds"] = kinds
        audit["source_count"] = self.store.one("SELECT COUNT(*) AS count FROM sources")["count"]
        audit["decision_count"] = self.store.one("SELECT COUNT(*) AS count FROM decisions")["count"]
        audit["release_count"] = self.store.one("SELECT COUNT(*) AS count FROM releases")["count"]
        return audit


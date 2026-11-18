"""导出可独立检查的完整报告证据包。"""
from collections import Counter, defaultdict
from .contracts import digest, report_query, text
from .errors import require


class EvidenceExporter:
    def __init__(self, store, observations, sources, coding, analytics, releases):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding
        self.analytics = analytics
        self.releases = releases

    def _evidence_rows(self, report):
        rows = []
        query = report["query"]
        for row in self.analytics._rows(query):
            item = row["item"]
            data = item["data"]
            state = row["state"]
            category = self.analytics._classify(row, query)
            coding = state.get("coding")
            rows.append({"origin": data["origin"], "external_id": data["external_id"],
                         "revision": item["revision"], "observation_hash": item["hash"],
                         "source_id": data["source_id"], "region": row["region"],
                         "registered_region": row["attribution"]["registered_region"],
                         "distribution_region": row["attribution"]["distribution_region"],
                         "region_basis": report["region_basis"],
                         "region_effective_at": row["attribution"]["effective_at"],
                         "correction_id": row["attribution"]["correction_id"],
                         "story_key": data["story_key"], "published_at": data["published_at"],
                         "category": category, "coding": coding,
                         "reviewer_ids": [d["decision_id"] for d in state["decisions"]]})
        rows.sort(key=lambda item: (item["origin"], item["external_id"]))
        return rows

    def current(self, query):
        report = self.analytics.report(query)
        evidence = self._evidence_rows(report)
        bundle = {"kind": "current", "report": report, "evidence": evidence,
                  "region_basis": report["region_basis"],
                  "event_head": report["event_head"]}
        bundle["bundle_hash"] = digest(bundle)
        return bundle

    def frozen(self, release_id):
        release_id = text(release_id, "release_id")
        release = self.releases.describe(release_id)
        report = release["report"]
        evidence = report["provenance"]
        bundle = {"kind": "frozen", "release_id": release_id, "report": report,
                  "region_basis": release.get("region_basis", report.get("region_basis", "registered")),
                  "evidence": evidence, "event_head": release["event_head"],
                  "preview_token": release["preview_token"]}
        bundle["bundle_hash"] = digest(bundle)
        return bundle

    @staticmethod
    def verify(bundle):
        require(isinstance(bundle, dict), "invalid_bundle", "bundle must be an object")
        required = {"kind", "report", "evidence", "event_head", "bundle_hash"}
        require(required <= set(bundle), "invalid_bundle", "bundle fields missing")
        actual = digest({key: value for key, value in bundle.items() if key != "bundle_hash"})
        require(actual == bundle["bundle_hash"], "bundle_hash_mismatch", "bundle content changed")
        report = bundle["report"]
        require(report["event_head"] == bundle["event_head"], "bundle_head_mismatch",
                "report and evidence head differ")
        require(report["story_count"] == len(report["stories"]), "bundle_count_mismatch",
                "story count differs from rows")
        require(report.get("region_basis", "registered") ==
                bundle.get("region_basis", "registered"),
                "bundle_basis_mismatch", "bundle and report use different region bases")
        story_totals = {}
        for story in report["stories"]:
            story_totals[story["region"]] = story_totals.get(story["region"], 0) + story["observation_count"]
        for row in report["regions"]:
            require(story_totals.get(row["region"], 0) == row["observation_count"],
                    "bundle_count_mismatch", "region observation count differs from rows",
                    region=row["region"])
        categories = Counter(row["category"] for row in bundle["evidence"])
        require(sum(categories.values()) == report["observations_in_window"], "bundle_count_mismatch",
                "observation count differs from evidence")
        require(categories["eligible"] == report["eligible_observations"], "bundle_count_mismatch",
                "eligible count differs from evidence")
        for category, count in report["excluded"].items():
            require(categories[category] == count, "bundle_count_mismatch",
                    "exclusion differs from evidence", category=category)
        return {"valid": True, "bundle_hash": bundle["bundle_hash"],
                "stories": report["story_count"], "observations": sum(categories.values())}

    def source_breakdown(self, query):
        bundle = self.current(query)
        by_source = defaultdict(lambda: Counter())
        regions = defaultdict(set)
        for row in bundle["evidence"]:
            by_source[row["source_id"]][row["category"]] += 1
            regions[row["source_id"]].add(row["region"])
        result = []
        for source_id, counts in sorted(by_source.items()):
            source = self.sources.describe(source_id)
            result.append({"source_id": source_id, "outlet": source["outlet"],
                           "registered_region": source["region"],
                           "attributed_regions": sorted(regions[source_id]),
                           "region_basis": bundle["report"]["region_basis"],
                           "categories": dict(sorted(counts.items())),
                           "observations": sum(counts.values())})
        require(sum(row["observations"] for row in result) == bundle["report"]["observations_in_window"],
                "breakdown_mismatch", "source counts do not reconcile")
        return {"query_hash": bundle["report"]["query_hash"],
                "event_head": bundle["event_head"], "sources": result,
                "region_basis": bundle["report"]["region_basis"],
                "total_observations": bundle["report"]["observations_in_window"]}

    def story_evidence(self, query, region, story_key):
        spec = report_query(query)
        report = self.analytics.report(spec)
        story = [row for row in report["stories"] if row["region"] == region and row["story_key"] == story_key]
        require(story, "story_missing", "story not found")
        rows = [row for row in self._evidence_rows(report) if row["region"] == region
                and row["story_key"] == story_key]
        require(sum(row["observation_count"] for row in story) ==
                sum(row["category"] == "eligible" for row in rows),
                "story_evidence_mismatch", "story count differs from evidence")
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "region_basis": report["region_basis"],
                "story": story, "evidence": rows}


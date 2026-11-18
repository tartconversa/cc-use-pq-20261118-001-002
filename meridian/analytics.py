"""区域汇总、显式排除与故事级去重。

报告同时保留两套地区口径：来源登记的地区（"registered"）和按稿件发表时间
查询来源地区时间线得到的实际发行地区（"distribution"）。两套口径各自独立
计算分母并附有对账信息，绝不把同一条观察记录同时计入两个地区。
"""
from collections import Counter, defaultdict
from .contracts import REGION_BASES, digest, report_query
from .errors import require


class EvidenceAnalytics:
    def __init__(self, store, observations, sources, coding, frames=None):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding
        self.frames = frames

    @staticmethod
    def _basis(spec, basis=None):
        basis = basis or spec.get("region_basis", "registered")
        require(basis in REGION_BASES, "invalid_region_basis", "unsupported region basis")
        return basis

    def _attribute(self, source, published_at, basis, distribution=None):
        registered = source["region"]
        distribution = distribution or self.sources.region_at(source["source_id"], published_at)
        return {"basis": basis,
                "region": registered if basis == "registered" else distribution["region"],
                "registered_region": registered,
                "distribution_region": distribution["region"],
                "distribution_basis": distribution["basis"],
                "effective_at": distribution["effective_at"],
                "correction_id": distribution["correction_id"]}

    def _in_window(self, spec, with_attribution=False):
        rows = []
        for item in self.observations.current():
            data = item["data"]
            if not spec["start"] <= data["published_at"] < spec["end"]:
                continue
            source = self.sources.describe(data["source_id"])
            distribution = None
            if with_attribution:
                distribution = self.sources.region_at(data["source_id"], data["published_at"])
            rows.append((item, data, source, distribution))
        return rows

    def _rows(self, spec, basis=None):
        basis = self._basis(spec, basis)
        rows = []
        for item, data, source, distribution in self._in_window(spec, with_attribution=True):
            attribution = self._attribute(source, data["published_at"], basis, distribution)
            if spec["regions"] and attribution["region"] not in spec["regions"]:
                continue
            state = self.coding.status(data["origin"], data["external_id"], item["revision"])
            rows.append({"item": item, "source": source, "state": state,
                         "region": attribution["region"], "attribution": attribution})
        return rows

    def _classify(self, row, spec):
        item = row["item"]
        state = row["state"]
        if item["status"] == "retracted":
            return "retracted"
        if state["status"] == "awaiting_review":
            return "uncoded"
        if state["status"] == "disputed":
            return "disputed"
        coding = state["coding"]
        if spec["topics"] and coding["topic"] not in spec["topics"]:
            return "outside_topic"
        if spec["audiences"] and coding["audience"] not in spec["audiences"]:
            return "outside_audience"
        return "eligible"

    def _view(self, spec, basis):
        observed = self._rows(spec, basis)
        exclusions = Counter()
        groups = defaultdict(list)
        provenance = []
        for row in observed:
            category = self._classify(row, spec)
            data = row["item"]["data"]
            attribution = row["attribution"]
            provenance.append({"origin": data["origin"], "external_id": data["external_id"],
                               "revision": row["item"]["revision"],
                               "source_id": data["source_id"], "category": category,
                               "region": row["region"],
                               "registered_region": attribution["registered_region"],
                               "distribution_region": attribution["distribution_region"],
                               "region_basis": basis,
                               "region_effective_at": attribution["effective_at"],
                               "correction_id": attribution["correction_id"]})
            if category != "eligible":
                exclusions[category] += 1
                continue
            item = row["item"]["data"]
            coding = row["state"]["coding"]
            key = (row["region"], item["story_key"], coding["topic"], coding["audience"])
            groups[key].append(row)
        story_rows = []
        for (region, story, topic, audience), members in sorted(groups.items()):
            ordered = sorted(members, key=lambda row: (row["item"]["data"]["published_at"],
                                                        row["item"]["data"]["source_id"],
                                                        row["item"]["data"]["origin"],
                                                        row["item"]["data"]["external_id"]))
            stances = [row["state"]["coding"]["stance"] for row in members]
            stance = stances[0] if len(set(stances)) == 1 else "mixed"
            story_rows.append({"region": region, "story_key": story, "topic": topic,
                               "audience": audience, "stance": stance,
                               "observation_count": len(members),
                               "outlets": sorted({row["source"]["outlet"] for row in members}),
                               "representative": {"origin": ordered[0]["item"]["data"]["origin"],
                                                  "external_id": ordered[0]["item"]["data"]["external_id"]}})
        by_region = defaultdict(lambda: {"stories": 0, "stances": Counter(), "topics": Counter(),
                                         "audiences": Counter(), "observations": 0})
        for story in story_rows:
            bucket = by_region[story["region"]]
            bucket["stories"] += 1
            bucket["stances"][story["stance"]] += 1
            bucket["topics"][story["topic"]] += 1
            bucket["audiences"][story["audience"]] += 1
            bucket["observations"] += story["observation_count"]
        regions = []
        for region, data in sorted(by_region.items()):
            denominator = data["stories"]
            regions.append({"region": region, "story_count": denominator,
                            "observation_count": data["observations"],
                            "stance_counts": dict(sorted(data["stances"].items())),
                            "stance_shares": {key: round(value / denominator, 6)
                                              for key, value in sorted(data["stances"].items())},
                            "topic_counts": dict(sorted(data["topics"].items())),
                            "audience_counts": dict(sorted(data["audiences"].items()))})
        return {"region_basis": basis, "observations_in_window": len(observed),
                "eligible_observations": sum(len(value) for value in groups.values()),
                "excluded": dict(sorted(exclusions.items())), "story_count": len(story_rows),
                "stories": story_rows, "regions": regions,
                "provenance": sorted(provenance, key=lambda item: (item["origin"], item["external_id"]))}

    def _attribution_activity(self, spec):
        """比较两套地区口径，并单独列出迟到稿件；不受地区过滤器影响。"""
        changed = []
        late = []
        backfilled = []
        registered_counts = Counter()
        distribution_counts = Counter()
        for item, data, source, distribution in self._in_window(spec, with_attribution=True):
            attribution = self._attribute(source, data["published_at"], "distribution", distribution)
            registered_counts[attribution["registered_region"]] += 1
            distribution_counts[attribution["distribution_region"]] += 1
            recorded_at = None
            correction_basis = None
            if attribution["correction_id"] is not None:
                row = self.store.one(
                    "SELECT basis,recorded_at FROM source_region_corrections WHERE correction_id=?",
                    (attribution["correction_id"],))
                if row is not None:
                    recorded_at = row["recorded_at"]
                    correction_basis = row["basis"]
            next_change = self.store.one(
                "SELECT region,effective_at,recorded_at FROM source_region_corrections "
                "WHERE source_id=? AND effective_at>? ORDER BY effective_at, correction_id LIMIT 1",
                (data["source_id"], data["published_at"]))
            entry = {"origin": data["origin"], "external_id": data["external_id"],
                     "revision": item["revision"], "source_id": data["source_id"],
                     "published_at": data["published_at"], "received_at": data["received_at"],
                     "registered_region": attribution["registered_region"],
                     "distribution_region": attribution["distribution_region"],
                     "effective_at": attribution["effective_at"],
                     "correction_id": attribution["correction_id"],
                     "correction_basis": correction_basis,
                     "correction_recorded_at": recorded_at,
                     "received_after_correction": bool(
                         recorded_at and data["received_at"] > recorded_at),
                     "next_correction_effective_at": next_change["effective_at"] if next_change else None,
                     "received_after_region_notice": bool(
                         next_change and data["received_at"] > next_change["recorded_at"]),
                     "in_query_registered": not spec["regions"] or
                     attribution["registered_region"] in spec["regions"],
                     "in_query_distribution": not spec["regions"] or
                     attribution["distribution_region"] in spec["regions"]}
            if attribution["registered_region"] != attribution["distribution_region"]:
                changed.append(entry)
            if data["received_at"] >= spec["end"]:
                late.append(entry)
            if entry["received_after_region_notice"]:
                backfilled.append(entry)
        order = lambda entry: (entry["published_at"], entry["origin"], entry["external_id"])
        return {"registered_counts": dict(sorted(registered_counts.items())),
                "distribution_counts": dict(sorted(distribution_counts.items())),
                "changed": sorted(changed, key=order), "changed_count": len(changed),
                "late_arrivals": sorted(late, key=order), "late_count": len(late),
                "backfilled_after_region_notice": sorted(backfilled, key=order),
                "backfilled_count": len(backfilled)}

    def report(self, payload):
        spec = report_query(payload)
        basis = self._basis(spec)
        views = {name: self._view(spec, name) for name in sorted(REGION_BASES)}
        primary = views[basis]
        region_bases = {name: {key: view[key] for key in
                               ("region_basis", "observations_in_window", "eligible_observations",
                                "excluded", "story_count", "stories", "regions")}
                        for name, view in views.items()}
        return {**primary, "query": spec, "query_hash": digest(spec),
                "event_head": self.store.head(), "region_basis": basis,
                "region_bases": region_bases,
                "region_attribution": self._attribution_activity(spec)}

    def reconciliation(self, payload):
        """对账两套地区口径：地区省份分母差异中有多少来自归属修正。"""
        spec = report_query(payload)
        registered = self._view(spec, "registered")
        distribution = self._view(spec, "distribution")
        movement = self._attribution_activity(spec)
        left = {row["region"]: row for row in registered["regions"]}
        right = {row["region"]: row for row in distribution["regions"]}
        moved_in = Counter(entry["distribution_region"] for entry in movement["changed"])
        moved_out = Counter(entry["registered_region"] for entry in movement["changed"])
        regions = []
        for region in sorted(set(left) | set(right)):
            before = left.get(region, {})
            after = right.get(region, {})
            before_stories = before.get("story_count", 0)
            after_stories = after.get("story_count", 0)
            delta = after_stories - before_stories
            regions.append({"region": region, "registered_story_count": before_stories,
                            "distribution_story_count": after_stories,
                            "story_delta": delta,
                            "story_delta_explained_by_attribution": moved_in[region] - moved_out[region],
                            "story_delta_unexplained": delta - (moved_in[region] - moved_out[region]),
                            "registered_observation_count": before.get("observation_count", 0),
                            "distribution_observation_count": after.get("observation_count", 0),
                            "observation_delta": after.get("observation_count", 0) -
                            before.get("observation_count", 0),
                            "attributed_in": moved_in[region],
                            "attributed_out": moved_out[region]})
        moved = []
        for entry in movement["changed"]:
            moved.append({key: entry[key] for key in
                          ("origin", "external_id", "revision", "source_id", "published_at",
                           "received_at", "registered_region", "distribution_region",
                           "effective_at", "correction_id", "correction_basis",
                           "correction_recorded_at", "received_after_correction",
                           "in_query_registered", "in_query_distribution")})
        moved.sort(key=lambda row: (row["published_at"], row["origin"], row["external_id"]))
        crossed = []
        if self.frames is not None:
            for frame in self.frames.coverage(spec):
                crossed.append({"frame_id": frame["frame_id"], "source_id": frame["source_id"],
                                "state": frame["state"],
                                "expected_count": frame["expected_count"],
                                "actual_count": frame["actual_count"],
                                "region_counts": (frame["sealed_region_counts"]
                                                  if frame["state"] == "closed"
                                                  else frame["current_region_counts"]),
                                "crosses_region_correction": frame["crosses_region_correction"]})
        return {"query": spec, "query_hash": digest(spec), "event_head": self.store.head(),
                "registered": {"story_count": registered["story_count"],
                               "eligible_observations": registered["eligible_observations"],
                               "excluded": registered["excluded"],
                               "regions": registered["regions"]},
                "distribution": {"story_count": distribution["story_count"],
                                 "eligible_observations": distribution["eligible_observations"],
                                 "excluded": distribution["excluded"],
                                 "regions": distribution["regions"]},
                "regions": regions,
                "attribution_moved_observations": moved,
                "attribution_moved_count": len(moved),
                "late_arrivals": movement["late_arrivals"],
                "late_arrival_count": movement["late_count"],
                "backfilled_after_region_notice": movement["backfilled_after_region_notice"],
                "backfilled_after_region_notice_count": movement["backfilled_count"],
                "collection_frames": crossed}

    def trace_story(self, spec, region, story_key):
        report = self.report(spec)
        rows = [row for row in report["stories"] if row["region"] == region and row["story_key"] == story_key]
        require(rows, "story_missing", "story not found in query")
        observations = []
        for row in self._rows(report["query"]):
            data = row["item"]["data"]
            if row["region"] == region and data["story_key"] == story_key:
                observations.append({"origin": data["origin"], "external_id": data["external_id"],
                                     "revision": row["item"]["revision"],
                                     "region": row["region"],
                                     "registered_region": row["attribution"]["registered_region"],
                                     "distribution_region": row["attribution"]["distribution_region"],
                                     "classification": self._classify(row, report["query"]),
                                     "coding_status": row["state"]["status"],
                                     "source_id": data["source_id"]})
        return {"stories": rows, "region_basis": report["region_basis"],
                "observations": sorted(observations, key=lambda x: (x["origin"], x["external_id"]))}

    def quality(self, report, *, minimum_stories=1, maximum_exclusion_share=0.4):
        require(type(minimum_stories) is int and minimum_stories >= 1, "invalid_gate", "minimum_stories invalid")
        require(isinstance(maximum_exclusion_share, (int, float)) and 0 <= maximum_exclusion_share <= 1,
                "invalid_gate", "maximum_exclusion_share invalid")
        total = report["observations_in_window"]
        reasons = []
        if report["story_count"] < minimum_stories:
            reasons.append("insufficient_stories")
        excluded = sum(report["excluded"].values())
        if total == 0 or excluded / total > maximum_exclusion_share:
            reasons.append("excess_exclusions")
        if report["excluded"].get("disputed", 0):
            reasons.append("unresolved_disputes")
        if any(story["stance"] == "mixed" and story["observation_count"] > 1
               for story in report["stories"]):
            reasons.append("cross_source_stance_conflict")
        return {"passed": not reasons, "reasons": reasons, "total": total,
                "region_basis": report["region_basis"],
                "excluded": excluded, "minimum_stories": minimum_stories,
                "maximum_exclusion_share": maximum_exclusion_share}

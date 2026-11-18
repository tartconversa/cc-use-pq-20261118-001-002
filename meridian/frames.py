"""采集窗口：来源身份覆盖契约跨地区更正保持完整。

窗口仍按来源原有采集区间对预期稿件数负责。若来源在窗口内部更换了发行地区，
窗口不会把完整预期数重复记到两个地区；它会按稿件发表时间把窗口成员分到
各自一侧，并显式报告这一跨界情况。
"""
from collections import Counter
from .contracts import digest, text, timestamp, utc_now
from .errors import MeridianError, require


class CollectionFrames:
    def __init__(self, store, sources, observations):
        self.store = store
        self.sources = sources
        self.observations = observations

    def open(self, frame_id, source_id, start, end, expected_count):
        with self.store.transaction():
            return self._open_locked(frame_id, source_id, start, end, expected_count)

    def _open_locked(self, frame_id, source_id, start, end, expected_count):
        frame_id = text(frame_id, "frame_id")
        source_id = text(source_id, "source_id")
        start = timestamp(start, "start")
        end = timestamp(end, "end")
        require(start < end, "invalid_window", "frame start must precede end")
        require(type(expected_count) is int and expected_count >= 0, "invalid_expected_count",
                "expected_count must be nonnegative")
        self.sources.validate_for_ingest(source_id)
        existing = self.store.one("SELECT * FROM collection_frames WHERE frame_id=?", (frame_id,))
        if existing:
            same = all((existing["source_id"] == source_id, existing["start_at"] == start,
                        existing["end_at"] == end, existing["expected_count"] == expected_count))
            require(same, "frame_conflict", "frame identifier has different coverage contract")
            return self.describe(frame_id) | {"duplicate": True}
        overlapping = self.store.one(
            "SELECT frame_id FROM collection_frames WHERE source_id=? AND start_at<? AND end_at>?",
            (source_id, end, start))
        require(overlapping is None, "frame_overlap", "source coverage windows overlap")
        now = utc_now()
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO collection_frames(frame_id,source_id,start_at,end_at,expected_count,state,created_at) "
                "VALUES(?,?,?,?,?,'open',?)", (frame_id, source_id, start, end, expected_count, now))
            self.store.event("frame_opened", frame_id, digest([source_id, start, end, expected_count]), now)
        return self.describe(frame_id) | {"duplicate": False}

    def _candidates(self, frame):
        rows = self.store.all(
            "SELECT o.* FROM observations o JOIN observation_heads h ON o.origin=h.origin "
            "AND o.external_id=h.external_id AND o.revision=h.revision WHERE o.source_id=? "
            "AND o.published_at>=? AND o.published_at<? AND o.status='active' "
            "ORDER BY o.origin,o.external_id",
            (frame["source_id"], frame["start"], frame["end"]))
        return [{"origin": row["origin"], "external_id": row["external_id"],
                 "revision": row["revision"], "hash": row["payload_hash"],
                 "published_at": row["published_at"]} for row in rows]

    def _region_split(self, frame, candidates=None):
        candidates = candidates if candidates is not None else self._candidates(frame)
        counts = Counter()
        members = []
        for item in candidates:
            attribution = self.sources.region_at(frame["source_id"], item["published_at"])
            counts[attribution["region"]] += 1
            members.append({"origin": item["origin"], "external_id": item["external_id"],
                            "revision": item["revision"], "published_at": item["published_at"],
                            "region": attribution["region"],
                            "effective_at": attribution["effective_at"],
                            "correction_id": attribution["correction_id"]})
        return {"regions": dict(sorted(counts.items())), "members": members}

    def describe(self, frame_id):
        row = self.store.one("SELECT * FROM collection_frames WHERE frame_id=?", (frame_id,))
        require(row is not None, "frame_missing", "frame not found")
        data = {"frame_id": frame_id, "source_id": row["source_id"], "start": row["start_at"],
                "end": row["end_at"], "expected_count": row["expected_count"],
                "state": row["state"], "actual_count": row["actual_count"],
                "frozen_hash": row["frozen_hash"]}
        candidates = self._candidates(data)
        data["current_count"] = len(candidates)
        data["current_hash"] = digest([{key: item[key] for key in ("origin", "external_id",
                                                                   "revision", "hash")}
                                       for item in candidates])
        data["drifted"] = row["state"] == "closed" and data["current_hash"] != row["frozen_hash"]
        split = self._region_split(data, candidates)
        data["registered_region"] = self.sources.describe(row["source_id"])["region"]
        data["current_region_counts"] = split["regions"]
        data["current_region_members"] = split["members"]
        data["crosses_region_correction"] = len(split["regions"]) > 1
        if row["state"] == "closed":
            members = self.store.all(
                "SELECT origin,external_id,revision FROM frame_members WHERE frame_id=? "
                "ORDER BY origin,external_id", (frame_id,))
            sealed = Counter()
            sealed_members = []
            for member in members:
                published = self.store.one(
                    "SELECT published_at FROM observations WHERE origin=? AND external_id=? AND revision=?",
                    (member["origin"], member["external_id"], member["revision"]))
                if published is None:
                    continue
                attribution = self.sources.region_at(row["source_id"], published["published_at"])
                sealed[attribution["region"]] += 1
                sealed_members.append({"origin": member["origin"],
                                       "external_id": member["external_id"],
                                       "revision": member["revision"],
                                       "published_at": published["published_at"],
                                       "region": attribution["region"],
                                       "effective_at": attribution["effective_at"],
                                       "correction_id": attribution["correction_id"]})
            data["sealed_region_counts"] = dict(sorted(sealed.items()))
            data["sealed_region_members"] = sorted(
                sealed_members, key=lambda item: (item["origin"], item["external_id"]))
            data["crosses_region_correction"] = len(sealed) > 1
        return data

    def close(self, frame_id):
        with self.store.transaction():
            return self._close_locked(frame_id)

    def _close_locked(self, frame_id):
        frame = self.describe(frame_id)
        if frame["state"] == "closed":
            require(not frame["drifted"], "frame_drift", "closed frame changed after sealing")
            return frame | {"duplicate": True}
        require(frame["current_count"] == frame["expected_count"], "frame_incomplete",
                "observed count differs from declared coverage", expected=frame["expected_count"],
                actual=frame["current_count"])
        now = utc_now()
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE collection_frames SET state='closed',actual_count=?,frozen_hash=?,closed_at=? WHERE frame_id=?",
                (frame["current_count"], frame["current_hash"], now, frame_id))
            for item in self._candidates(frame):
                self.store.db.execute(
                    "INSERT INTO frame_members(frame_id,origin,external_id,revision) VALUES(?,?,?,?)",
                    (frame_id, item["origin"], item["external_id"], item["revision"]))
            self.store.event("frame_closed", frame_id, frame["current_hash"], now)
        return self.describe(frame_id) | {"duplicate": False}

    def coverage(self, spec):
        """来源身份不受地区更正影响；服务过查询地区的来源仍纳入覆盖检查。"""
        sources = [row for row in self.sources.list()
                   if not spec["regions"]
                   or row["region"] in spec["regions"]
                   or any(region in spec["regions"]
                          for region in self.sources.region_summary(row["source_id"])["distribution_regions"])]
        result = []
        for source in sources:
            frames = self.store.all(
                "SELECT frame_id FROM collection_frames WHERE source_id=? AND start_at>=? AND end_at<=? "
                "ORDER BY start_at", (source["source_id"], spec["start"], spec["end"]))
            for row in frames:
                result.append(self.describe(row["frame_id"]))
        return result

    def quality(self, spec):
        frames = self.coverage(spec)
        problems = []
        for frame in frames:
            if frame["state"] != "closed":
                problems.append({"frame_id": frame["frame_id"], "reason": "open",
                                 "region_counts": frame["current_region_counts"]})
            elif frame["drifted"]:
                problems.append({"frame_id": frame["frame_id"], "reason": "drifted",
                                 "region_counts": frame["sealed_region_counts"]})
        return {"frames": frames, "problems": problems, "passed": not problems,
                "crossing_frames": [frame["frame_id"] for frame in frames
                                    if frame["state"] == "closed"
                                    and frame["crosses_region_correction"]]}

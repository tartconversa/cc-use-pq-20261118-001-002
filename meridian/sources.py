"""来源登记限制地区、允许词表和带生效时间的地区更正。"""
from .contracts import digest, region_correction, source, timestamp, utc_now
from .errors import MeridianError, require
from .storage import decode, encode


class SourceRegistry:
    def __init__(self, store):
        self.store = store

    def register(self, payload):
        with self.store.transaction():
            return self._register_locked(payload)

    def _register_locked(self, payload):
        data = source(payload)
        existing = self.store.one("SELECT * FROM sources WHERE source_id=?", (data["source_id"],))
        if existing:
            current = self.describe(data["source_id"])
            if all(current[key] == data[key] for key in data):
                return {**current, "duplicate": True}
            raise MeridianError("source_conflict", "source identifier already has different metadata")
        now = utc_now()
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO sources(source_id,region,outlet,topics,audiences,active,created_at) VALUES(?,?,?,?,?,1,?)",
                (data["source_id"], data["region"], data["outlet"],
                 encode(data["topics"]), encode(data["audiences"]), now),
            )
            self.store.event("source_registered", data["source_id"], digest(data), now)
        return {**data, "active": True, "version": 1, "duplicate": False}

    def describe(self, source_id):
        row = self.store.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
        require(row is not None, "source_missing", "source is not registered", source_id=source_id)
        return {"source_id": row["source_id"], "region": row["region"],
                "outlet": row["outlet"], "topics": decode(row["topics"]),
                "audiences": decode(row["audiences"]), "active": bool(row["active"]),
                "version": row["version"]}

    def list(self):
        return [self.describe(row["source_id"]) for row in self.store.all("SELECT source_id FROM sources ORDER BY source_id")]

    def deactivate(self, source_id):
        with self.store.transaction():
            return self._deactivate_locked(source_id)

    def _deactivate_locked(self, source_id):
        source_data = self.describe(source_id)
        if not source_data["active"]:
            return {**source_data, "duplicate": True}
        now = utc_now()
        with self.store.transaction():
            self.store.db.execute("UPDATE sources SET active=0,version=version+1 WHERE source_id=?", (source_id,))
            self.store.event("source_deactivated", source_id, digest(source_data), now)
        return {**self.describe(source_id), "duplicate": False}

    def validate_for_ingest(self, source_id):
        data = self.describe(source_id)
        require(data["active"], "source_inactive", "source no longer accepts observations")
        return data

    def validate_coding(self, source_id, topic, audience):
        data = self.describe(source_id)
        require(topic in data["topics"], "topic_outside_source", "topic is not in source vocabulary")
        require(audience in data["audiences"], "audience_outside_source", "audience is not in source vocabulary")
        return data

    def correct_region(self, payload):
        with self.store.transaction():
            return self._correct_region_locked(payload)

    def _correct_region_locked(self, payload):
        data = region_correction(payload)
        source_data = self.describe(data["source_id"])
        existing = self.store.all(
            "SELECT * FROM source_region_corrections WHERE source_id=? ORDER BY effective_at",
            (data["source_id"],))
        for row in existing:
            if row["effective_at"] != data["effective_at"]:
                continue
            same = (row["region"] == data["region"] and row["basis"] == data["basis"]
                    and row["reason"] == data["reason"])
            if same and (data.get("correction_id") is None
                         or data["correction_id"] == row["correction_id"]):
                return self._describe_correction(row["correction_id"]) | {"duplicate": True}
            raise MeridianError("region_correction_conflict",
                                "source already has a different correction at this effective time",
                                details={"source_id": data["source_id"],
                                         "effective_at": data["effective_at"]})
        before = self._region_before(source_data, existing, data["effective_at"])
        require(before["region"] != data["region"], "region_correction_noop",
                "correction does not change the region in force at that time",
                source_id=data["source_id"], effective_at=data["effective_at"])
        if data.get("correction_id") is not None:
            require(self.store.one("SELECT 1 FROM source_region_corrections WHERE correction_id=?",
                                   (data["correction_id"],)) is None,
                    "region_correction_conflict", "correction identifier already used")
        now = utc_now()
        with self.store.transaction():
            event_no = self.store.event("source_region_corrected", data["source_id"],
                                        digest(data), now)
            correction_id = data.get("correction_id") or "region-" + digest(
                [data["source_id"], data["effective_at"]])[:16]
            self.store.db.execute(
                "INSERT INTO source_region_corrections(correction_id,source_id,region,effective_at,"
                "basis,reason,recorded_at,event_no) VALUES(?,?,?,?,?,?,?,?)",
                (correction_id, data["source_id"], data["region"], data["effective_at"],
                 data["basis"], data["reason"], now, event_no))
        return self._describe_correction(correction_id) | {"duplicate": False}

    @staticmethod
    def _region_before(source_data, corrections, effective_at):
        region = source_data["region"]
        chosen = {"region": region, "basis": "registered", "effective_at": None,
                  "correction_id": None}
        for row in corrections:
            if row["effective_at"] >= effective_at:
                break
            region = row["region"]
            chosen = {"region": region, "basis": "correction",
                      "effective_at": row["effective_at"],
                      "correction_id": row["correction_id"]}
        return chosen

    def _describe_correction(self, correction_id):
        row = self.store.one("SELECT * FROM source_region_corrections WHERE correction_id=?",
                             (correction_id,))
        require(row is not None, "region_correction_missing", "region correction not found")
        return {key: row[key] for key in ("correction_id", "source_id", "region", "effective_at",
                                          "basis", "reason", "recorded_at", "event_no")}

    def corrections(self, source_id):
        self.describe(source_id)
        rows = self.store.all(
            "SELECT correction_id FROM source_region_corrections WHERE source_id=? "
            "ORDER BY effective_at, correction_id", (source_id,))
        return [self._describe_correction(row["correction_id"]) for row in rows]

    def region_timeline(self, source_id):
        """登记口径注册地区，随后按 effective_at 排序的实际发行地区区段。"""
        data = self.describe(source_id)
        segments = [{"region": data["region"], "basis": "registered", "effective_at": None,
                     "correction_id": None, "recorded_at": None}]
        for row in self.corrections(source_id):
            segments.append({"region": row["region"], "basis": "correction",
                             "effective_at": row["effective_at"],
                             "correction_id": row["correction_id"],
                             "recorded_at": row["recorded_at"]})
        return segments

    def region_at(self, source_id, published_at):
        """按稿件发表时间归属地区；录入时间不参与判断。"""
        published_at = timestamp(published_at, "at")
        data = self.describe(source_id)
        chosen = {"region": data["region"], "basis": "registered", "effective_at": None,
                  "correction_id": None}
        for row in self.store.all(
                "SELECT * FROM source_region_corrections WHERE source_id=? AND effective_at<=? "
                "ORDER BY effective_at, correction_id", (source_id, published_at)):
            chosen = {"region": row["region"], "basis": "correction",
                      "effective_at": row["effective_at"],
                      "correction_id": row["correction_id"]}
        return chosen

    def region_summary(self, source_id):
        data = self.describe(source_id)
        timeline = self.region_timeline(source_id)
        regions = []
        for segment in timeline:
            if segment["region"] not in regions:
                regions.append(segment["region"])
        return {"source_id": source_id, "registered_region": data["region"],
                "distribution_regions": regions,
                "corrected": len(timeline) > 1, "timeline": timeline,
                "corrections": self.corrections(source_id)}


"""Bounded history of complete live snapshots, never notification arrival rates."""
import json
import sqlite3
import statistics
import math
import time
from collections import Counter
from pathlib import Path


class AlertHistory:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("CREATE TABLE IF NOT EXISTS snapshots (cluster TEXT, scope TEXT, bucket INTEGER, at REAL, scan TEXT, counts TEXT, PRIMARY KEY(cluster,scope,bucket))")
        return conn

    @staticmethod
    def eligible(scan):
        return (scan.get("origin") == "live_scan" and bool(scan.get("cluster_id"))
                and not scan.get("degraded") and scan.get("revision", 1) == 1 and not scan.get("history") and scan.get("collection")
                and all(v == "ok" for v in scan["collection"].values()))

    def apply(self, scan, scope, now=None):
        now = time.time() if now is None else now
        if not self.eligible(scan):
            scan["automatic_baseline"] = {"status": "ineligible", "reason": "complete live scans required"}
            return scan
        with self._connect() as conn:
            rows = conn.execute("SELECT scan,counts FROM snapshots WHERE cluster=? AND scope=? AND bucket<? AND at>=? ORDER BY bucket DESC LIMIT 288",
                                (scan["cluster_id"], scope, int(now//300), now-86400)).fetchall()
        version = scan.get("signature_version", 1)
        rows = [r for r in rows if json.loads(r[1]).get("__signature_version__", 1) == version]
        scan["automatic_baseline"] = {"status": "ready" if len(rows) >= 3 else "warming_up",
            "signature_version": version,
            "snapshots": len(rows), "minimum_snapshots": 3, "bucket_s": 300, "lookback_s": 86400,
            "scope": scope, "measure": "records_per_complete_scan_not_arrival_rate", "scan_ids": [r[0] for r in rows]}
        if len(rows) < 3:
            return scan
        histories = [json.loads(r[1]) for r in rows]
        current = Counter()
        for group in scan["groups"]:
            current[group["template_id"]] += group["alert_count"]
        for group in scan["groups"]:
            values = [h.get(group["template_id"], 0) for h in histories]
            expected = statistics.mean(values)
            deviation = max(0, (current[group["template_id"]]-expected)/max(statistics.pstdev(values), math.sqrt(max(expected, 1))))
            group["frequency"] = {"status": "automatic_snapshot_baseline", "measure": "records_per_complete_scan",
                "observed_records": current[group["template_id"]], "expected_records": expected, "deviation_score": round(deviation, 3)}
            group["priority"]["frequency_score"] = deviation
        scan["ranking"] = [g["group_id"] for g in sorted(scan["groups"], key=lambda g:
            (g["priority"]["severity_rank"], g["priority"]["frequency_score"], g["priority"]["rare"]), reverse=True)]
        return scan

    def bootstrap(self, directory, cluster, scope, now=None):
        """Migrate only provably complete stored live scans; never synthesize data."""
        now = time.time() if now is None else now
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM snapshots WHERE cluster=? AND scope=? LIMIT 1", (cluster, scope)).fetchone():
                return
        paths = sorted(Path(directory).glob("scan-*.json"), key=lambda p:p.stat().st_mtime, reverse=True)[:100]
        for path in reversed(paths):
            try:
                scan = json.loads(path.read_text())
                at = scan.get("created_at", 0)
                if scan.get("cluster_id") != cluster or not now-86400 <= at < now or not scan.get("groups"):
                    continue
                explicit = scan.get("origin") == "live_scan" and scan.get("scan_scope") == scope
                legacy_all = (not scope and scan.get("cluster_kind") == "general"
                              and set(scan.get("collection", {})) == {"kubectl", "prometheus"}
                              and len({g["namespace"] for g in scan["groups"]}) > 1)
                if not (explicit or legacy_all) or any("template_id" not in g for g in scan["groups"]):
                    continue
                scan["origin"] = "live_scan"
                self.record(scan, scope, now=at)
            except (ValueError, TypeError, KeyError, OSError):
                continue

    def record(self, scan, scope, now=None):
        if not self.eligible(scan):
            return
        now = time.time() if now is None else now
        counts = Counter()
        for group in scan["groups"]:
            counts[group["template_id"]] += group["alert_count"]
        counts["__signature_version__"] = scan.get("signature_version", 1)
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
                         (scan["cluster_id"], scope, int(now//300), now, scan["scan_id"], json.dumps(counts)))
            conn.execute("DELETE FROM snapshots WHERE at<?", (now-7*86400,))

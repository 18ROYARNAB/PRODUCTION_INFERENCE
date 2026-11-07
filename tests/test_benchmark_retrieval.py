import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.benchmark_retrieval import (
    BenchmarkRecord,
    iter_alternating_records,
    summarize_ranks,
)


def nvd_record(
    cve_id: str, severity: str = "HIGH", description: str | None = None
) -> dict[str, object]:
    return {
        "cve": {
            "id": cve_id,
            "descriptions": [
                {
                    "lang": "en",
                    "value": description or f"Description for {cve_id}",
                }
            ],
            "metrics": {
                "cvssMetricV31": [{"cvssData": {"baseSeverity": severity}}]
            },
        }
    }


class AlternatingNvdRecordTests(unittest.TestCase):
    def write_dataset(self, path: Path) -> None:
        payload = {
            "vulnerabilities": [
                nvd_record("CVE-2026-0001", "LOW"),
                nvd_record("CVE-2026-0002", "UNKNOWN"),
                {
                    "cve": {
                        "id": "CVE-2026-0003",
                        "descriptions": [{"lang": "fr", "value": "French only"}],
                    }
                },
                nvd_record(
                    "CVE-2026-0004",
                    "MEDIUM",
                    "Details mention CVE-2026-0004 but hide identifier.",
                ),
                nvd_record("CVE-2026-0005", "CRITICAL"),
            ]
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_selects_every_other_ingestable_searchable_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nvd.json"
            self.write_dataset(path)

            records = list(
                iter_alternating_records(
                    path,
                    offset=0,
                    limit=10,
                    allowed_severities={"LOW", "MEDIUM", "HIGH", "CRITICAL"},
                )
            )

        self.assertEqual(
            [record.cve_id for record in records],
            ["CVE-2026-0001", "CVE-2026-0005"],
        )

    def test_reads_compressed_json_and_supports_alternate_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nvd.json.gz"
            raw = json.dumps(
                {
                    "vulnerabilities": [
                        nvd_record(f"CVE-2026-{index:04d}")
                        for index in range(1, 5)
                    ]
                }
            ).encode()
            with gzip.open(path, "wb") as output:
                output.write(raw)

            records = list(
                iter_alternating_records(
                    path,
                    offset=1,
                    limit=10,
                    allowed_severities={"HIGH"},
                )
            )

        self.assertEqual(
            [record.cve_id for record in records],
            ["CVE-2026-0002", "CVE-2026-0004"],
        )

    def test_benchmark_query_removes_cve_identifier(self) -> None:
        record = BenchmarkRecord(
            "CVE-2026-0001",
            "See CVE-2026-0001 for details; affects a server component.",
            "HIGH",
        )

        self.assertEqual(
            record.query,
            "for details; a server",
        )
        self.assertEqual(
            record.point_id,
            "9ec6f1c2-ac94-51a2-a584-3ab902b5bd0d",
        )


class RetrievalMetricTests(unittest.TestCase):
    def test_summarizes_recall_mrr_and_latency(self) -> None:
        summary = summarize_ranks([1, 3, None], [10.0, 20.0, 30.0], 3)

        self.assertEqual(summary["recall_at_3"], 0.6667)
        self.assertEqual(summary["mrr"], 0.4444)
        self.assertEqual(summary["top1_accuracy"], 0.3333)
        self.assertEqual(summary["mean_latency_ms"], 20.0)
        self.assertEqual(summary["p50_latency_ms"], 20.0)
        self.assertEqual(summary["p95_latency_ms"], 30.0)

    def test_empty_summary_has_null_quality_metrics(self) -> None:
        summary = summarize_ranks([], [], 5)

        self.assertIsNone(summary["recall_at_5"])
        self.assertIsNone(summary["mrr"])
        self.assertIsNone(summary["mean_latency_ms"])


if __name__ == "__main__":
    unittest.main()

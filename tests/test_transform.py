"""Run with: python -m unittest discover tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import transform  # noqa: E402
from ingest import TABLES  # noqa: E402


class ResolveLatestRawFiles(unittest.TestCase):
    """Picks one whole pull per dataset - every chunk of it, and only the newest day."""

    def resolve(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            for record in records:
                (raw / record["output_file"]).touch()
            manifest = raw / "_manifest.json"
            manifest.write_text(json.dumps(records), encoding="utf-8")
            with mock.patch.object(transform, "RAW_DATA_DIR", raw), \
                 mock.patch.object(transform, "MANIFEST_PATH", manifest):
                return {name: [p.name for p in paths]
                        for name, paths in transform.resolve_latest_raw_files().items()}

    def records(self, day, lfs_seconds=("07:03:44", "07:03:44")):
        out = [{"name": spec.name, "chunk": 1, "output_file": f"{spec.name}_{day}.csv",
                "retrieved_at_utc": f"{day}T07:03:44+00:00"}
               for spec in TABLES if spec.name != "lfs_underemployment"]
        out += [{"name": "lfs_underemployment", "chunk": chunk,
                 "output_file": f"lfs_{day}_part{chunk}.csv",
                 "retrieved_at_utc": f"{day}T{second}+00:00"}
                for chunk, second in enumerate(lfs_seconds, start=1)]
        return out

    def test_keeps_every_chunk_when_chunks_are_a_second_apart(self):
        files = self.resolve(self.records("2026-08-02", ("07:03:44", "07:03:45")))
        self.assertEqual(files["lfs_underemployment"],
                         ["lfs_2026-08-02_part1.csv", "lfs_2026-08-02_part2.csv"])

    def test_ignores_an_older_pull(self):
        files = self.resolve(self.records("2026-07-01") + self.records("2026-08-02"))
        self.assertEqual(files["lfs_underemployment"],
                         ["lfs_2026-08-02_part1.csv", "lfs_2026-08-02_part2.csv"])
        self.assertEqual(files["qna_gdp_growth"], ["qna_gdp_growth_2026-08-02.csv"])


if __name__ == "__main__":
    unittest.main()

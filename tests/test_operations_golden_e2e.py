from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from scripts.operations_golden_e2e import run_golden


class OperationsGoldenE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_operating_loop_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            report = await run_golden(Path(raw_root), iterations=5)

            self.assertTrue(report["passed"], report["invariants"])
            self.assertTrue(all(report["invariants"].values()))
            self.assertEqual(report["counts"]["goals"], 5)
            self.assertEqual(report["counts"]["outbox_delivered"], 5)
            self.assertEqual(report["routing_summary"]["eligible_route_count"], 1)
            self.assertTrue(Path(report["report_path"]).is_file())


if __name__ == "__main__":
    unittest.main()

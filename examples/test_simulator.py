"""Small regression suite: run python3 examples/test_simulator.py."""

import copy
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evaluate_hypothetical_traces import evaluate, load_model
from generate_hypothetical_traces import generate


ROOT = Path(__file__).resolve().parent


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "experiment.json").read_text())

    def replay(self, config=None, policy="last_window_hotness"):
        config = config or self.config
        rows = generate(config)
        scenario = next(iter(config["scenarios"]))
        return evaluate([r for r in rows if r["scenario"] == scenario], policy, load_model(config))

    def test_original_examples(self):
        expected = {"persistent_hotspot": (128, 96), "brief_hotspot": (38, 74), "shifting_hotspot": (96, 96)}
        rows = generate(self.config)
        for scenario, totals in expected.items():
            for policy, total in zip(("static", "last_window_hotness"), totals):
                summary, _ = evaluate([r for r in rows if r["scenario"] == scenario], policy, load_model(self.config))
                self.assertEqual(summary["total_us"], total)

    def test_multiple_replacements_and_capacity(self):
        config = json.loads((ROOT / "experiment_multi_page.json").read_text())
        static, _ = self.replay(config, "static")
        result, log = self.replay(config)
        self.assertEqual(static["total_us"], 241.5)
        self.assertEqual(result["total_us"], 207.5)
        self.assertEqual((result["promotions"], result["demotions"]), (2, 2))
        self.assertEqual(json.loads(log[2]["dram_pages_during_window"]), ["A", "B"])
        self.assertEqual(json.loads(log[3]["dram_pages_during_window"]), ["D", "E"])
        for row in log:
            self.assertLessEqual(row["dram_bytes_during_window"], config["model"]["dram_capacity_bytes"])
            self.assertLessEqual(row["cxl_bytes_during_window"], config["model"]["cxl_capacity_bytes"])

    def test_budget_limits_multiple_moves(self):
        config = json.loads((ROOT / "experiment_multi_page.json").read_text())
        config["model"]["migration_budget_bytes"] = 8192
        result, log = self.replay(config)
        self.assertEqual(result["total_us"], 221.5)
        self.assertEqual(log[2]["action_after_window"], "demote A; promote D; blocked: migration budget")
        self.assertEqual(log[3]["action_after_window"], "demote B; promote E")

    def test_free_slot_needs_only_promotion(self):
        self.config["model"].update(dram_capacity_bytes=8192, cxl_capacity_bytes=4096, migration_budget_bytes=4096)
        result, log = self.replay()
        self.assertEqual(result["total_us"], 78)
        self.assertEqual((result["promotions"], result["demotions"]), (1, 0))
        self.assertEqual(log[0]["action_after_window"], "promote B")

    def test_full_tiers_block_swap_without_headroom(self):
        self.config["model"]["cxl_capacity_bytes"] = 4096
        result, log = self.replay()
        self.assertEqual(result["total_us"], 128)
        self.assertEqual(result["migration_payload_bytes"], 0)
        self.assertIn("headroom", log[0]["action_after_window"])

    def test_zero_budget(self):
        self.config["model"]["migration_budget_bytes"] = 0
        self.assertEqual(self.replay()[0]["total_us"], 128)

    def test_zero_dram_and_all_dram(self):
        self.config["model"].update(dram_capacity_bytes=0, initial_dram_pages=[])
        self.assertEqual(self.replay()[0]["total_us"], 144)
        self.config["model"].update(dram_capacity_bytes=8192, cxl_capacity_bytes=0, initial_dram_pages=["A", "B"])
        result, _ = self.replay()
        self.assertEqual(result["total_us"], 48)
        self.assertEqual(result["promotions"], 0)

    def test_ties_and_final_window_do_not_move(self):
        self.config["scenarios"] = {"tie": [{"windows": 2, "requests": {"A": 100, "B": 100}}]}
        self.assertEqual(self.replay()[0]["promotions"], 0)
        self.config["scenarios"] = {"last": [{"windows": 1, "requests": {"A": 0, "B": 100}}]}
        self.assertEqual(self.replay()[0]["promotions"], 0)

    def test_invalid_population_and_counts(self):
        for field, value in (("dram_capacity_bytes", 4095), ("cxl_capacity_bytes", 0), ("initial_dram_pages", ["A", "A"])):
            config = copy.deepcopy(self.config)
            config["model"][field] = value
            with self.assertRaises(ValueError):
                load_model(config)
        self.config["scenarios"]["persistent_hotspot"][0]["requests"].pop("B")
        with self.assertRaises(ValueError):
            generate(self.config)

    def test_cli_and_mismatched_trace_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            trace = directory / "trace.csv"
            config = ROOT / "experiment_multi_page.json"
            subprocess.run([sys.executable, str(ROOT / "generate_hypothetical_traces.py"), "--config", str(config), "--output", str(trace)], check=True, capture_output=True)
            command = [sys.executable, str(ROOT / "evaluate_hypothetical_traces.py"), "--trace", str(trace), "--output-dir", str(directory), "--config"]
            subprocess.run(command + [str(config)], check=True, capture_output=True)
            with (directory / "hypothetical_results.csv").open() as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)
            mismatch = subprocess.run(command + [str(ROOT / "experiment.json")], capture_output=True, text=True)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("Trace columns must match", mismatch.stderr)


if __name__ == "__main__":
    unittest.main()

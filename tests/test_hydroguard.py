from __future__ import annotations

import sys
import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hydroguard import HydroGuardConfig, HydroGuardPipeline
from run_demo import build_demo


class HydroGuardTests(unittest.TestCase):
    def _run(self, structural_risk: bool):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.csv"
            build_demo(structural_risk).to_csv(source, index=False)
            config = HydroGuardConfig(model_backend="robust", anomaly_threshold=0.68)
            result = HydroGuardPipeline(config).run(source, root / "out")
            self.assertEqual(result["input_rows"], 720)
            self.assertTrue((root / "out" / "scored_data.csv").exists())
            self.assertTrue((root / "out" / "repair_audit.csv").exists())
            self.assertTrue((root / "out" / "quality_report.md").exists())
            self.assertIsNotNone(result["demo_metrics"])
            self.assertTrue(HydroGuardPipeline._load(root / "out" / "scored_data.csv")["anomaly_score"].between(0, 1).all())
            return result

    def test_normal_path_produces_scores_and_repairs(self):
        result = self._run(False)
        self.assertIn(result["decision"], {"approved_with_repairs", "approved"})
        self.assertGreater(result["anomaly_rows"], 0)
        self.assertGreaterEqual(result["repairs_applied"], 2)
        self.assertGreater(result["demo_metrics"]["recall"], 0.5)

    def test_structural_risk_is_routed_to_review(self):
        result = self._run(True)
        self.assertEqual(result["decision"], "needs_human_review")
        self.assertIn("duplicate_timestamp", result["review_reasons"])

    def test_required_columns_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.csv"
            build_demo(False).drop(columns=["water_level_m"]).to_csv(source, index=False)
            with self.assertRaises(ValueError):
                HydroGuardPipeline().run(source, Path(tmp) / "out")

    def test_invalid_and_missing_timestamps_require_review_with_source_rows(self):
        for bad_value, issue_type in [(None, "missing_timestamp"), ("not-a-date", "invalid_timestamp")]:
            with self.subTest(issue_type=issue_type), tempfile.TemporaryDirectory() as tmp:
                frame = build_demo(False)
                frame["timestamp"] = frame["timestamp"].astype(object)
                frame.at[5, "timestamp"] = bad_value
                source = Path(tmp) / "input.csv"
                frame.to_csv(source, index=False)
                pipeline = HydroGuardPipeline(HydroGuardConfig(model_backend="robust"))
                result = pipeline.run(source, Path(tmp) / "out")
                self.assertEqual(result["decision"], "needs_human_review")
                self.assertIn(issue_type, result["review_reasons"])
                issue = next(i for i in pipeline.state["issues"] if i["issue_type"] == issue_type)
                self.assertEqual(issue["source_row"], 7)
                self.assertEqual(pipeline.scored.iloc[issue["row_position"]]["anomaly_type"], issue_type)

    def test_boundary_missing_values_are_not_extrapolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = build_demo(False)
            frame.loc[[0, len(frame) - 1], "water_level_m"] = np.nan
            source = Path(tmp) / "input.csv"
            frame.to_csv(source, index=False)
            pipeline = HydroGuardPipeline(HydroGuardConfig(model_backend="robust"))
            result = pipeline.run(source, Path(tmp) / "out")
            self.assertTrue(pd.isna(pipeline.repaired.iloc[0]["water_level_m"]))
            self.assertTrue(pd.isna(pipeline.repaired.iloc[-1]["water_level_m"]))
            self.assertEqual(result["decision"], "needs_human_review")

    def test_missing_values_across_timestamp_gaps_are_not_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = build_demo(False)
            frame["timestamp"] = [stamp if pos < 310 else stamp + pd.Timedelta(hours=10)
                                  for pos, stamp in enumerate(frame["timestamp"])]
            source = Path(tmp) / "input.csv"
            frame.to_csv(source, index=False)
            pipeline = HydroGuardPipeline(HydroGuardConfig(model_backend="robust"))
            pipeline.run(source, Path(tmp) / "out")
            self.assertTrue(pipeline.repaired.loc[310:311, "water_level_m"].isna().all())

    def test_integer_labels_do_not_leak_into_features(self):
        predictions = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = build_demo(False)
            for name, sample in [("with_label", frame.assign(injected_anomaly=frame["injected_anomaly"].astype(int))),
                                 ("without_label", frame.drop(columns=["injected_anomaly"]))]:
                source = root / f"{name}.csv"
                sample.to_csv(source, index=False)
                pipeline = HydroGuardPipeline(HydroGuardConfig(model_backend="robust"))
                pipeline.run(source, root / name)
                self.assertFalse(any("injected_anomaly" in c for c in pipeline.state["feature_columns"]))
                predictions.append(pipeline.scored["anomaly_score"].to_numpy())
            np.testing.assert_array_equal(*predictions)

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn is not installed")
    def test_isolation_forest_backend_runs_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.csv"
            build_demo(False).to_csv(source, index=False)
            pipeline = HydroGuardPipeline(HydroGuardConfig(model_backend="isolation_forest"))
            result = pipeline.run(source, Path(tmp) / "out")
            self.assertEqual(result["model_backend_used"], "IsolationForest")
            self.assertNotIn("model_fallback_reason", pipeline.state)
            self.assertTrue(pipeline.scored["anomaly_score"].between(0, 1).all())


if __name__ == "__main__":
    unittest.main()

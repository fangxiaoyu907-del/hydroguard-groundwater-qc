"""HydroGuard: auditable anomaly detection for groundwater monitoring data.

The pipeline combines temporal features, an optional Isolation Forest model,
robust statistics and domain rules.  It only repairs low-risk issues and sends
structural risks to human review.  All decisions are written to audit files.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class HydroGuardConfig:
    timestamp_column: str = "timestamp"
    target_column: str = "water_level_m"
    label_column: str = "injected_anomaly"
    max_auto_missing_gap: int = 3
    anomaly_threshold: float = 0.72
    flatline_window: int = 8
    expected_interval_minutes: Optional[float] = 60.0
    interval_tolerance: float = 1.6
    contamination: float = 0.035
    random_state: int = 42
    model_backend: str = "auto"  # auto, isolation_forest, robust


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


class HydroGuardPipeline:
    """Fit, score, repair and report a groundwater time-series document."""

    def __init__(self, config: Optional[HydroGuardConfig] = None):
        self.config = config or HydroGuardConfig()
        self.data = pd.DataFrame()
        self.scored = pd.DataFrame()
        self.repaired = pd.DataFrame()
        self.state: Dict[str, Any] = {
            "visited_steps": [],
            "issues": [],
            "audit": [],
            "review_reasons": [],
            "decision": "pending",
        }

    def _visit(self, name: str) -> None:
        self.state["visited_steps"].append(name)

    @staticmethod
    def _load(path: Path) -> pd.DataFrame:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return pd.read_csv(path)
        if suffix in {".xlsx", ".xls"}:
            return pd.read_excel(path)
        if suffix == ".json":
            try:
                return pd.read_json(path)
            except ValueError:
                return pd.read_json(path, lines=True)
        raise ValueError("支持的输入格式：CSV、XLSX/XLS、JSON")

    def ingest(self, input_path: str | Path) -> None:
        self._visit("ingest")
        path = Path(input_path)
        if not path.exists():
            raise FileNotFoundError(path)
        frame = self._load(path)
        if frame.empty:
            raise ValueError("输入文件不包含数据行")
        required = {self.config.timestamp_column, self.config.target_column}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError("缺少必要字段：" + ", ".join(sorted(missing)))
        frame = frame.copy()
        frame["_source_row"] = np.arange(len(frame)) + 2
        parsed = pd.to_datetime(frame[self.config.timestamp_column], errors="coerce")
        timestamp_issues = {
            int(frame.iloc[pos]["_source_row"]): (
                "missing_timestamp" if pd.isna(frame.iloc[pos][self.config.timestamp_column]) else "invalid_timestamp"
            )
            for pos in np.flatnonzero(parsed.isna().to_numpy())
        }
        frame[self.config.timestamp_column] = parsed
        self.data = frame.sort_values(self.config.timestamp_column, kind="mergesort", na_position="last").reset_index(drop=True)
        for pos, row in self.data.iterrows():
            issue_type = timestamp_issues.get(int(row["_source_row"]))
            if issue_type:
                self._add_issue(issue_type, int(pos), self.config.timestamp_column, "high", "时间戳缺失或无法解析")
                self.state["review_reasons"].append(issue_type)
        self.state.update({
            "input_file": str(path),
            "input_rows": int(len(frame)),
            "input_columns": [str(c) for c in frame.columns if c != "_source_row"],
        })

    def _add_issue(self, issue_type: str, row_position: int, column: str, severity: str, reason: str, **extra: Any) -> None:
        row = self.data.iloc[row_position] if not self.data.empty and row_position < len(self.data) else None
        item: Dict[str, Any] = {
            "issue_type": issue_type,
            "row_position": int(row_position),
            "source_row": int(row["_source_row"]) if row is not None else None,
            "column": column,
            "severity": severity,
            "reason": reason,
        }
        item.update(extra)
        self.state["issues"].append(item)

    @staticmethod
    def _robust_z(values: pd.Series) -> pd.Series:
        numeric = pd.to_numeric(values, errors="coerce")
        median = numeric.median()
        mad = (numeric - median).abs().median()
        if pd.isna(mad) or mad < 1e-12:
            return pd.Series(np.zeros(len(numeric)), index=numeric.index, dtype=float)
        # Missing observations have their own rule; keep score fusion finite.
        return ((numeric - median).abs() / (1.4826 * mad)).clip(upper=30).fillna(0.0)

    def build_features(self) -> Tuple[pd.DataFrame, List[str]]:
        self._visit("feature_engineering")
        # Evaluation labels and prior pipeline outputs must never become features.
        excluded = {"_source_row", self.config.label_column, "model_score", "anomaly_score", "is_anomaly", "anomaly_type"}
        numeric_columns = [str(c) for c in self.data.select_dtypes(include=np.number).columns if c not in excluded]
        if self.config.target_column not in numeric_columns:
            numeric_columns.append(self.config.target_column)
        features = pd.DataFrame(index=self.data.index)
        for column in numeric_columns:
            series = pd.to_numeric(self.data[column], errors="coerce")
            rolling_median = series.rolling(9, center=True, min_periods=3).median()
            rolling_mad = (series - rolling_median).abs().rolling(9, center=True, min_periods=3).median()
            features[f"{column}__level"] = series
            features[f"{column}__diff1"] = series.diff()
            features[f"{column}__residual"] = series - rolling_median
            features[f"{column}__local_score"] = (series - rolling_median).abs() / (1.4826 * rolling_mad + 1e-6)
        if not features.empty:
            features = features.replace([np.inf, -np.inf], np.nan)
            for column in features.columns:
                median = features[column].median()
                features[column] = features[column].fillna(0.0 if pd.isna(median) else median)
        self.state["feature_columns"] = list(features.columns)
        return features, numeric_columns

    def _model_score(self, features: pd.DataFrame) -> Tuple[np.ndarray, str]:
        backend = self.config.model_backend
        if backend not in {"auto", "isolation_forest", "robust"}:
            raise ValueError("model_backend 必须为 auto、isolation_forest 或 robust")
        if backend in {"auto", "isolation_forest"}:
            try:
                from sklearn.ensemble import IsolationForest

                model = IsolationForest(
                    n_estimators=240,
                    contamination=self.config.contamination,
                    random_state=self.config.random_state,
                    n_jobs=-1,
                )
                raw = -model.fit(features).decision_function(features)
                low, high = np.quantile(raw, [0.05, 0.995])
                scaled = np.clip((raw - low) / (high - low + 1e-12), 0, 1)
                return scaled, "IsolationForest"
            except Exception as exc:
                if backend == "isolation_forest":
                    raise RuntimeError("IsolationForest 不可用，请安装 requirements.txt 中的依赖") from exc
                self.state["model_fallback_reason"] = f"{type(exc).__name__}: {exc}"

        robust_columns = [self._robust_z(features[c]).to_numpy(dtype=float) for c in features.columns]
        if not robust_columns:
            return np.zeros(len(features)), "RobustTemporalFallback"
        matrix = np.column_stack(robust_columns)
        strongest = np.nanmax(matrix, axis=1)
        score = 1.0 - np.exp(-strongest / 4.0)
        return np.clip(score, 0, 1), "RobustTemporalFallback"

    def detect(self) -> None:
        self._visit("detect")
        features, numeric_columns = self.build_features()
        model_score, model_name = self._model_score(features)
        target = pd.to_numeric(self.data[self.config.target_column], errors="coerce")
        target_score = 1.0 - np.exp(-self._robust_z(target).to_numpy(dtype=float) / 4.0)
        diff_score = 1.0 - np.exp(-self._robust_z(target.diff()).to_numpy(dtype=float) / 4.0)
        combined = np.clip(0.55 * model_score + 0.30 * target_score + 0.15 * diff_score, 0, 1)

        scored = self.data.copy()
        scored["model_score"] = np.round(model_score, 6)
        scored["anomaly_score"] = np.round(combined, 6)
        scored["is_anomaly"] = combined >= self.config.anomaly_threshold
        scored["anomaly_type"] = "normal"
        self.state["model_backend_used"] = model_name

        for issue in self.state["issues"]:
            if issue["issue_type"] in {"missing_timestamp", "invalid_timestamp"}:
                scored.at[issue["row_position"], "is_anomaly"] = True
                scored.at[issue["row_position"], "anomaly_type"] = issue["issue_type"]

        # Missing values: only short gaps are eligible for automatic repair.
        missing_mask = target.isna().to_numpy()
        run: List[int] = []
        missing_runs: List[List[int]] = []
        for pos, missing in enumerate(missing_mask):
            if missing:
                run.append(pos)
            elif run:
                missing_runs.append(run)
                run = []
        if run:
            missing_runs.append(run)
        for positions in missing_runs:
            interior = positions[0] > 0 and positions[-1] < len(target) - 1
            continuous = interior and self._continuous_window(positions[0] - 1, positions[-1] + 1)
            low_risk = len(positions) <= self.config.max_auto_missing_gap and continuous
            severity = "low" if low_risk else "high"
            for pos in positions:
                scored.at[pos, "is_anomaly"] = True
                scored.at[pos, "anomaly_type"] = "missing"
                self._add_issue("missing", pos, self.config.target_column, severity, f"连续缺失长度={len(positions)}", run_length=len(positions))
            if severity == "high":
                reason = f"long_missing_run:{len(positions)}" if len(positions) > self.config.max_auto_missing_gap else "unbounded_or_discontinuous_missing_run"
                self.state["review_reasons"].append(reason)

        # Structural timestamp rules.
        time_col = self.config.timestamp_column
        duplicate_mask = self.data[time_col].notna() & self.data[time_col].duplicated(keep=False)
        for pos in np.flatnonzero(duplicate_mask.to_numpy()):
            scored.at[pos, "is_anomaly"] = True
            scored.at[pos, "anomaly_type"] = "duplicate_timestamp"
            self._add_issue("duplicate_timestamp", int(pos), time_col, "high", "重复时间戳")
        if duplicate_mask.any():
            self.state["review_reasons"].append("duplicate_timestamp")

        intervals = self.data[time_col].diff().dt.total_seconds().div(60)
        expected = self.config.expected_interval_minutes
        if expected:
            gap_mask = intervals > expected * self.config.interval_tolerance
            for pos in np.flatnonzero(gap_mask.fillna(False).to_numpy()):
                scored.at[pos, "is_anomaly"] = True
                scored.at[pos, "anomaly_type"] = "time_gap"
                self._add_issue("time_gap", int(pos), time_col, "high", f"间隔={intervals.iloc[pos]:.1f}分钟", interval_minutes=float(intervals.iloc[pos]))
            if gap_mask.any():
                self.state["review_reasons"].append("irregular_time_gap")

        # Flatline detection for sensor freezes.
        rolling_span = target.rolling(self.config.flatline_window, min_periods=self.config.flatline_window).apply(
            lambda x: float(np.nanmax(x) - np.nanmin(x)), raw=True
        )
        flat_mask = rolling_span < 1e-9
        for pos in np.flatnonzero(flat_mask.fillna(False).to_numpy()):
            scored.at[pos, "is_anomaly"] = True
            if scored.at[pos, "anomaly_type"] == "normal":
                scored.at[pos, "anomaly_type"] = "flatline"
            self._add_issue("flatline", int(pos), self.config.target_column, "medium", f"连续{self.config.flatline_window}点无变化")

        # Model anomalies not already explained by deterministic rules.
        unexplained = scored["is_anomaly"] & scored["anomaly_type"].eq("normal")
        for pos in np.flatnonzero(unexplained.to_numpy()):
            scored.at[pos, "anomaly_type"] = "model_anomaly"
            severity = "low" if combined[pos] < 0.90 else "medium"
            self._add_issue(
                "model_anomaly", int(pos), self.config.target_column, severity,
                f"融合异常分数={combined[pos]:.3f}", anomaly_score=float(combined[pos]),
            )

        self.scored = scored
        self.state["numeric_columns"] = numeric_columns
        self.state["anomaly_rows"] = int(scored["is_anomaly"].sum())

    def _continuous_window(self, start: int, end: int) -> bool:
        times = self.data[self.config.timestamp_column].iloc[start:end + 1]
        if times.isna().any():
            return False
        intervals = times.diff().dt.total_seconds().div(60).iloc[1:]
        if not (intervals > 0).all():
            return False
        expected = self.config.expected_interval_minutes
        return not expected or bool((intervals <= expected * self.config.interval_tolerance).all())

    def _audit(self, pos: int, action: str, before: Any, after: Any, reason: str) -> None:
        self.state["audit"].append({
            "source_row": int(self.data.iloc[pos]["_source_row"]),
            "row_position": int(pos),
            "column": self.config.target_column,
            "action": action,
            "before": _json_value(before),
            "after": _json_value(after),
            "reason": reason,
        })

    def repair(self) -> None:
        self._visit("repair")
        repaired = self.scored.copy()
        target_col = self.config.target_column
        source = pd.to_numeric(repaired[target_col], errors="coerce")
        interpolation = source.interpolate(method="linear", limit_area="inside")

        missing_positions = np.flatnonzero(source.isna().to_numpy())
        for pos in missing_positions:
            issue = next((i for i in self.state["issues"] if i["row_position"] == int(pos) and i["issue_type"] == "missing"), None)
            if issue and issue["severity"] == "low" and pd.notna(interpolation.iloc[pos]):
                repaired.at[pos, target_col] = interpolation.iloc[pos]
                self._audit(int(pos), "linear_interpolation", source.iloc[pos], interpolation.iloc[pos], "短缺失段低风险修复")

        # Auto-repair only isolated, high-confidence spikes with normal neighbors.
        model_positions = np.flatnonzero(repaired["anomaly_type"].eq("model_anomaly").to_numpy())
        for pos in model_positions:
            if pos == 0 or pos == len(repaired) - 1:
                continue
            if repaired.at[pos, "anomaly_score"] < 0.90:
                continue
            if bool(repaired.at[pos - 1, "is_anomaly"]) or bool(repaired.at[pos + 1, "is_anomaly"]):
                continue
            if not self._continuous_window(int(pos) - 1, int(pos) + 1):
                continue
            before = pd.to_numeric(pd.Series([repaired.at[pos, target_col]]), errors="coerce").iloc[0]
            after = np.nanmean([pd.to_numeric(pd.Series([repaired.at[pos - 1, target_col]]), errors="coerce").iloc[0],
                                pd.to_numeric(pd.Series([repaired.at[pos + 1, target_col]]), errors="coerce").iloc[0]])
            if pd.notna(before) and pd.notna(after):
                repaired.at[pos, target_col] = after
                self._audit(int(pos), "neighbor_interpolation", before, after, "孤立高置信尖峰低风险修复")

        self.repaired = repaired
        self.state["repairs_applied"] = len(self.state["audit"])

    def evaluate(self, label_column: Optional[str] = None) -> Optional[Dict[str, float]]:
        label_column = label_column or self.config.label_column
        if label_column not in self.data.columns:
            return None
        labels = self.data[label_column]
        if labels.isna().any() or not labels.isin([True, False, 0, 1]).all():
            self.state["evaluation_warning"] = "评估标签必须全部为布尔值或0/1；已跳过指标计算"
            return None
        truth = labels.astype(bool).to_numpy()
        prediction = self.scored["is_anomaly"].astype(bool).to_numpy()
        tp = int(np.sum(truth & prediction))
        fp = int(np.sum(~truth & prediction))
        fn = int(np.sum(truth & ~prediction))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        metrics = {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}
        self.state["demo_metrics"] = metrics
        return metrics

    def report(self, output_dir: str | Path) -> Dict[str, Any]:
        self._visit("report")
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        self.state["review_reasons"] = list(dict.fromkeys(self.state["review_reasons"]))
        self.state["decision"] = "needs_human_review" if self.state["review_reasons"] else "approved_with_repairs" if self.state["repairs_applied"] else "approved"
        self.evaluate()

        scored_path = out / "scored_data.csv"
        repaired_path = out / "repaired_data.csv"
        event_path = out / "anomaly_events.csv"
        audit_path = out / "repair_audit.csv"
        metrics_path = out / "demo_metrics.json"
        report_path = out / "quality_report.md"
        state_path = out / "run_state.json"

        export_columns = [c for c in self.scored.columns if c != "_source_row"]
        self.scored[export_columns].to_csv(scored_path, index=False)
        self.repaired[[c for c in self.repaired.columns if c != "_source_row"]].to_csv(repaired_path, index=False)
        pd.DataFrame(self.state["issues"]).to_csv(event_path, index=False)
        pd.DataFrame(self.state["audit"], columns=["source_row", "row_position", "column", "action", "before", "after", "reason"]).to_csv(audit_path, index=False)
        metrics_path.write_text(json.dumps(self.state.get("demo_metrics", {}), ensure_ascii=False, indent=2), encoding="utf-8")

        metrics = self.state.get("demo_metrics")
        lines = [
            "# HydroGuard 地下水监测数据质控报告", "",
            f"- 处置结论：**{self.state['decision']}**",
            f"- 模型后端：{self.state['model_backend_used']}",
            f"- 输入记录：{self.state['input_rows']}",
            f"- 异常标记：{self.state['anomaly_rows']}",
            f"- 自动修复：{self.state['repairs_applied']}",
            f"- 转人工原因：{', '.join(self.state['review_reasons']) or '无'}", "",
        ]
        if metrics:
            lines.extend([
                "## 合成演示集指标", "",
                f"- Precision：{metrics['precision']}",
                f"- Recall：{metrics['recall']}",
                f"- F1：{metrics['f1']}", "",
                "> 以上指标仅用于验证演示流程，不代表真实生产数据效果。", "",
            ])
        lines.extend(["## 审计说明", "", "所有自动修改均写入 repair_audit.csv；重复时间戳、异常采样间隔等结构性风险不自动删除。", ""])
        report_path.write_text("\n".join(lines), encoding="utf-8")

        self.state["output_files"] = {name: str(path) for name, path in {
            "scored_data": scored_path,
            "repaired_data": repaired_path,
            "anomaly_events": event_path,
            "repair_audit": audit_path,
            "demo_metrics": metrics_path,
            "quality_report": report_path,
            "run_state": state_path,
        }.items()}
        state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2, default=_json_value), encoding="utf-8")
        return self.result()

    def result(self) -> Dict[str, Any]:
        return {
            "decision": self.state["decision"],
            "model_backend_used": self.state.get("model_backend_used"),
            "input_rows": self.state.get("input_rows", 0),
            "anomaly_rows": self.state.get("anomaly_rows", 0),
            "repairs_applied": self.state.get("repairs_applied", 0),
            "review_reasons": self.state["review_reasons"],
            "demo_metrics": self.state.get("demo_metrics"),
            "output_files": self.state.get("output_files", {}),
        }

    def run(self, input_path: str | Path, output_dir: str | Path) -> Dict[str, Any]:
        self.ingest(input_path)
        self.detect()
        self.repair()
        return self.report(output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="地下水监测数据无监督异常检测、低风险修复与审计")
    parser.add_argument("--input", required=True, help="CSV、Excel 或 JSON 文件")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument("--timestamp-column", default="timestamp")
    parser.add_argument("--target-column", default="water_level_m")
    parser.add_argument("--backend", choices=["auto", "isolation_forest", "robust"], default="auto")
    args = parser.parse_args()
    config = HydroGuardConfig(timestamp_column=args.timestamp_column, target_column=args.target_column, model_backend=args.backend)
    print(json.dumps(HydroGuardPipeline(config).run(args.input, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

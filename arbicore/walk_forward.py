"""Minimal walk-forward evaluation helper.

Splits a sequential return series into rolling train/test windows.
Does not claim statistical rigor for tiny samples — use with real backtest PnL series.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence


@dataclass
class WindowResult:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    train_mean: float
    test_mean: float
    test_sum: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalkForwardReport:
    windows: list[WindowResult]
    positive_test_windows: int
    total_windows: int
    passed: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "windows": [w.as_dict() for w in self.windows],
            "positive_test_windows": self.positive_test_windows,
            "total_windows": self.total_windows,
            "passed": self.passed,
            "reason": self.reason,
        }


def evaluate(
    returns: Sequence[float],
    *,
    train_size: int = 60,
    test_size: int = 20,
    step: int = 20,
    min_positive_ratio: float = 0.5,
) -> WalkForwardReport:
    series = [float(x) for x in returns]
    n = len(series)
    windows: list[WindowResult] = []
    i = 0
    while i + train_size + test_size <= n:
        tr = series[i : i + train_size]
        te = series[i + train_size : i + train_size + test_size]
        windows.append(
            WindowResult(
                train_start=i,
                train_end=i + train_size,
                test_start=i + train_size,
                test_end=i + train_size + test_size,
                train_mean=sum(tr) / len(tr) if tr else 0.0,
                test_mean=sum(te) / len(te) if te else 0.0,
                test_sum=sum(te),
            )
        )
        i += step

    if not windows:
        return WalkForwardReport(
            windows=[],
            positive_test_windows=0,
            total_windows=0,
            passed=False,
            reason=f"need at least {train_size + test_size} points, got {n}",
        )

    pos = sum(1 for w in windows if w.test_sum > 0)
    ratio = pos / len(windows)
    passed = ratio >= min_positive_ratio
    return WalkForwardReport(
        windows=windows,
        positive_test_windows=pos,
        total_windows=len(windows),
        passed=passed,
        reason=(
            f"{pos}/{len(windows)} test windows positive (need >= {min_positive_ratio:.0%})"
        ),
    )


__all__ = ["WindowResult", "WalkForwardReport", "evaluate"]

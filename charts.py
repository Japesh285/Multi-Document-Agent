"""
charts.py — Auto-generate charts from analysis results.

Detects chart type from data shape and saves PNG files to output/charts/.
Chart types supported:
  - Horizontal bar   (categorical Series, single-col DataFrame)
  - Pie              (small categorical distributions ≤ 10 slices)
  - Line / multi-line (DatetimeIndex Series or DataFrame)
  - Grouped bar      (multi-column DataFrame, ≤ 20 rows)
  - Win/Loss         (Result column breakdown — dedicated helper)

All functions return the file path on success, or None on failure.
"""

from __future__ import annotations
import re
from pathlib import Path
from typing import Any

import pandas as pd

from utils import get_logger

log = get_logger("charts")

OUTPUT_DIR = Path("output") / "charts"
_COLORS    = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B2",
               "#937860", "#DA8BC3", "#8C8C8C", "#CCB974", "#64B5CD"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _safe_name(step_id: str) -> str:
    return re.sub(r"[^\w\-]", "_", step_id)


def _get_plt():
    """Import matplotlib with non-interactive Agg backend."""
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    return plt


def _save(fig, plt, fname: Path, step_id: str) -> str | None:
    try:
        fig.tight_layout()
        fig.savefig(fname, dpi=120, bbox_inches="tight")
        plt.close(fig)
        log.debug("charts: saved %s", fname)
        return str(fname)
    except Exception as exc:
        log.debug("charts: save failed %s — %s", step_id, exc)
        try:
            plt.close("all")
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Specific chart builders
# ---------------------------------------------------------------------------

def bar_chart(
    data: pd.Series,
    step_id: str,
    title: str = "",
    xlabel: str = "Value",
    max_bars: int = 20,
) -> str | None:
    """Horizontal bar chart from a numeric Series."""
    try:
        plt = _get_plt()
        data = data.dropna().sort_values(ascending=True).tail(max_bars)
        if data.empty:
            return None
        _ensure_dir()
        fname = OUTPUT_DIR / f"{_safe_name(step_id)}.png"
        fig, ax = plt.subplots(figsize=(10, max(4, len(data) * 0.4)))
        bars = ax.barh(data.index.astype(str), data.values, color=_COLORS[0])
        ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=8)
        ax.set_xlabel(xlabel)
        ax.set_title(title or step_id, fontsize=11)
        return _save(fig, plt, fname, step_id)
    except Exception as exc:
        log.debug("charts: bar_chart failed — %s", exc)
        return None


def pie_chart(
    data: pd.Series,
    step_id: str,
    title: str = "",
    max_slices: int = 10,
) -> str | None:
    """Pie chart from a categorical Series (value counts)."""
    try:
        plt = _get_plt()
        data = data.dropna().head(max_slices)
        if data.empty or len(data) < 2:
            return None
        _ensure_dir()
        fname = OUTPUT_DIR / f"{_safe_name(step_id)}_pie.png"
        fig, ax = plt.subplots(figsize=(8, 8))
        wedge_props = {"linewidth": 0.8, "edgecolor": "white"}
        ax.pie(
            data.values,
            labels=data.index.astype(str),
            autopct="%1.1f%%",
            startangle=140,
            colors=_COLORS[: len(data)],
            wedgeprops=wedge_props,
        )
        ax.set_title(title or step_id, fontsize=11)
        return _save(fig, plt, fname, step_id)
    except Exception as exc:
        log.debug("charts: pie_chart failed — %s", exc)
        return None


def line_chart(
    data: pd.Series | pd.DataFrame,
    step_id: str,
    title: str = "",
    ylabel: str = "Value",
) -> str | None:
    """Line chart for time-series data."""
    try:
        plt = _get_plt()
        if isinstance(data, pd.Series):
            data = data.dropna()
        if data.empty if isinstance(data, pd.Series) else data.empty:
            return None
        _ensure_dir()
        fname = OUTPUT_DIR / f"{_safe_name(step_id)}.png"
        fig, ax = plt.subplots(figsize=(12, 5))
        if isinstance(data, pd.Series):
            ax.plot(data.index, data.values, marker="o", linewidth=1.8,
                    color=_COLORS[0], markersize=4)
        else:
            numeric = data.select_dtypes(include="number").columns[:5]
            for i, col in enumerate(numeric):
                ax.plot(data.index, data[col], label=col,
                        linewidth=1.8, color=_COLORS[i], marker="o", markersize=3)
            ax.legend(fontsize=9)
        ax.set_ylabel(ylabel)
        ax.set_title(title or step_id, fontsize=11)
        fig.autofmt_xdate()
        return _save(fig, plt, fname, step_id)
    except Exception as exc:
        log.debug("charts: line_chart failed — %s", exc)
        return None


def win_loss_chart(
    win: int,
    loss: int,
    push: int = 0,
    pending: int = 0,
    step_id: str = "win_loss",
    title: str = "Win / Loss Breakdown",
) -> str | None:
    """Dedicated Win/Loss/Push/Pending bar chart."""
    try:
        plt = _get_plt()
        categories = []
        values     = []
        colors     = []
        for label, val, color in [
            ("Win",     win,     "#55A868"),
            ("Loss",    loss,    "#C44E52"),
            ("Push",    push,    "#8172B2"),
            ("Pending", pending, "#8C8C8C"),
        ]:
            if val > 0:
                categories.append(label)
                values.append(val)
                colors.append(color)
        if not values:
            return None
        _ensure_dir()
        fname = OUTPUT_DIR / f"{_safe_name(step_id)}.png"
        fig, ax = plt.subplots(figsize=(7, 4))
        bars = ax.bar(categories, values, color=colors, width=0.55)
        ax.bar_label(bars, fmt="%d", padding=3, fontsize=10, fontweight="bold")
        ax.set_title(title, fontsize=11)
        ax.set_ylabel("Bets")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        return _save(fig, plt, fname, step_id)
    except Exception as exc:
        log.debug("charts: win_loss_chart failed — %s", exc)
        return None


def grouped_bar_chart(
    df: pd.DataFrame,
    step_id: str,
    title: str = "",
    max_rows: int = 20,
) -> str | None:
    """Grouped bar chart for multi-column DataFrames."""
    try:
        plt = _get_plt()
        numeric = df.select_dtypes(include="number").columns[:4].tolist()
        if not numeric:
            return None
        data = df[numeric].head(max_rows)
        if data.empty:
            return None
        _ensure_dir()
        fname = OUTPUT_DIR / f"{_safe_name(step_id)}.png"
        fig, ax = plt.subplots(figsize=(12, 5))
        data.plot(kind="bar", ax=ax, color=_COLORS[: len(numeric)], width=0.75)
        ax.set_xticklabels(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
        ax.set_title(title or step_id, fontsize=11)
        ax.legend(fontsize=9)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        return _save(fig, plt, fname, step_id)
    except Exception as exc:
        log.debug("charts: grouped_bar_chart failed — %s", exc)
        return None


# ---------------------------------------------------------------------------
# Auto-detect and dispatch
# ---------------------------------------------------------------------------

def auto_chart(
    data: Any,
    step_id: str,
    title: str = "",
    *,
    max_categories: int = 20,
    prefer_pie_threshold: int = 6,
) -> str | None:
    """
    Detect the best chart type for `data` and generate it.
    Returns file path or None.
    """
    try:
        import matplotlib  # noqa: PLC0415 — quick availability check
    except ImportError:
        log.warning("charts: matplotlib not installed — skipping")
        return None

    if data is None:
        return None

    # ── pd.Series ────────────────────────────────────────────────────────────
    if isinstance(data, pd.Series):
        data = data.dropna()
        if data.empty:
            return None

        if pd.api.types.is_datetime64_any_dtype(data.index):
            return line_chart(data, step_id, title)

        if pd.api.types.is_numeric_dtype(data):
            n = len(data)
            if n <= prefer_pie_threshold:
                return pie_chart(data, step_id, title)
            return bar_chart(data, step_id, title, max_bars=max_categories)

    # ── pd.DataFrame ─────────────────────────────────────────────────────────
    if isinstance(data, pd.DataFrame) and not data.empty:
        numeric = data.select_dtypes(include="number").columns.tolist()

        if pd.api.types.is_datetime64_any_dtype(data.index) and numeric:
            return line_chart(data, step_id, title)

        if len(numeric) == 1 and len(data) <= max_categories:
            col = numeric[0]
            s   = data[col].rename(data.index if data.index.name else col)
            return bar_chart(s, step_id, title, xlabel=col)

        if len(numeric) >= 2 and len(data) <= max_categories:
            return grouped_bar_chart(data, step_id, title, max_rows=max_categories)

    return None

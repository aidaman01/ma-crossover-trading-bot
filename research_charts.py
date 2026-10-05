"""Static research charts (matplotlib, PNG) written to docs/research/ by research.py."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import metrics  # noqa: E402

SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
FIXED_STYLE = {   # benchmarks: neutral greys with distinct dashes, never a series colour
    "SPY buy & hold": ("#3d3c39", "-", 1.4),
    "60/40 SPY/IEF": ("#8a8986", "--", 1.4),
}


def style(name: str, i: int, highlight: str | None = None) -> tuple[str, str, float]:
    if name in FIXED_STYLE:
        return FIXED_STYLE[name]
    return SLOTS[i % len(SLOTS)], "-", 2.4 if name == highlight else 1.6


def axes_style(ax) -> None:
    ax.grid(color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#b9b8b2")
    ax.tick_params(colors=MUTED, labelsize=8)


def money_log_axis(ax, lo: float, hi: float) -> None:
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v / 1000:,.0f}k"))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.yaxis.set_minor_locator(mticker.NullLocator())
    ticks = [t * 1000 for t in (50, 75, 100, 150, 200, 300, 400, 600, 800, 1200)]
    ax.set_yticks([t for t in ticks if lo * 0.85 <= t <= hi * 1.15])


def save(fig, path: str) -> None:
    fig.savefig(path, bbox_inches="tight", facecolor="white", dpi=120)
    plt.close(fig)


def equity_main(results: dict, names: list[str], path: str, capital: float) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    curves = {n: results[n]["equity"] for n in names}
    for i, (n, eq) in enumerate(curves.items()):
        color, ls, lw = style(n, i, highlight=names[0])
        ax.plot(eq.index, eq.values, color=color, ls=ls, lw=lw, label=f"{n}  (${eq.iloc[-1] / 1000:,.0f}k)")
    ax.axhline(capital, color="#d1d0cb", lw=0.8)
    money_log_axis(ax, min(c.min() for c in curves.values()), max(c.max() for c in curves.values()))
    axes_style(ax)
    first = next(iter(curves.values()))
    ax.set_title(f"Growth of $100k, {first.index[0]:%b %Y} – {first.index[-1]:%b %Y} (log scale, after costs)",
                 loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, loc="upper left", labelcolor=MUTED)
    save(fig, path)


def drawdowns(results: dict, names: list[str], path: str) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(10, 5.6), sharex=True, sharey=True)
    for i, (ax, n) in enumerate(zip(axes.flat, names, strict=False)):
        dd = metrics.drawdown_series(results[n]["equity"]) * 100
        color, _, _ = style(n, i, highlight=names[0])
        ax.fill_between(dd.index, dd.values, 0, color=color, alpha=0.3, lw=0)
        ax.plot(dd.index, dd.values, color=color, lw=0.9)
        ax.set_title(f"{n}: worst {dd.min():.1f}%".replace("-", "−"), loc="left", fontsize=9, color=INK)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        axes_style(ax)
    fig.suptitle("Drawdown from previous peak (daily closes)", x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    save(fig, path)


def rolling(series: dict, names: list[str], path: str) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    for i, n in enumerate(names):
        color, ls, lw = style(n, i, highlight=names[0])
        vol = series[f"{n} | vol 63d"]
        shp = series[f"{n} | sharpe 252d"]
        main = i == 0
        ax1.plot(vol.index, vol.values * 100, color=color, ls=ls, lw=1.8 if main else 1.0,
                 alpha=1 if main else 0.75, label=n, zorder=3 if main else 2)
        ax2.plot(shp.index, shp.values, color=color, ls=ls, lw=1.8 if main else 1.0,
                 alpha=1 if main else 0.75, label=n, zorder=3 if main else 2)
    ax1.set_title("Rolling 63-day volatility (annualized)", loc="left", fontsize=10, color=INK)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax2.set_title("Rolling 252-day Sharpe ratio (excess over cash)", loc="left", fontsize=10, color=INK)
    ax2.axhline(0, color="#b9b8b2", lw=0.8)
    for ax in (ax1, ax2):
        axes_style(ax)
    ax1.legend(frameon=False, fontsize=8, ncol=4, loc="upper left", labelcolor=MUTED)
    fig.tight_layout()
    save(fig, path)


def robustness(df: pd.DataFrame, path: str) -> None:
    """Dot plot of Sharpe, CAGR and max drawdown for every robustness variant."""
    df = df.reset_index(drop=True)
    labels = [f"{g}: {v}" for g, v in zip(df["group"], df["variant"], strict=True)]
    y = np.arange(len(df))[::-1]
    fig, axes = plt.subplots(1, 3, figsize=(11, 0.26 * len(df) + 1.2), sharey=True)
    for ax, (col, title, scale) in zip(axes, [("sharpe", "Sharpe", 1), ("cagr", "CAGR %", 100),
                                              ("max_dd", "Max drawdown %", 100)], strict=True):
        vals = df[col].to_numpy() * scale
        std = df["standard"].astype(bool).to_numpy()
        ax.scatter(vals[~std], y[~std], s=22, color="#8a8986", zorder=3)
        ax.scatter(vals[std], y[std], s=40, color=SLOTS[0], zorder=4, label="live configuration")
        ref = vals[std][0] if std.any() else None
        if ref is not None:
            ax.axvline(ref, color=SLOTS[0], lw=0.8, alpha=0.5)
        ax.set_title(title, loc="left", fontsize=10, color=INK)
        axes_style(ax)
    axes[0].set_yticks(y)
    axes[0].set_yticklabels(labels, fontsize=7.5)
    axes[0].legend(frameon=False, fontsize=8, loc="lower left", labelcolor=MUTED)
    fig.suptitle("Robustness of the chosen strategy (each dot = one variant, full period)", x=0.01, ha="left",
                 fontsize=11, color=INK)
    fig.tight_layout()
    save(fig, path)


def regime_bars(df: pd.DataFrame, names: list[str], path: str) -> None:
    kinds = list(dict.fromkeys(df["regime_type"]))
    fig, axes = plt.subplots(1, len(kinds), figsize=(10, 3.8), sharey=True)
    width = 0.8 / len(names)
    for ax, kind in zip(np.atleast_1d(axes), kinds, strict=True):
        sub = df[df["regime_type"] == kind]
        regs = sorted(sub["regime"].unique())
        x = np.arange(len(regs))
        for i, n in enumerate(names):
            vals = [float(sub[(sub["regime"] == r) & (sub["strategy"] == n)]["ann_compounded"].iloc[0]) * 100
                    for r in regs]
            color, _, _ = style(n, i, highlight=names[0])
            ax.bar(x + (i - (len(names) - 1) / 2) * width, vals, width * 0.9, color=color, label=n)
        shares = [float(sub[sub["regime"] == r]["share_of_days"].iloc[0]) for r in regs]
        ax.set_xticks(x)
        ax.set_xticklabels([f"{r}\n({s:.0%} of days)" for r, s in zip(regs, shares, strict=True)], fontsize=8)
        ax.axhline(0, color="#b9b8b2", lw=0.8)
        ax.set_title(f"{kind} regime", loc="left", fontsize=10, color=INK)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        axes_style(ax)
    np.atleast_1d(axes)[0].set_ylabel("annualized compounded return", fontsize=8, color=MUTED)
    handles, labels = np.atleast_1d(axes)[0].get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, fontsize=8, ncol=len(names), loc="lower center",
               bbox_to_anchor=(0.5, -0.02), labelcolor=MUTED)
    fig.suptitle("Performance by market regime (regime known at the previous close)", x=0.01, ha="left",
                 fontsize=11, color=INK)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    save(fig, path)


def approaches(results: dict, names: list[str], path: str) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    curves = {n: results[n]["equity"] for n in names}
    for i, (n, eq) in enumerate(curves.items()):
        color, ls, lw = style(n, i)
        ax.plot(eq.index, eq.values, color=color, ls=ls, lw=lw, label=f"{n}  (${eq.iloc[-1] / 1000:,.0f}k)")
    money_log_axis(ax, min(c.min() for c in curves.values()), max(c.max() for c in curves.values()))
    axes_style(ax)
    ax.set_title("Approach comparison on the same history (in-sample model selection)", loc="left",
                 fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=7.5, loc="upper left", labelcolor=MUTED)
    save(fig, path)

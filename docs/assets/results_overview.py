#!/usr/bin/env python3
"""The README's results figure: the paper's SWE-Bench headline, per agent-model combination.

    python docs/assets/results_overview.py      # needs matplotlib; writes the PNG and SVG here

Left panel    mean cost per task, % lower than default execution, with +/- one standard
              deviation. One series, so one hue and no legend; the value sits at the bar's tip.
Right panel   Pass@1, default and TokenEconomizer, as a dumbbell per combination, with the
              change in points beside it. A different measure on a different scale, so a
              second panel rather than a second axis.

The numbers are the paper's (arXiv 2610.22732, Table 1, the TokenEconomizer row against
default), as the project page shows them; nothing here is computed from runs in this
repository.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch

OUT_DIR = Path(__file__).resolve().parent
PNG_DPI = 300

# (agent, model, cost change %, its s.d., Pass@1 default, Pass@1 TokenEconomizer)
ROWS = [
    ("Claude Code", "Sonnet 5", -23.6, 1.8, 74.7, 76.0),
    ("Claude Code", "Opus 4.8", -30.9, 2.1, 74.0, 73.0),
    ("Codex", "GPT-5.6 Terra", -24.7, 1.2, 73.8, 74.7),
    ("Codex", "GPT-5.6 Sol", -26.1, 2.1, 80.1, 81.7),
    ("Copilot CLI", "Sonnet 5", -18.4, 1.5, 75.0, 77.6),
]

# The project page's colors: TokenEconomizer's mark, a gray for default that still clears
# 3:1 on white, and text, rules and grid one step off the surface.
TE = "#285E9F"
DEFAULT = "#8A939C"
INK = "#132031"
MUTED = "#5D6978"
GRID = "#E6EAEF"
AXIS = "#A9B2BD"
SURFACE = "#FFFFFF"

FS_TITLE = 8.6
FS_LABEL = 8.0
FS_VALUE = 7.6
FS_TICK = 7.0
FS_NOTE = 6.6

FIG_W, FIG_H = 8.0, 2.95
BAR_H = 0.46            # of a row's pitch
DOT = 6.2               # marker diameter, points
RING = 1.3              # the surface ring around each dot, points
STEM = 1.4              # the dumbbell's line, points


def rounded_bar(ax, y, length, height, color, radius_px=4.0):
    """A horizontal bar from 0 to `length`, rounded at its data end. It starts left of the
    baseline and the axes clip it there, so it meets the baseline square, in one patch."""
    fig = ax.figure
    fig.canvas.draw()
    (x0, y0), (x1, y1) = ax.transData.transform([(0, 0), (1, 1)])
    px_per_x, px_per_y = abs(x1 - x0), abs(y1 - y0)
    r = radius_px / px_per_x
    ax.add_patch(FancyBboxPatch((-2 * r, y - height / 2), length + 2 * r, height,
                                boxstyle=f"round,pad=0,rounding_size={r}",
                                mutation_aspect=px_per_x / px_per_y,
                                facecolor=color, edgecolor="none", zorder=3))


def style_axis(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["bottom"].set_linewidth(0.6)
    ax.tick_params(axis="x", colors=MUTED, labelsize=FS_TICK, length=0, pad=3)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)


def main():
    plt.rcParams["font.family"] = "DejaVu Sans"
    fig = plt.figure(figsize=(FIG_W, FIG_H), facecolor=SURFACE)
    left = fig.add_axes([0.205, 0.24, 0.42, 0.58])
    right = fig.add_axes([0.665, 0.24, 0.25, 0.58], sharey=left)
    n = len(ROWS)
    ys = list(range(n - 1, -1, -1))         # first row on top

    # Cost: % lower than default.
    style_axis(left)
    left.set_xlim(0, 37)
    left.set_ylim(-0.6, n - 0.4)
    left.set_xticks([0, 10, 20, 30], ["0%", "10%", "20%", "30%"])
    left.set_yticks(ys, [f"{a} · {m}" for a, m, *_ in ROWS], fontsize=FS_LABEL, color=INK)
    left.axvline(0, color=AXIS, linewidth=0.8, zorder=4)
    for y, (_, _, cost, sd, *_rest) in zip(ys, ROWS):
        drop = -cost
        rounded_bar(left, y, drop, BAR_H, TE)
        left.plot([drop - sd, drop + sd], [y, y], color=INK, linewidth=0.7,
                  solid_capstyle="butt", zorder=4)
        left.text(drop + sd + 0.8, y, f"{drop:.1f}%", va="center", ha="left",
                  fontsize=FS_VALUE, color=INK, fontweight="bold", zorder=5)
    left.set_title("Mean cost per task, lower than default", loc="left", fontsize=FS_TITLE,
                   color=INK, fontweight="bold", pad=8)

    # Pass@1: default and TokenEconomizer.
    style_axis(right)
    right.set_xlim(71.5, 83.5)
    right.set_xticks([72, 76, 80], ["72", "76", "80"])
    right.tick_params(axis="y", labelleft=False)
    for y, (*_, base, te) in zip(ys, ROWS):
        right.plot([base, te], [y, y], color=AXIS, linewidth=STEM, zorder=2,
                   solid_capstyle="round")
        for x, c in ((base, DEFAULT), (te, TE)):
            right.plot([x], [y], marker="o", markersize=DOT, color=c,
                       markeredgecolor=SURFACE, markeredgewidth=RING, zorder=3)
        right.text(1.06, y, f"{te - base:+.1f}".replace("-", "−"),
                   transform=right.get_yaxis_transform(), va="center", ha="left",
                   fontsize=FS_VALUE, color=INK, fontweight="bold")
    right.text(1.06, n - 0.4, "points", transform=right.get_yaxis_transform(),
               va="bottom", ha="left", fontsize=FS_NOTE, color=MUTED)
    right.set_title("Pass@1 (%)", loc="left", fontsize=FS_TITLE, color=INK,
                    fontweight="bold", pad=8)
    handles = [Line2D([], [], marker="o", linestyle="none", markersize=DOT, color=c,
                      markeredgecolor=SURFACE, markeredgewidth=RING, label=lab)
               for c, lab in ((DEFAULT, "default"), (TE, "TokenEconomizer"))]
    fig.legend(handles=handles, loc="center left", bbox_to_anchor=(0.655, 0.09), ncol=2,
               frameon=False, fontsize=FS_TICK, labelcolor=INK, handletextpad=0.2,
               columnspacing=1.0, borderaxespad=0)
    fig.text(0.035, 0.09, "SWE-Bench: 277 tasks from 6 repositories, each run in order. "
             "Whiskers: \u00b11 s.d.", fontsize=FS_NOTE, color=MUTED, ha="left", va="center")

    base = OUT_DIR / "results_overview"
    tight = dict(bbox_inches="tight", pad_inches=0.12)
    fig.savefig(base.with_suffix(".png"), dpi=PNG_DPI, facecolor=SURFACE, **tight)
    fig.savefig(base.with_suffix(".svg"), facecolor=SURFACE, metadata={"Date": None},
                **tight)


if __name__ == "__main__":
    main()

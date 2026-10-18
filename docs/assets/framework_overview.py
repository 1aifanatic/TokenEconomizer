#!/usr/bin/env python3
"""The README figure: the paper's framework overview, in the plugin's words.

    python docs/assets/framework_overview.py      # needs matplotlib; writes the PNG and SVG here

Left column   what the plugin keeps for a repository, the three mechanisms that act on the
              next session, and the fold that updates them when a session ends.
Right column  the agent, unchanged, drawn as the three surfaces the plugin reaches. Each
              mechanism is shown by what it adds to or removes from its surface: facts added
              to the session's context, tools struck out of the loaded set, and calls struck
              out of a turn the guard stopped.

The layout is the paper figure's. Every row sits on one pitch, so a card, its arrow and its
surface share one horizontal axis; the gaps inside a row are solved for, so the three surfaces
end flush; and text placed next to other text is measured, with anything that could outgrow
its box asserted, so a re-wording cannot silently overlap.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Rectangle

OUT_DIR = Path(__file__).resolve().parent
PNG_DPI = 600

# Drawn at the placed size: 10 units per inch on both axes, so circles stay round.
FIG_W, FIG_H = 5.5, 2.775
XMAX, YMAX = 55.0, 27.75
UNITS_PER_INCH = 10.0

# Colorblind-friendly, print-safe palette.
INK = "#20303C"
MUTED = "#63727D"
LINE = "#C6D0D6"
PANEL = "#FBFCFD"
SKILL = "#3E73A8"
SKILL_LIGHT = "#DFEAF4"
TOOLS = "#BC7D14"
TOOLS_LIGHT = "#F7E8C9"
SPIRAL = "#BF4F4B"
SPIRAL_LIGHT = "#F4DCDA"
NEUTRAL_FC, NEUTRAL_EC = "#EDF0F2", "#B7C2C9"
DROPPED_FC, DROPPED_EC, DROPPED_TC = "#EDEFF1", "#BAC4CA", "#98A4AC"

# One type scale for the whole drawing; nothing is rescaled after export.
FS_PANEL = 6.0      # panel headers
FS_ROW = 5.6        # row titles inside the agent panel
FS_CHIP = 5.8       # mechanism names
FS_BODY = 4.6       # explanatory copy
FS_SMALL = 4.4      # annotations
FS_TAG = 4.5        # lifecycle tags
FS_MONO = 4.1       # tool names and turn actions
FS_MARK = 4.3       # the two runtime interventions
FS_DOTS = 6.2       # the ellipsis standing in for turns not drawn

MONO = "DejaVu Sans Mono"
ELLIPSIS = "…"

# ---- layout -----------------------------------------------------------------
# The three rows sit on one pitch and every surface is drawn at one height, so a
# mechanism card, its arrow and the object it acts on share a horizontal axis.
CTRL_X0, CTRL_X1 = 0.50, 15.90
AGENT_X0, AGENT_X1 = 17.60, 54.50
PANEL_Y0, PANEL_Y1 = 6.15, 27.35
PAD = 0.85                                 # one padding value inside both panels

AIN_X0, AIN_X1 = 19.20, 53.20              # content area inside the agent panel
CHIP_X0, CHIP_X1 = CTRL_X0 + PAD, CTRL_X1 - PAD
HDR_TITLE_Y, HDR_SUB_Y, HDR_RULE = 26.40, 25.70, 23.85

ROW_TOP, ROW_PITCH, ROW_H = 23.30, 5.95, 4.85
ROWS = {n: (ROW_TOP - (n - 1) * ROW_PITCH - ROW_H,
            ROW_TOP - (n - 1) * ROW_PITCH) for n in (1, 2, 3)}
BAR_OFFSET, BAR_H = 1.70, 1.50             # bar top below the row title
CHIP_H = 4.30

LEARN_Y0, LEARN_Y1 = 0.25, 4.55            # learner box, below the controller
LEG_Y, LEG_X = 2.40, 50.00                 # the completed trace travels back along this


def bar_y(number):
    return ROWS[number][1] - BAR_OFFSET - BAR_H


def axis_y(number):
    """The one horizontal axis a mechanism, its arrow and its surface share."""
    return bar_y(number) + BAR_H / 2


def flush_gap(widths):
    """Gap that makes a run of boxes span the content width exactly.

    Solving for the gap rather than hand-picking one is what keeps the three
    surfaces ending flush with each other after any width is edited."""
    gap = (AIN_X1 - AIN_X0 - sum(widths)) / (len(widths) - 1)
    assert 0.35 <= gap <= 1.20, f"box widths leave a {gap:.2f} unit gap"
    return gap


def rounded(ax, xy, width, height, *, fc="white", ec=LINE, lw=0.7,
            radius=0.32, zorder=1, alpha=1.0, ls="solid"):
    patch = FancyBboxPatch(
        xy, width, height,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        facecolor=fc, edgecolor=ec, linewidth=lw, zorder=zorder,
        alpha=alpha, linestyle=ls,
    )
    ax.add_patch(patch)
    return patch


def arrow(ax, p0, p1, *, color=MUTED, lw=0.85, mutation=4.5, zorder=6):
    a = FancyArrowPatch(
        p0, p1, arrowstyle="-|>", mutation_scale=mutation, linewidth=lw,
        color=color, shrinkA=0, shrinkB=0, zorder=zorder,
    )
    ax.add_patch(a)
    return a


def width_of(fig, text, fontsize, *, weight="normal", family=None):
    """Measured width of a label, in drawing units.

    Several labels are laid out end to end on one line (a row title next to its
    eyebrow, a right-aligned tag).  Estimating their widths from a character count
    is what let an earlier revision overlap its own text, so the real renderer is
    asked instead."""
    probe = fig.text(0, 0, text, fontsize=fontsize, fontweight=weight,
                     family=family)
    extent = probe.get_window_extent(renderer=fig.canvas.get_renderer())
    probe.remove()
    return extent.width / fig.dpi * UNITS_PER_INCH


def badge(ax, x, y, number, color, *, radius=0.72, fs=4.6):
    """The numbered token that ties a mechanism card to the surface it acts on."""
    ax.add_patch(Circle((x, y), radius, facecolor=color, edgecolor="none", zorder=7))
    ax.text(x, y, number, ha="center", va="center", fontsize=fs,
            color="white", fontweight="bold", zorder=8)


def tag(fig, ax, x_right, y, text, color, light, *, fs=FS_TAG, weight="bold"):
    """Right-aligned lifecycle tag; it never shares a line with body copy."""
    width = width_of(fig, text, fs, weight=weight) + 1.10
    rounded(ax, (x_right - width, y - 0.62), width, 1.24, fc=light, ec="none",
            radius=0.62, zorder=5)
    ax.text(x_right - width / 2, y, text, ha="center", va="center",
            fontsize=fs, color=color, fontweight=weight, zorder=6)
    return x_right - width


def annotate(ax, number, x0, x1, text, color, *, fs=FS_SMALL):
    """Range note under the part of a surface a mechanism changes.

    All three rows annotate at the same offset, so the notes read as one band."""
    y = bar_y(number) - 0.35
    ax.plot([x0, x1], [y, y], color=color, lw=0.7, zorder=6)
    for x in (x0, x1):
        ax.plot([x, x], [y, y + 0.36], color=color, lw=0.7, zorder=6)
    ax.text((x0 + x1) / 2, y - 0.42, text, fontsize=fs, color=color,
            ha="center", va="top", fontweight="bold", zorder=6)


# ---- left column ------------------------------------------------------------

MECHANISMS = [
    (1, "Micro-skills", SKILL, SKILL_LIGHT,
     ("repository facts learned from failures",
      "that recur across finished sessions")),
    (2, "Tool pruning", TOOLS, TOOLS_LIGHT,
     ("tools left unused session after session",
      "go on a deny list once you approve it")),
    (3, "Failure-loop intervention", SPIRAL, SPIRAL_LIGHT,
     ("learns how long turns here usually run;",
      "one redirect, then a stop or a notice")),
]


def draw_controller(fig, ax):
    rounded(ax, (CTRL_X0, PANEL_Y0), CTRL_X1 - CTRL_X0, PANEL_Y1 - PANEL_Y0,
            fc=PANEL, ec="#9FADB6", lw=0.9, radius=0.55, zorder=1)

    ax.text(CHIP_X0, HDR_TITLE_Y, "PLUGIN", fontsize=FS_PANEL, color=INK,
            fontweight="bold", ha="left", va="center", zorder=5)
    ax.text(CHIP_X0, HDR_SUB_Y,
            "what it knows about this repository,\nfrom the sessions before this one",
            fontsize=FS_BODY, color=MUTED, ha="left", va="top",
            linespacing=1.30, zorder=5)
    ax.plot([CHIP_X0, CHIP_X1], [HDR_RULE] * 2, color=LINE, lw=0.6, zorder=4)

    for number, title, color, light, body in MECHANISMS:
        top = axis_y(number) + CHIP_H / 2
        rounded(ax, (CHIP_X0, top - CHIP_H), CHIP_X1 - CHIP_X0, CHIP_H,
                fc="white", ec=color, lw=0.85, radius=0.40, zorder=3)
        badge(ax, CHIP_X0 + 0.75, top - 1.05, str(number), color)
        room = CHIP_X1 - (CHIP_X0 + 1.80) - 0.30
        used = width_of(fig, title, FS_CHIP, weight="bold")
        assert used <= room, f"card {number} title is {used:.2f} units, room {room:.2f}"
        ax.text(CHIP_X0 + 1.80, top - 1.05, title, fontsize=FS_CHIP, color=color,
                fontweight="bold", ha="left", va="center", zorder=5)
        body_room = CHIP_X1 - CHIP_X0 - 0.70
        body_used = max(width_of(fig, line, FS_BODY) for line in body)
        assert body_used <= body_room, (
            f"card {number} body is {body_used:.2f} units, room {body_room:.2f}")
        ax.text(CHIP_X0 + 0.35, top - 2.25, "\n".join(body), fontsize=FS_BODY,
                color=MUTED, ha="left", va="top", linespacing=1.30, zorder=5)


def draw_learners(fig, ax):
    rounded(ax, (CTRL_X0, LEARN_Y0), CTRL_X1 - CTRL_X0, LEARN_Y1 - LEARN_Y0,
            fc="white", ec="#9FADB6", lw=0.85, radius=0.45, zorder=3)
    header = "SESSION END"
    ax.text(CHIP_X0, LEARN_Y1 - 1.00, header, fontsize=FS_ROW, color=INK,
            fontweight="bold", ha="left", va="center", zorder=5)
    first_badge = CHIP_X1 - 0.58 - 2 * 1.45 - 0.58
    assert CHIP_X0 + width_of(fig, header, FS_ROW, weight="bold") <= first_badge - 0.30, header
    for i, (number, _, color, _, _) in enumerate(MECHANISMS):
        badge(ax, CHIP_X1 - 0.58 - (2 - i) * 1.45, LEARN_Y1 - 1.00, str(number),
              color, radius=0.58, fs=4.0)
    ax.text(CHIP_X0, LEARN_Y1 - 2.05,
            "its log updates what all three\nmechanisms know about the repository",
            fontsize=FS_BODY, color=MUTED, ha="left", va="top",
            linespacing=1.30, zorder=5)

    arrow(ax, (CHIP_X0 + 1.55, LEARN_Y1 + 0.05), (CHIP_X0 + 1.55, PANEL_Y0 - 0.05),
          color=MUTED, zorder=7)
    ax.text(CHIP_X0 + 2.30, (LEARN_Y1 + PANEL_Y0) / 2, "for the next session",
            fontsize=FS_SMALL, color=MUTED, ha="left", va="center", zorder=7)


# ---- right column -----------------------------------------------------------

def draw_agent_header(fig, ax):
    title = "YOUR CODING AGENT"
    ax.text(AIN_X0, HDR_TITLE_Y, title, fontsize=FS_PANEL, color=INK,
            fontweight="bold", ha="left", va="center", zorder=5)
    note_x = AIN_X0 + width_of(fig, title, FS_PANEL, weight="bold") + 0.70
    ax.text(note_x, HDR_TITLE_Y, "(unchanged)", fontsize=FS_BODY, color=MUTED,
            ha="left", va="center", zorder=5)
    left = tag(fig, ax, AIN_X1, HDR_TITLE_Y, "Claude Code  ·  Codex  ·  Copilot CLI  ·  Pi",
               INK, "#E7ECEF", weight="normal")
    assert note_x + width_of(fig, "(unchanged)", FS_BODY) <= left - 0.50, "header collides"
    ax.plot([AIN_X0, AIN_X1], [HDR_RULE] * 2, color=LINE, lw=0.6, zorder=4)


def row_title(fig, ax, number, eyebrow, title, color, timing, light):
    y = ROWS[number][1] - 0.65
    badge(ax, AIN_X0 + 0.55, y, str(number), color, radius=0.62, fs=4.2)
    x = AIN_X0 + 1.50
    ax.text(x, y, eyebrow, fontsize=FS_ROW, color=color, fontweight="bold",
            ha="left", va="center", zorder=5)
    x += width_of(fig, eyebrow, FS_ROW, weight="bold") + 0.65
    ax.text(x, y, title, fontsize=FS_ROW, color=INK, ha="left", va="center",
            zorder=5)
    tag(fig, ax, AIN_X1, y, timing, color, light)


def draw_prefix_row(fig, ax):
    row_title(fig, ax, 1, "KNOWLEDGE", "session context", SKILL,
              "at session start", SKILL_LIGHT)

    y = bar_y(1)
    skill_x, skill_w = AIN_X0 + 13.40, 10.40
    segments = [
        (AIN_X0, 13.40, NEUTRAL_FC, NEUTRAL_EC, "system prompt + your instructions",
         MUTED, "normal"),
        (skill_x, skill_w, SKILL_LIGHT, SKILL, "+ learned micro-skills",
         SKILL, "bold"),
        (skill_x + skill_w, AIN_X1 - skill_x - skill_w, "white", NEUTRAL_EC,
         "your prompt", MUTED, "normal"),
    ]
    for x, w, fc, ec, label, tc, weight in segments:
        ax.add_patch(Rectangle((x, y), w, BAR_H, facecolor=fc, edgecolor=ec,
                               linewidth=0.7, zorder=4))
        assert width_of(fig, label, FS_SMALL, weight=weight) <= w - 0.30, label
        ax.text(x + w / 2, y + BAR_H / 2, label, fontsize=FS_SMALL, color=tc,
                ha="center", va="center", fontweight=weight, zorder=5)
    annotate(ax, 1, skill_x, skill_x + skill_w,
             "active facts only", SKILL)


# Claude Code's names. The kept ones are the work surface the plugin never proposes; the
# dropped ones are tools it has proposed in practice. The web tools are never proposed.
TOOLCHIPS = [
    ("Bash", "kept"), ("Read", "kept"), ("Edit", "kept"), ("Write", "kept"),
    ("Grep", "kept"), ("Glob", "kept"),
    ("CronCreate", "dropped"), ("Workflow", "dropped"), ("NotebookEdit", "dropped"),
    (ELLIPSIS, "more"),
]
TOOL_GAP = 0.70


def draw_tools_row(fig, ax):
    row_title(fig, ax, 2, "INTERFACE", "loaded tool set", TOOLS,
              "from the next session", TOOLS_LIGHT)

    y = bar_y(2)
    # Chips are sized to their names; the padding is solved so the row ends flush.
    names = [width_of(fig, name, FS_MONO, family=MONO) for name, _ in TOOLCHIPS]
    pad = (AIN_X1 - AIN_X0 - TOOL_GAP * (len(names) - 1) - sum(names)) / len(names)
    assert pad >= 0.45, f"tool names leave {pad:.2f} units of padding"
    x, dropped = AIN_X0, []
    for (name, state), used in zip(TOOLCHIPS, names):
        w = used + pad
        if state == "more":
            # Drawn without a box: the rest of the proposal, not one more tool.
            ax.text(x + w / 2, y + BAR_H / 2, name, fontsize=FS_DOTS, color=DROPPED_TC,
                    ha="center", va="center", zorder=5)
            dropped.append((x, x + w))
            x += w + TOOL_GAP
            continue
        if state == "kept":
            fc, ec, tc = TOOLS_LIGHT, TOOLS, "#74500C"
        else:
            fc, ec, tc = DROPPED_FC, DROPPED_EC, DROPPED_TC
            dropped.append((x, x + w))
        rounded(ax, (x, y), w, BAR_H, fc=fc, ec=ec, lw=0.7, radius=0.26, zorder=4)
        ax.text(x + w / 2, y + BAR_H / 2, name, fontsize=FS_MONO, color=tc,
                family=MONO, ha="center", va="center", zorder=5)
        if state == "dropped":
            ax.plot([x + 0.28, x + w - 0.28], [y + BAR_H / 2] * 2,
                    color=DROPPED_TC, lw=0.6, zorder=6)
        x += w + TOOL_GAP
    annotate(ax, 2, dropped[0][0], dropped[-1][1], "denied, once you approve", DROPPED_TC)


# One prompt-turn, read left to right, in the order the guard acts: the same failure comes
# back, and only then is one redirect attached to a tool result; when it comes back again
# the turn is stopped (headless; interactive use gets a notice instead), so the calls after
# it are struck out exactly like a pruned tool. An ellipsis stands for the repetitions a
# schematic cannot show, so the two groups are not read as exactly two calls each.
TURNBOXES = [
    (ELLIPSIS, "earlier", 1.40, None),
    ("edit", "repeat", 2.55, "loop"),
    ("test", "repeat", 2.55, "loop"),
    (ELLIPSIS, "more", 1.40, "loop"),
    ("REDIRECT", "redirect", 3.70, None),
    ("edit", "repeat", 2.55, "again"),
    ("test", "repeat", 2.55, "again"),
    (ELLIPSIS, "more", 1.40, "again"),
    ("STOP", "abort", 3.30, None),
    ("edit", "dropped", 2.50, "dropped"),
    ("test", "dropped", 2.50, "dropped"),
]
ROLLOUT_NOTES = [
    ("loop", "repeated failures", SPIRAL),
    ("again", "still repeating", SPIRAL),
    ("dropped", "never run", DROPPED_TC),
]

BOX_STYLES = {                     # fill, edge, text, family, weight
    "repeat": (SPIRAL_LIGHT, SPIRAL, SPIRAL, MONO, "normal"),
    "redirect": (SPIRAL, SPIRAL, "white", None, "bold"),
    "abort": ("white", SPIRAL, SPIRAL, None, "bold"),
    "dropped": (DROPPED_FC, DROPPED_EC, DROPPED_TC, MONO, "normal"),
}


def draw_rollout_row(fig, ax):
    row_title(fig, ax, 3, "CONTROL", "one prompt-turn", SPIRAL,
              "during each turn", SPIRAL_LIGHT)

    y = bar_y(3)
    gap = flush_gap([w for _, _, w, _ in TURNBOXES])
    spans, x = {}, AIN_X0
    for label, state, w, group in TURNBOXES:
        if state in ("earlier", "more"):
            # Drawn without a box, so it reads as continuation rather than one turn.
            ax.text(x + w / 2, y + BAR_H / 2, label, fontsize=FS_DOTS,
                    color=SPIRAL if state == "more" else DROPPED_TC,
                    ha="center", va="center", zorder=5)
        else:
            fc, ec, tc, family, weight = BOX_STYLES[state]
            rounded(ax, (x, y), w, BAR_H, fc=fc, ec=ec,
                    lw=1.1 if state == "abort" else 0.7, radius=0.26, zorder=4)
            fs = FS_MARK if state in ("redirect", "abort") else FS_MONO
            assert width_of(fig, label, fs, weight=weight,
                            family=family) <= w - 0.30, label
            ax.text(x + w / 2, y + BAR_H / 2, label, fontsize=fs, color=tc,
                    family=family, fontweight=weight, ha="center", va="center",
                    zorder=5)
            if state == "dropped":
                ax.plot([x + 0.28, x + w - 0.28], [y + BAR_H / 2] * 2,
                        color=DROPPED_TC, lw=0.6, zorder=6)
        if group:
            spans.setdefault(group, [x, x + w])[1] = x + w
        x += w + gap

    placed = []
    for group, text, color in ROLLOUT_NOTES:
        x0, x1 = spans[group]
        annotate(ax, 3, x0, x1, text, color)
        half = width_of(fig, text, FS_SMALL, weight="bold") / 2
        placed.append(((x0 + x1) / 2 - half, (x0 + x1) / 2 + half))
    for (_, left_end), (right_start, _) in zip(placed, placed[1:]):
        assert right_start > left_end + 0.30, "rollout notes collide"


def draw_agent(fig, ax):
    rounded(ax, (AGENT_X0, PANEL_Y0), AGENT_X1 - AGENT_X0, PANEL_Y1 - PANEL_Y0,
            fc="white", ec="#9FADB6", lw=0.9, radius=0.55, zorder=1)
    draw_agent_header(fig, ax)
    draw_prefix_row(fig, ax)
    draw_tools_row(fig, ax)
    draw_rollout_row(fig, ax)


# ---- wiring -----------------------------------------------------------------

def draw_control_arrows(ax):
    for number, _, color, _, _ in MECHANISMS:
        y = axis_y(number)
        arrow(ax, (CTRL_X1 + 0.10, y), (AGENT_X0 + 1.05, y), color=color, zorder=7)


def draw_return(ax):
    ax.plot([LEG_X, LEG_X], [PANEL_Y0, LEG_Y], color=MUTED, lw=0.85, zorder=6)
    arrow(ax, (LEG_X, LEG_Y), (CTRL_X1 + 0.20, LEG_Y), color=MUTED, zorder=6)
    ax.text((CTRL_X1 + LEG_X) / 2, LEG_Y + 0.55, "the finished session's log",
            fontsize=FS_SMALL, color=MUTED, ha="center", va="bottom", zorder=6)


def main():
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        # Outline SVG glyphs so the vector asset is portable and cannot fall back
        # to a substituted/rasterized font in downstream viewers.
        "svg.fonttype": "path",
        "svg.hashsalt": "framework_overview",   # stable element ids across runs
        "pdf.fonttype": 42,         # embed TrueType text in PDF
        "ps.fonttype": 42,
        "axes.unicode_minus": False,
    })

    fig = plt.figure(figsize=(FIG_W, FIG_H))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, XMAX)
    ax.set_ylim(0, YMAX)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    draw_controller(fig, ax)
    draw_learners(fig, ax)
    draw_agent(fig, ax)
    draw_control_arrows(ax)
    draw_return(ax)

    base = OUT_DIR / "framework_overview"
    # No creation date in the SVG, so regenerating an unchanged figure changes no file.
    fig.savefig(base.with_suffix(".png"), dpi=PNG_DPI, facecolor="white")
    fig.savefig(base.with_suffix(".svg"), facecolor="white", metadata={"Date": None})
    print(f"wrote {base.with_suffix('.png')}")
    print(f"      {base.with_suffix('.svg')}")


if __name__ == "__main__":
    main()

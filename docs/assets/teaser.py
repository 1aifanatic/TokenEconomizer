#!/usr/bin/env python3
"""The teaser: TokenEconomizer's title card, how it trims the waste of a repository's tasks, and
the paper's headline results.

    python docs/assets/teaser.py --plex FONTS             # the README's: teaser.gif and teaser.png
    python docs/assets/teaser.py --plex FONTS --page DIR  # the project page's: DIR/teaser.webm and
                                                          # .mp4, and its poster DIR/teaser.png

Needs Pillow and ffmpeg. Both versions are the same animation, in the project page's colors and
type, IBM Plex: FONTS is a folder holding IBMPlexSans-{Regular,SemiBold,Italic} and
IBMPlexMono-{Regular,SemiBold} as .woff or .ttf (`npm pack @ibm/plex-sans @ibm/plex-mono` has them).

Three acts, crossfaded:

Title    the page hero's two-tone name, the paper's subtitle, the three mechanisms in the paper's
         order, the agents it works with, and the authors' institutions' logos (logo_*.png: the
         project page's SVGs, rasterized with a transparent background, as Pillow draws no SVG).
Method   a sketch, not a measurement: six made-up tasks, alike, each a bar of the work itself and
         three kinds of waste, named beside the first bar, under a dashed line at what a task costs
         without TokenEconomizer. On the right, what TokenEconomizer has learned: tasks 1-3 each
         fly into it and teach it one mechanism, with a plain example; in the next task that waste
         shrinks while its card lights up, and the last tasks start lean. No number in it comes
         from a run (runs stay out of the repository); the waste is sized so the last tasks land
         inside the paper's range of savings, and nothing is labelled with a number.
Results  the paper's SWE-Bench headline, as the README's line under the badges and the project
         page's tiles state it, and per agent-model combination the cost cut and the change in
         Pass@1. The numbers are results_overview.py's ROWS (the paper's Table 1), not copied.

Both loop, fading from the results back to the title: the README's GIF, and the page's video (the
page plays it while it is in view). The still -- the README's for reduced motion, the page's
poster -- is the method's last frame, the one that explains the most on its own.

All of it is drawn on a light tint, not pure white, with white cards on it.

Every frame is drawn at SS times its size and box-filtered down, which is the anti-aliasing.
Anything that could outgrow its box is asserted.
"""

import argparse
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from results_overview import ROWS         # the paper's headline numbers, kept in one place

OUT_DIR = Path(__file__).resolve().parent

W, H = 1120, 600            # logical pixels, the README's column at about 0.8x
GIF_SCALE = 1.25            # the GIF's pixels per logical pixel: sharp at the README's width
PAGE_SCALE = 2              # the video's
SS = 3                      # supersampling factor
FPS = 25


# ---- look -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Look:
    """Colors and type."""
    bg: str
    ink: str
    muted: str
    faint: str
    line: str
    panel: str
    s1: str                 # micro-skills
    s1_light: str
    s2: str                 # tool pruning
    s2_light: str
    s3: str                 # the loop guard
    s3_light: str
    work: str
    fonts: dict             # style -> font file
    accent: str = "#285E9F"     # the page's accent, and TokenEconomizer's mark in its charts
    accent_soft: str = "#E5EDF7"
    good: str = "#2B7149"       # the page's green, for Pass@1 that went up
    dropped_fc: str = "#EDEFF1"
    dropped_ec: str = "#BAC4CA"
    dropped_tc: str = "#98A4AC"


def plex(folder):
    """The page's type, with SemiBold as its bold (the weight its headings use)."""
    want = {"": "IBMPlexSans-Regular", "bold": "IBMPlexSans-SemiBold",
            "italic": "IBMPlexSans-Italic", "mono": "IBMPlexMono-Regular",
            "mono bold": "IBMPlexMono-SemiBold"}
    found = {}
    for style, name in want.items():
        hits = [p for ext in (".ttf", ".woff") for p in Path(folder).rglob(name + ext)]
        if not hits:
            raise SystemExit(f"no {name}.ttf or .woff under {folder}")
        found[style] = hits[0]
    return found


# The project page's light tokens (assets/css/style.css; its figures stay light in dark mode).
def page_look(fonts):
    return Look(
        bg="#F2F5F9", ink="#132031", muted="#5D6978", faint="#A9B2BD", line="#C3CCD6",
        panel="#FFFFFF", s1="#3E73A8", s1_light="#E6EEF7", s2="#A86C0C", s2_light="#F8EEDB",
        s3="#B84843", s3_light="#F8E5E3", work="#C9D1DA", fonts=fonts)


L = None                    # the look being drawn; main() sets it, with the fonts it is given


# ---- the tasks ------------------------------------------------------------------------------
# Every task does the same work, and wastes the same three things until TokenEconomizer has
# learned how not to: cost units, the full amount and what is left once it has.
N = 6
WORK = 160
MECHS = ("facts", "prune", "guard")         # in the paper's order, top to bottom on each bar
FULL = {"facts": 40, "prune": 44, "guard": 40}
LEFT = {"facts": 8, "prune": 22, "guard": 16}
LEARNED_AFTER = {"facts": 1, "prune": 2, "guard": 3}    # the task that teaches it
NAME = {"facts": "Micro-skills", "prune": "Tool pruning", "guard": "Failure-loop intervention"}
WASTE = {"facts": "re-learning the repo", "prune": "tools never used",
         "guard": "stuck in loops"}
DOES = {"facts": "Remembers how the repo works", "prune": "Skips tools never used here",
        "guard": "Spots loops and steps in"}
CAPTION = {"facts": "told to the agent as each task starts",
           "prune": "unused tools are no longer loaded",
           "guard": "a nudge when the same error repeats"}


def color(m):
    return {"facts": L.s1, "prune": L.s2, "guard": L.s3}[m]


def light(m):
    return {"facts": L.s1_light, "prune": L.s2_light, "guard": L.s3_light}[m]


# ---- layout (logical pixels) --------------------------------------------------------------
CX0, CX1 = 40, 672                          # the chart
TITLE_Y = 46
GX = 202                                    # the waste's names end here
BX0 = 226                                   # the bars start here
PITCH = (CX1 - BX0) / N
BAR_W = 46
BASE_Y = 528                                # the bars' baseline
UNIT = 1.52                                 # pixels per cost unit
TASK_Y = 550
FULL_Y = BASE_Y - (WORK + sum(FULL.values())) * UNIT     # a task without TokenEconomizer
LEAN_Y = BASE_Y - (WORK + sum(LEFT.values())) * UNIT

PX0, PX1, PY0, PY1 = 704, 1088, 24, 576     # TokenEconomizer's panel
CARD_X0, CARD_X1 = 720, 1072
CARD_TOP = {"facts": 112, "prune": 266, "guard": 420}
CARD_H = 140


def col_x(i):
    return BX0 + PITCH * (i - 0.5)


# ---- timeline (frames) ----------------------------------------------------------------------
TITLE_LEN, METHOD_HOLD, RESULTS_LEN = 80, 36, 130      # each act, and the method's hold at its end
XFADE, FADE = 8, 10                                 # from one act to the next; the loop's
INTRO, GROW_FIRST, GROW, NAMES = 8, 22, 12, 18
TRIM, AFTER_TRIM, LINE = 14, 6, 12
FLY, LEARN, READ = 12, 12, 16
LEAN_GAP, SAVED = 3, 12


def schedule():
    """When each step of the method starts, in frames from the method's first."""
    t, at = INTRO, {}
    for i in range(1, N + 1):
        at["grow", i] = t
        t += GROW_FIRST + NAMES if i == 1 else GROW
        if any(LEARNED_AFTER[m] == i - 1 for m in MECHS):
            at["trim", i] = t
            t += TRIM + AFTER_TRIM
        learns = [m for m in MECHS if LEARNED_AFTER[m] == i]
        if learns:
            at["fly", i] = t
            t += FLY
            for m in learns:
                at["learn", m] = t
            t += LEARN + READ
        else:
            t += LEAN_GAP
    at["saved"] = t
    t += SAVED
    at["end"] = t
    return at


AT = schedule()


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def ease_out(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def ease_in_out(x):
    x = clamp(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def back_out(x):
    x = clamp(x)
    c = 1.9
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2


def progress(f, start, length):
    return clamp((f - start) / length)


def mix(c0, c1, t):
    a, b = rgb(c0), rgb(c1)
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def rgb(color):
    return tuple(int(color[k:k + 2], 16) for k in (1, 3, 5))


def amount(m, i, f):
    """What task i spends on waste m at frame f: all of it until TokenEconomizer has learned
    from an earlier task, shrinking in the first task after, and what is left from then on."""
    after = LEARNED_AFTER[m]
    if after >= i:
        return FULL[m]
    if after == i - 1:
        return FULL[m] - (FULL[m] - LEFT[m]) * ease_in_out(progress(f, AT["trim", i], TRIM))
    return LEFT[m]


def trim_glow(m, f):
    """How lit m's card is while the task after it learned shrinks m's waste: 0 to 1 and back."""
    i = LEARNED_AFTER[m] + 1
    if i > N or ("trim", i) not in AT:
        return 0.0
    return math.sin(math.pi * progress(f, AT["trim", i] - 2, TRIM + 8))


# ---- drawing --------------------------------------------------------------------------------
class Painter:
    """Logical coordinates in, a canvas k times their size out."""

    _fonts = {}
    _pictures = {}

    def __init__(self, k, base=None):
        self.k = k
        self.im = base.copy() if base is not None else Image.new(
            "RGB", (round(W * k), round(H * k)), L.bg)
        self.d = ImageDraw.Draw(self.im, "RGBA")

    def font(self, size, style=""):
        path = L.fonts[style]
        key = (path, size, self.k)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(str(path), round(size * self.k))
        return self._fonts[key]

    def width(self, text, size, style=""):
        return self.font(size, style).getlength(text) / self.k

    def _c(self, color, alpha=1.0):
        return (*rgb(color), round(255 * clamp(alpha)))

    def _box(self, box):
        x0, y0, x1, y1 = (v * self.k for v in box)
        return (x0, y0, max(x0, x1), max(y0, y1))

    def rect(self, box, fill=None, outline=None, width=1.0, radius=0.0, alpha=1.0):
        if alpha <= 0:
            return
        b = self._box(box)
        kw = dict(fill=self._c(fill, alpha) if fill else None,
                  outline=self._c(outline, alpha) if outline else None,
                  width=max(1, round(width * self.k)))
        if radius:
            self.d.rounded_rectangle(b, radius=radius * self.k, **kw)
        else:
            self.d.rectangle(b, **kw)

    def circle(self, cx, cy, r, fill=None, outline=None, width=1.0, alpha=1.0):
        if alpha > 0 and r > 0:
            self.rect((cx - r, cy - r, cx + r, cy + r), fill, outline, width, radius=r, alpha=alpha)

    def line(self, pts, color, width=1.0, alpha=1.0):
        if alpha > 0:
            self.d.line([(x * self.k, y * self.k) for x, y in pts], fill=self._c(color, alpha),
                        width=max(1, round(width * self.k)), joint="curve")

    def picture(self, name, x, cy, height, alpha=1.0):
        """A transparent PNG from this folder, `height` tall, from x and centred on cy."""
        key = (name, height, self.k)
        if key not in self._pictures:
            im = Image.open(OUT_DIR / name).convert("RGBA")
            h = round(height * self.k)
            self._pictures[key] = im.resize((round(im.width * h / im.height), h), Image.LANCZOS)
        im = self._pictures[key]
        if alpha > 0:
            mask = im.getchannel("A").point(lambda v: round(v * clamp(alpha)))
            self.im.paste(im.convert("RGB"), (round(x * self.k), round((cy - height / 2) * self.k)),
                          mask)

    def arc(self, cx, cy, r, start, end, color, width=2.0, alpha=1.0):
        if alpha > 0:
            self.d.arc(self._box((cx - r, cy - r, cx + r, cy + r)), start, end,
                       fill=self._c(color, alpha), width=max(1, round(width * self.k)))

    def text(self, xy, text, size, color, style="", anchor="lm", alpha=1.0):
        if alpha > 0:
            self.d.text((xy[0] * self.k, xy[1] * self.k), text, font=self.font(size, style),
                        fill=self._c(color, alpha), anchor=anchor)

    def dashed(self, a, b, color, width=1.2, dash=5.0, gap=4.0, alpha=1.0):
        length = math.dist(a, b)
        s = 0.0
        while s < length:
            e = min(length, s + dash)
            self.line([(a[0] + (b[0] - a[0]) * u / length, a[1] + (b[1] - a[1]) * u / length)
                       for u in (s, e)], color, width, alpha)
            s = e + gap

    def dashed_rect(self, box, color, width=1.2, dash=5.0, gap=4.0, alpha=1.0):
        x0, y0, x1, y1 = box
        for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)),
                     ((x0, y1), (x0, y0))):
            self.dashed(a, b, color, width, dash, gap, alpha)

    def check(self, cx, cy, r, color, width=2.2, alpha=1.0):
        """A check mark drawn as lines (not every typeface has one)."""
        self.line([(cx + r * u, cy + r * v) for u, v in ((-1, 0), (-0.3, 0.7), (1, -0.7))],
                  color, width, alpha)

    def loop_icon(self, cx, cy, r, color, alpha=1.0):
        """A circling arrow, drawn (not every typeface has one)."""
        self.arc(cx, cy, r, -50, 250, color, 1.8, alpha)
        a = math.radians(250)                   # the end, heading clockwise (y points down)
        tip = (cx + r * math.cos(a), cy + r * math.sin(a))
        head = math.atan2(math.cos(a), -math.sin(a))
        wings = [(tip[0] - 4.5 * math.cos(head + s), tip[1] - 4.5 * math.sin(head + s))
                 for s in (0.6, -0.6)]
        self.line([wings[0], tip, wings[1]], color, 1.8, alpha)

    def chip(self, x, cy, parts, *, fc, ec, h=26, pad=9, strike=False, icon=None, alpha=1.0):
        """A rounded box holding runs of (text, size, style, color), after a drawn icon if one is
        given; struck through for a tool left out. Returns its right edge."""
        lead = 20 if icon else 0
        w = lead + sum(self.width(t, z, st) for t, z, st, _ in parts) + 2 * pad
        self.rect((x, cy - h / 2, x + w, cy + h / 2), fc, ec, 1.4, radius=5, alpha=alpha)
        if icon:
            icon(x + pad + 7, cy)
        tx = x + pad + lead
        for t, z, st, c in parts:
            self.text((tx, cy), t, z, c, st, anchor="lm", alpha=alpha)
            tx += self.width(t, z, st)
        if strike:
            self.line([(x + pad - 3, cy), (x + w - pad + 3, cy)], parts[0][3], 1.3, alpha)
        return x + w


def draw_static(p):
    """What never moves in the method: the title, the baseline, the panel and its header."""
    title = "Each finished task makes the next ones waste less"
    p.text((CX0, TITLE_Y), title, 22, L.ink, "bold", anchor="ls")
    assert CX0 + p.width(title, 22, "bold") <= CX1, "the title outgrows the chart"
    p.line([(BX0 - 8, BASE_Y), (CX1, BASE_Y)], L.faint, 1.4)
    p.rect((PX0, PY0, PX1, PY1), L.panel, L.line, 1.6, radius=14)
    p.text((CARD_X0 + 4, 56), "TokenEconomizer", 20, L.ink, "bold")
    p.text((CARD_X0 + 4, 82), "learns from each finished task", 15, L.muted)
    p.line([(CARD_X0, 100), (CARD_X1, 100)], L.line, 1.2)


def stack(i, f):
    """Task i's bar, bottom to top, as (units, color, waste or None)."""
    return [(WORK, L.work, None)] + [(amount(m, i, f), color(m), m) for m in reversed(MECHS)]


def draw_bar(p, i, f):
    start = AT["grow", i]
    grow = GROW_FIRST if i == 1 else GROW
    p.text((col_x(i), TASK_Y), f"task {i}", 14, mix(L.faint, L.ink, clamp((f - start) / 4)),
           anchor="mm")
    assert p.width(f"task {i}", 14) <= PITCH - 8, "task labels meet"
    if f < start:
        return
    segs = stack(i, f)
    total = sum(v for v, _, _ in segs)
    shown = total * ease_out(progress(f, start, grow))
    x0, x1 = col_x(i) - BAR_W / 2, col_x(i) + BAR_W / 2
    y = BASE_Y
    for n, (v, c, _) in enumerate(segs):
        v = min(v, shown)
        if v <= 0:
            break
        p.rect((x0, y - v * UNIT, x1, y - (1.4 if n else 0.0)), c)
        y -= v * UNIT
        shown -= v
    # While waste shrinks, what is cut fades out above the bar in its color.
    if ("trim", i) in AT:
        t = progress(f, AT["trim", i], TRIM)
        m = next(m for m in MECHS if LEARNED_AFTER[m] == i - 1)
        cut = FULL[m] - amount(m, i, f)
        if 0 < t < 1 and cut > 0.3:
            p.rect((x0, y - cut * UNIT, x1, y - 1.4), color(m), alpha=0.35 * (1 - t))
    if i == 1:
        draw_names(p, segs, total * ease_out(progress(f, start, grow)))


def draw_names(p, segs, shown):
    """The first bar's parts, named beside it as it rises past each one."""
    below, x = 0.0, col_x(1) - BAR_W / 2
    for v, c, m in segs:
        mid = below + v / 2
        a = clamp((shown - mid) / 12)
        y = BASE_Y - mid * UNIT
        label = WASTE[m] if m else "the work itself"
        tc = color(m) if m else L.muted
        style = "bold" if m else ""
        p.text((GX, y), label, 15, tc, style, anchor="rm", alpha=a)
        assert GX - p.width(label, 15, style) >= CX0 - 16, f"{label!r} outgrows the gutter"
        p.line([(GX + 6, y), (x - 5, y)], mix(L.bg, tc, 0.6), 1.2, alpha=a)
        below += v


def draw_without(p, f):
    """The dashed line at a task's full cost, drawn when TokenEconomizer first trims one."""
    if f < AT["trim", 2]:
        return
    x0 = col_x(1) + BAR_W / 2 + 6
    end = x0 + (CX1 - x0) * ease_out(progress(f, AT["trim", 2], LINE))
    p.dashed((x0, FULL_Y), (end, FULL_Y), L.muted, 1.3)
    label = "without TokenEconomizer"
    p.text((CX1, FULL_Y - 13), label, 14, L.muted, "italic", anchor="rm",
           alpha=progress(f, AT["trim", 2] + LINE - 4, 8))


def draw_saved(p, f):
    a = progress(f, AT["saved"], SAVED)
    if a <= 0:
        return
    x, mid = col_x(N), (FULL_Y + LEAN_Y) / 2
    reach = (LEAN_Y - FULL_Y) / 2 - 5
    r = reach * ease_out(a)
    for s in (-1, 1):
        tip = mid + s * r
        p.line([(x, mid + s * 11), (x, tip)], L.ink, 1.5, alpha=a)
        p.line([(x - 4, tip - s * 5), (x, tip), (x + 4, tip - s * 5)], L.ink, 1.5, alpha=a)
    p.rect((x - 26, mid - 10, x + 26, mid + 10), L.bg, alpha=a)
    p.text((x, mid), "saved", 14, L.ink, "bold", anchor="mm", alpha=a)
    assert p.width("saved", 14, "bold") <= 52


def draw_card(p, m, f):
    top, c = CARD_TOP[m], color(m)
    box = (CARD_X0, top, CARD_X1, top + CARD_H)
    a = ease_in_out(progress(f, AT["learn", m], LEARN))
    if a < 1:
        p.dashed_rect(box, L.faint, 1.2, alpha=1 - a)
    if a <= 0:
        return
    glow = trim_glow(m, f)
    pop = 1 - math.exp(-6 * progress(f, AT["learn", m], LEARN + 8))
    p.rect(box, mix("#FFFFFF", light(m), 0.7 * glow + 0.5 * (1 - pop)), c, 1.8 + 1.4 * glow,
           radius=10, alpha=a)
    dy = 8 * (1 - a)
    x, y = CARD_X0 + 20, top + dy
    p.circle(x + 10, y + 24, 10.5, c, alpha=a)
    p.text((x + 10, y + 24.5), str(MECHS.index(m) + 1), 12, "#FFFFFF", "bold", anchor="mm",
           alpha=a)
    p.text((x + 28, y + 24), NAME[m], 14, c, "bold", alpha=a)
    s = back_out(progress(f, AT["learn", m] + 4, 9))
    p.circle(CARD_X1 - 24, top + 24, 11 * s, c, alpha=a)
    p.check(CARD_X1 - 24, top + 24, 4.8 * s, "#FFFFFF", alpha=a if s > 0.3 else 0)
    assert x + 28 + p.width(NAME[m], 14, "bold") <= CARD_X1 - 44, f"{m}'s name meets its check"
    p.text((x, y + 53), DOES[m], 17, L.ink, "bold", alpha=a)
    assert x + p.width(DOES[m], 17, "bold") <= CARD_X1 - 16, f"{m}'s line outgrows its card"
    right = draw_example(p, m, x, y + 88, a)
    assert right <= CARD_X1 - 16, f"{m}'s example outgrows its card"
    p.text((x, y + 119), CAPTION[m], 13, L.muted, alpha=a)
    assert x + p.width(CAPTION[m], 13) <= CARD_X1 - 16, f"{m}'s caption outgrows its card"


def draw_example(p, m, x, cy, a):
    """Each card's example, in plain terms: a fact it was told, the tools it kept and the ones
    left out, and an error that keeps coming back meeting a nudge. Returns its right edge."""
    if m == "facts":
        return p.chip(x, cy, [("Run tests with ", 13, "", L.s1),
                              ("python -m pytest", 13, "mono bold", L.s1)],
                      fc=L.s1_light, ec=L.s1, alpha=a)
    if m == "prune":
        for tool in ("Read", "Edit"):
            x = p.chip(x, cy, [(tool, 13, "", L.ink)], fc="#FFFFFF", ec=L.line, pad=8,
                       alpha=a) + 6
        for tool in ("Notebook", "Browser"):
            x = p.chip(x, cy, [(tool, 13, "", L.dropped_tc)], fc=L.dropped_fc, ec=L.dropped_ec,
                       pad=8, strike=True, alpha=a) + 6
        return x - 6
    x = p.chip(x, cy, [("same error ×3", 13, "", L.s3)], fc=L.s3_light, ec=L.s3, pad=8,
               icon=lambda ix, iy: p.loop_icon(ix, iy, 6, L.s3, a), alpha=a)
    p.text((x + 13, cy), "→", 16, L.s3, "bold", anchor="mm", alpha=a)
    return p.chip(x + 26, cy, [("try another way", 13, "bold", "#FFFFFF")], fc=L.s3, ec=L.s3,
                  pad=9, alpha=a)


def draw_fly(p, i, f):
    if ("fly", i) not in AT:
        return
    t = progress(f, AT["fly", i], FLY)
    if f < AT["fly", i] or t >= 1:
        return
    top = BASE_Y - sum(v for v, _, _ in stack(i, f)) * UNIT
    m = next(m for m in MECHS if LEARNED_AFTER[m] == i)
    x0, y0 = col_x(i), top - 8
    x1, y1 = CARD_X0 - 2, CARD_TOP[m] + CARD_H / 2
    cxp, cyp = (x0 + x1) / 2, max(100, min(y0, y1) - 70)

    def at(u):
        return ((1 - u) ** 2 * x0 + 2 * (1 - u) * u * cxp + u ** 2 * x1,
                (1 - u) ** 2 * y0 + 2 * (1 - u) * u * cyp + u ** 2 * y1)
    u = ease_in_out(t)
    for k in range(5, 0, -1):
        p.circle(*at(clamp(u - 0.045 * k)), 5.5 - 0.7 * k, L.ink, alpha=0.14 * (6 - k))
    p.circle(*at(u), 5.5, L.ink)


def render(f, k, base):
    """The method at frame f."""
    p = Painter(k, base)
    draw_without(p, f)
    for i in range(1, N + 1):
        draw_bar(p, i, f)
    draw_saved(p, f)
    for m in MECHS:
        draw_card(p, m, f)
    for i in range(1, N + 1):
        draw_fly(p, i, f)
    return p.im.reduce(SS)


# ---- the title card ---------------------------------------------------------------------------
AGENTS = ("Claude Code", "Codex", "GitHub Copilot CLI", "Pi")
LOGOS = (("logo_berkeley.png", 40), ("logo_microsoft.png", 34))   # tall as the page hero's 26:22
LOGO_GAP = 48


def picture_width(name, height):
    with Image.open(OUT_DIR / name) as im:
        return im.width * height / im.height


def rise(f, start, length=14, by=12):
    """An entrance: how far it has faded in, and how far it still sits below its place."""
    a = ease_out(progress(f, start, length))
    return a, by * (1 - a)


def draw_title(p, f):
    cx = W / 2
    a, dy = rise(f, 2, 16, 16)
    head, tail, size = "Token", "Economizer", 76
    x = cx - p.width(head + tail, size, "bold") / 2
    p.text((x, 214 + dy), head, size, L.ink, "bold", anchor="ls", alpha=a)
    p.text((x + p.width(head, size, "bold"), 214 + dy), tail, size, L.accent, "bold", anchor="ls",
           alpha=a)
    u = ease_in_out(progress(f, 12, 14))
    if u > 0:
        p.line([(cx - 36 * u, 242), (cx + 36 * u, 242)], L.accent, 3)
    a, dy = rise(f, 14)
    sub = "Experience-driven black-box cost control for coding agents"
    p.text((cx, 284 + dy), sub, 24, L.muted, anchor="mm", alpha=a)
    assert p.width(sub, 24) <= W - 80, "the subtitle outgrows the card"
    # The three mechanisms, numbered as everywhere else, each a chip.
    chips = [(str(n + 1), NAME[m], color(m), light(m)) for n, m in enumerate(MECHS)]
    widths = [36 + p.width(name, 16, "bold") + 16 for _, name, _, _ in chips]
    x = cx - (sum(widths) + 14 * (len(chips) - 1)) / 2
    for n, ((num, name, c, soft), w) in enumerate(zip(chips, widths)):
        a, dy = rise(f, 24 + 5 * n, 12, 10)
        y = 358 + dy
        p.rect((x, y - 19, x + w, y + 19), "#FFFFFF", mix(soft, c, 0.55), 1.6, radius=19, alpha=a)
        p.circle(x + 19, y, 11, c, alpha=a)
        p.text((x + 19, y + 0.5), num, 13, "#FFFFFF", "bold", anchor="mm", alpha=a)
        p.text((x + 36, y), name, 16, c, "bold", alpha=a)
        x += w + 14
    a, dy = rise(f, 40)
    lead = "Works with  "
    names = "  ·  ".join(AGENTS)
    x = cx - (p.width(lead, 17) + p.width(names, 17, "bold")) / 2
    p.text((x, 434 + dy), lead, 17, L.muted, alpha=a)
    p.text((x + p.width(lead, 17), 434 + dy), names, 17, L.ink, "bold", alpha=a)
    a, dy = rise(f, 46)
    widths = [picture_width(name, h) for name, h in LOGOS]
    x = cx - (sum(widths) + LOGO_GAP * (len(LOGOS) - 1)) / 2
    for (name, h), w in zip(LOGOS, widths):
        p.picture(name, x, 506 + dy, h, alpha=a)
        x += w + LOGO_GAP


# ---- the results ---------------------------------------------------------------------------
CELLS = 30                  # the paper: cheaper than default in each of 6 repositories x 5 pairs
TILE_X = (40, 394, 748)
TILE_W, TILE_Y0, TILE_Y1 = 332, 84, 216
CHART_Y0, CHART_Y1 = 236, 556
ROW_Y0, ROW_PITCH = 312, 50
BAR_X0, BAR_X1 = 318, 900   # 0 to the largest cut's bar


def draw_results(p, f):
    a, dy = rise(f, 0, 12, 8)
    p.text((CX0, 56 + dy), "Measured on SWE-Bench Tasks", 26, L.ink, "bold", anchor="ls", alpha=a)
    cuts = [-cost for _, _, cost, _, _, _ in ROWS]
    up = sum(te > base for *_, base, te in ROWS)
    tiles = [
        (lambda t: (f"{min(cuts) * t:.1f}–{max(cuts) * t:.1f}", "%"), L.accent,
         ("lower mean cost, across five", "agent–model combinations")),
        (lambda t: (f"{round(CELLS * t)}", f" / {CELLS}"), L.ink,
         ("repository × combination cells", "cheaper than default")),
        (lambda t: (f"{round(up * t)}", f" of {len(ROWS)}"), L.ink,
         ("agent–model combinations where", "Pass@1 also improves")),
    ]
    for n, ((value, c, label), x) in enumerate(zip(tiles, TILE_X)):
        a, dy = rise(f, 6 + 5 * n, 12, 10)
        y0, y1 = TILE_Y0 + dy, TILE_Y1 + dy
        p.rect((x, y0, x + TILE_W, y1), "#FFFFFF", L.line, 1.2, radius=12, alpha=a)
        big, small = value(ease_out(progress(f, 10 + 5 * n, 26)))
        p.text((x + 22, y0 + 56), big, 44, c, "bold", anchor="ls", alpha=a)
        p.text((x + 22 + p.width(big, 44, "bold") + 2, y0 + 56), small, 24, L.muted, anchor="ls",
               alpha=a)
        for line, ly in zip(label, (y0 + 86, y0 + 108)):
            p.text((x + 22, ly), line, 15, L.muted, alpha=a)
            assert p.width(line, 15) <= TILE_W - 40, f"{line!r} outgrows its tile"
    a, dy = rise(f, 22, 12, 10)
    y0 = CHART_Y0 + dy
    p.rect((CX0, y0, W - 40, CHART_Y1 + dy), "#FFFFFF", L.line, 1.2, radius=12, alpha=a)
    p.text((CX0 + 22, y0 + 34), "Mean cost per task, lower than default", 16, L.ink, "bold",
           anchor="ls", alpha=a)
    p.text((W - 62, y0 + 34), "Pass@1 change", 14, L.muted, anchor="rs", alpha=a)
    scale = (BAR_X1 - BAR_X0) / max(cuts)
    for n, ((agent, model, cost, _, base, te), cut) in enumerate(zip(ROWS, cuts)):
        y = ROW_Y0 + ROW_PITCH * n + dy
        p.text((CX0 + 22, y), agent, 15, L.ink, "bold", alpha=a)
        p.text((CX0 + 22 + p.width(agent + "  ", 15, "bold"), y), model, 15, L.muted, alpha=a)
        assert CX0 + 22 + p.width(agent + "  ", 15, "bold") + p.width(model, 15) <= BAR_X0 - 14
        p.rect((BAR_X0, y - 11, BAR_X1, y + 11), L.accent_soft, radius=5, alpha=0.6 * a)
        t = ease_out(progress(f, 30 + 4 * n, 24))
        if t > 0:
            end = BAR_X0 + cut * scale * t
            p.rect((BAR_X0, y - 11, end, y + 11), L.accent, radius=5)
            p.text((end + 10, y), f"{cut * t:.1f}%", 15, L.accent, "bold", alpha=t)
        d = round(te - base, 1)
        p.text((W - 62, y), f"{'+' if d > 0 else '−'}{abs(d):.1f}", 15,
               L.good if d > 0 else L.muted, "bold", anchor="rm",
               alpha=ease_out(progress(f, 46 + 4 * n, 10)))
    assert BAR_X1 + 10 + p.width("30.9%", 15, "bold") <= W - 62 - p.width("+0.0", 15, "bold") - 24
    a, _ = rise(f, 64)
    p.text((W / 2, 582), "arXiv 2610.22732   ·   tokeneconomizer.github.io", 14, L.muted,
           anchor="mm", alpha=a)


def act(k, base, draw, f):
    p = Painter(k, base)
    draw(p, f)
    return p.im.reduce(SS)


def frames(scale):
    """Every frame, at `scale` times the logical size: the title, the method and the results,
    each fading into the next, and back to the title. The fade back stops a step short of the
    title's first frame, which the loop shows next.
    An act's frames after its last movement are the same image, drawn once."""
    k = SS * scale
    blank = Painter(k).im
    method = Painter(k)
    draw_static(method)
    acts = [
        (lambda f: act(k, blank, draw_title, f), TITLE_LEN, TITLE_LEN - 20),
        (lambda f: render(f, k, method.im), AT["end"] + METHOD_HOLD, AT["end"]),
        (lambda f: act(k, blank, draw_results, f), RESULTS_LEN, RESULTS_LEN - 50),
    ]
    last = None
    for draw, length, settled in acts:
        if last is not None:
            first = draw(0)
            for j in range(XFADE):
                yield Image.blend(last, first, ease_in_out((j + 1) / (XFADE + 1)))
        held = draw(settled)
        for f in range(length):
            yield held if f >= settled else draw(f)
        last = held
    first = act(k, blank, draw_title, 0)
    for j in range(FADE):
        yield Image.blend(last, first, ease_in_out((j + 1) / (FADE + 1)))


def still(scale, out):
    """The method's last frame, as a PNG: the page's poster, and the README's for reduced motion."""
    k = SS * scale
    base = Painter(k)
    draw_static(base)
    render(AT["end"], k, base.im).save(out, optimize=True)
    print(f"wrote {out}")


def encode(scale, outputs):
    """Draws the frames once and has ffmpeg write each (path, filters, options) from them."""
    with tempfile.TemporaryDirectory() as tmp:
        n = 0
        for n, im in enumerate(frames(scale)):
            im.save(Path(tmp) / f"{n:04d}.png")
        for out, filters, extra in outputs:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS),
                            "-i", str(Path(tmp) / "%04d.png"), "-vf", filters, *extra, str(out)],
                           check=True)
            print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB, {(n + 1) / FPS:.1f} s)")


def main():
    global L
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plex", metavar="FONTS", required=True, help="a folder with the IBM Plex fonts")
    ap.add_argument("--page", metavar="DIR", help="write the project page's version into DIR")
    args = ap.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("teaser.py needs ffmpeg on PATH")
    L = page_look(plex(args.plex))
    if args.page:
        out = Path(args.page)
        # VP9 for the browsers that play it (smaller, and in every Chromium), H.264 for the rest.
        encode(PAGE_SCALE, [
            (out / "teaser.webm", "format=yuv420p",
             ["-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0", "-row-mt", "1", "-an"]),
            (out / "teaser.mp4", "format=yuv420p",
             ["-c:v", "libx264", "-crf", "20", "-preset", "slow", "-tune", "animation",
              "-movflags", "+faststart", "-an"])])
        still(PAGE_SCALE, out / "teaser.png")
        return
    encode(GIF_SCALE, [(OUT_DIR / "teaser.gif",
                        "split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];"
                        "[b][p]paletteuse=dither=none:diff_mode=rectangle",
                        ["-loop", "0"])])
    still(2, OUT_DIR / "teaser.png")


if __name__ == "__main__":
    main()

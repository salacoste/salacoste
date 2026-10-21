#!/usr/bin/env python3
"""Build the animated SVG panels and live stats for the github.com/salacoste profile README.

Every panel is a self-contained SVG: text is converted to glyph outlines (shaped with
HarfBuzz, so kerning survives), motion uses SMIL only, and there is no CSS, JavaScript
or external request inside the images. That is the subset that survives GitHub's image
proxy in every browser. README.md is generated from profile.json; edit that, not the README.

Usage:
    python scripts/build.py            # fetch live data (GITHUB_TOKEN optional), render everything
    python scripts/build.py --offline  # render from data/github.json without touching the network
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "scripts" / "fonts"
ASSETS = ROOT / "assets"
CARDS = ASSETS / "cards"
CONFIG = ROOT / "profile.json"
CACHE = ROOT / "data" / "github.json"
README = ROOT / "README.md"

# --------------------------------------------------------------------------- palette
# One committed look: an armoured console floating in deep space. The panels read as
# dark plates on both GitHub themes, so there is a single file per panel.

C = {
    "void": "#04070d",
    "panel": "#0a1018",
    "panel_hi": "#121b27",
    "steel": "#1f2b3a",
    "steel_hi": "#3a4c62",
    "rivet": "#56697f",
    "ink": "#e2eaf3",
    "muted": "#93a3b6",
    "faint": "#5f7187",
    "hud": "#5cc8e6",
    "amber": "#f4a833",
    "hazard_dark": "#17120a",
    "sheath": "#1a2430",
    "rib": "#3b4b5f",
    "core_neutral": "#8ea2b8",
    "plug": "#283646",
    "plug_line": "#52667e",
    "led": "#4fe08f",
    "planet_core": "#0d2236",
    "planet_edge": "#060b13",
    "wires": {
        "graphite": "#6f8297",
        "orange": "#ff8b4d",
        "blue": "#5ba3ff",
        "magenta": "#e870d4",
        "green": "#4fd98c",
        "teal": "#3fd6dc",
    },
}
CORE_MIX = 0.55
GLOW = 0.38


def mix(hex_a: str, hex_b: str, t: float) -> str:
    """Blend hex_a toward hex_b by t (0..1)."""
    a = [int(hex_a[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


# --------------------------------------------------------------------------- type


class Font:
    """A TrueType face that can shape text with HarfBuzz and emit glyph outlines."""

    def __init__(self, filename: str, key: str, features: dict | None = None):
        path = FONTS / filename
        self.key = key
        self.features = features or {}
        self.hb_font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))
        self.tt = TTFont(str(path))
        self.upem = self.tt["head"].unitsPerEm
        self.glyphset = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self._outlines: dict[int, str] = {}

    def shape(self, text: str) -> list[tuple[int, int, int, int]]:
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb_font, buf, self.features)
        return [
            (info.codepoint, pos.x_advance, pos.x_offset, pos.y_offset)
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions)
        ]

    def width(self, text: str, size: float, tracking: float = 0.0) -> float:
        glyphs = self.shape(text)
        return (sum(g[1] for g in glyphs) + tracking * self.upem * max(len(glyphs) - 1, 0)) * size / self.upem

    def outline(self, gid: int) -> str:
        if gid not in self._outlines:
            pen = SVGPathPen(self.glyphset, ntos=lambda v: str(round(v)))
            self.glyphset[self.order[gid]].draw(pen)
            self._outlines[gid] = pen.getCommands()
        return self._outlines[gid]


class Type:
    def __init__(self):
        self.regular = Font("Barlow-Regular.ttf", "r")
        self.medium = Font("Barlow-Medium.ttf", "m")
        self.cond = Font("BarlowCondensed-SemiBold.ttf", "c")
        self.mono = Font("JetBrainsMono-Medium.ttf", "j", {"calt": False, "liga": False})


def wrap(font: Font, text: str, size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and font.width(trial, size) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


# --------------------------------------------------------------------------- svg document


def f(v: float) -> str:
    """Compact number formatting for SVG attributes."""
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class Svg:
    def __init__(self, width: int, height: int, title: str, desc: str):
        self.width = width
        self.height = height
        self.title = title
        self.desc = desc
        self.glyphs: dict[str, str] = {}
        self.defs: list[str] = []
        self.parts: list[str] = []

    def add(self, *chunks: str) -> None:
        self.parts.extend(chunks)

    def text(self, font: Font, s: str, x: float, y: float, size: float, fill: str,
             anchor: str = "start", tracking: float = 0.0, attrs: str = "", inner: str = "") -> str:
        glyphs = font.shape(s)
        k = size / font.upem
        extra = tracking * font.upem
        total = (sum(g[1] for g in glyphs) + extra * max(len(glyphs) - 1, 0)) * k
        if anchor == "end":
            x -= total
        elif anchor == "middle":
            x -= total / 2
        uses = []
        pen = 0.0
        for gid, adv, xo, yo in glyphs:
            if font.outline(gid):
                ref = f"{font.key}{gid}"
                self.glyphs.setdefault(ref, font.outline(gid))
                y_attr = f' y="{yo}"' if yo else ""
                uses.append(f'<use xlink:href="#{ref}" x="{f(pen + xo)}"{y_attr}/>')
            pen += adv + extra
        return (
            f'<g transform="translate({f(x)} {f(y)}) scale({k:.5f} {-k:.5f})" '
            f'fill="{fill}"{attrs}>{inner}{"".join(uses)}</g>'
        )

    def render(self) -> str:
        glyph_defs = "".join(f'<path id="{gid}" d="{d}"/>' for gid, d in self.glyphs.items())
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 {self.width} {self.height}" width="{self.width}" height="{self.height}" '
            f'role="img" aria-labelledby="title desc">'
            f'<title id="title">{escape(self.title)}</title><desc id="desc">{escape(self.desc)}</desc>'
            f"<defs>{''.join(self.defs)}{glyph_defs}</defs>{''.join(self.parts)}</svg>\n"
        )


# --------------------------------------------------------------------------- shared shapes


def plate_points(x0: float, y0: float, x1: float, y1: float, cut: float) -> str:
    """A plate with its top-left and bottom-right corners cut at 45 degrees."""
    pts = [(x0 + cut, y0), (x1, y0), (x1, y1 - cut), (x1 - cut, y1), (x0, y1), (x0, y0 + cut)]
    return " ".join(f"{f(x)},{f(y)}" for x, y in pts)


def hazard_pattern(pid: str = "hazard") -> str:
    return (
        f'<pattern id="{pid}" width="7" height="7" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate(45)"><rect width="7" height="7" fill="{C["hazard_dark"]}"/>'
        f'<rect width="3.5" height="7" fill="{C["amber"]}"/></pattern>'
    )


def rivet(x: float, y: float, r: float = 1.5) -> str:
    return (f'<circle cx="{f(x)}" cy="{f(y)}" r="{r}" fill="{C["steel"]}" stroke="{C["rivet"]}" '
            f'stroke-width="0.9"/>')


def blink(period: float, low: float = 0.25, begin: float = 0.0) -> str:
    return (
        f'<animate attributeName="opacity" values="1;1;{low};1" keyTimes="0;0.55;0.75;1" '
        f'dur="{period:.2f}s" begin="{begin:.2f}s" repeatCount="indefinite"/>'
    )


def fade_in(start: float, duration: float) -> str:
    """Opacity 0 until `start`, then ease to 1 and hold. Static renderers show the final frame."""
    total = start + duration
    return (
        f'<animate attributeName="opacity" values="0;0;1" keyTimes="0;{start / total:.4f};1" '
        f'dur="{total:.2f}s" fill="freeze" calcMode="spline" keySplines="0 0 1 1;0.25 0.1 0.25 1"/>'
    )


def grow(attr: str, start_value: float, end_value: float, start: float, duration: float) -> str:
    total = start + duration
    return (
        f'<animate attributeName="{attr}" values="{f(start_value)};{f(start_value)};{f(end_value)}" '
        f'keyTimes="0;{start / total:.4f};1" dur="{total:.2f}s" fill="freeze" calcMode="spline" '
        f'keySplines="0 0 1 1;0.45 0 0.2 1"/>'
    )


def star_icon(cx: float, cy: float, stroke: str) -> str:
    pts = []
    for n in range(10):
        r = 6.4 if n % 2 == 0 else 2.8
        a = -math.pi / 2 + n * math.pi / 5
        pts.append(f"{f(cx + r * math.cos(a))},{f(cy + r * math.sin(a))}")
    return (f'<polygon points="{" ".join(pts)}" fill="none" stroke="{stroke}" '
            f'stroke-width="1.4" stroke-linejoin="round"/>')


def fork_icon(cx: float, cy: float, stroke: str) -> str:
    return (
        f'<g fill="none" stroke="{stroke}" stroke-width="1.4" stroke-linecap="round">'
        f'<circle cx="{f(cx - 3.8)}" cy="{f(cy - 4.6)}" r="1.9"/>'
        f'<circle cx="{f(cx + 3.8)}" cy="{f(cy - 4.6)}" r="1.9"/>'
        f'<circle cx="{f(cx)}" cy="{f(cy + 5.2)}" r="1.9"/>'
        f'<path d="M{f(cx - 3.8)} {f(cy - 2.7)}v1.2q0 2.2 3.8 2.6q3.8-.4 3.8-2.6v-1.2M{f(cx)} {f(cy + 1.1)}v2.2"/>'
        f"</g>"
    )


def cable(d: str, core: str, anim: str = "", core_width: float = 2.0) -> str:
    """Armoured conduit: dark sheath, ribbed armour bands, glowing coloured core."""
    return (
        f'<path d="{d}" fill="none" stroke="{C["sheath"]}" stroke-width="6.5" stroke-linecap="round">{anim}</path>'
        f'<path d="{d}" fill="none" stroke="{C["rib"]}" stroke-width="6.5" stroke-dasharray="1.4 3.6">{anim}</path>'
        f'<path d="{d}" fill="none" stroke="{core}" stroke-width="{core_width}" stroke-linecap="round">{anim}</path>'
    )


def plug(colour: str) -> str:
    """A connector body drawn from its cable end at (0, 0) toward +x, tip at x=12."""
    return (
        f'<rect x="0" y="-4.6" width="12" height="9.2" rx="1.6" fill="{C["plug"]}" '
        f'stroke="{C["plug_line"]}" stroke-width="1"/>'
        f'<rect x="8.6" y="-4.6" width="2.4" height="9.2" fill="{colour}"/>'
        f'<path d="M2.5 -2.2H6.5M2.5 0H6.5M2.5 2.2H6.5" stroke="{C["plug_line"]}" stroke-width="0.8"/>'
    )


# --------------------------------------------------------------------------- timeline


EASE = {
    "lin": "0 0 1 1",
    "io": "0.45 0 0.25 1",
    "out": "0.15 0.6 0.3 1",
    "in": "0.6 0 0.9 0.45",
}


class Timeline:
    """Keyframed SMIL animations that loop with a shared period and start time."""

    def __init__(self, period: float, begin: float):
        self.period = period
        self.begin = begin

    def anim(self, attr: str, frames: list, tag: str = "animate", extra: str = "") -> str:
        frames = sorted(frames, key=lambda fr: fr[0])
        assert frames[0][0] == 0 and abs(frames[-1][0] - self.period) < 1e-9, "frames must span the period"
        keys, values, splines = [], [], []
        last = -1.0
        for n, fr in enumerate(frames):
            key = 0.0 if n == 0 else (1.0 if n == len(frames) - 1 else fr[0] / self.period)
            if n and key <= last:
                key = last + 1e-5
            assert key <= 1.0, "keyframes too close to the end of the period"
            keys.append(key)
            last = key
            values.append(str(fr[1]))
            if n:
                splines.append(EASE[fr[2] if len(fr) > 2 else "lin"])
        return (
            f'<{tag} attributeName="{attr}" values="{";".join(values)}" '
            f'keyTimes="{";".join(f"{k:.6f}" for k in keys)}" calcMode="spline" '
            f'keySplines="{";".join(splines)}" dur="{self.period:.2f}s" begin="{self.begin:.2f}s" '
            f'repeatCount="indefinite"{extra}/>'
        )

    def window(self, attr: str, off, on, windows: list[tuple[float, float]], ramp: float = 0.02) -> str:
        frames = [(0, off)]
        for a, b in windows:
            frames += [(a, off), (a + ramp, on), (b, on), (b + ramp, off)]
        frames.append((self.period, off))
        return self.anim(attr, frames)


def bezier_point(p, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
    u = 1 - t
    return (
        u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
        u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3,
    )


def bezier_length(p, steps: int = 400) -> float:
    length = 0.0
    prev = p[0]
    for i in range(1, steps + 1):
        cur = bezier_point(p, i / steps)
        length += math.dist(prev, cur)
        prev = cur
    return length


# --------------------------------------------------------------------------- hero


def starfield(svg: Svg, W: int, H: int, seed: int = 7) -> None:
    rng = random.Random(seed)
    stars = []
    for n in range(150):
        x, y = rng.uniform(4, W - 4), rng.uniform(4, H - 4)
        r = rng.choice([0.45, 0.6, 0.6, 0.8, 0.8, 1.0, 1.25])
        o = rng.uniform(0.18, 0.75)
        tint = rng.choice([C["ink"], C["ink"], C["ink"], C["hud"], "#ffd9a8"])
        if n < 16:
            dur = rng.uniform(2.2, 5.5)
            beg = rng.uniform(0, 4)
            stars.append(
                f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(r + 0.25)}" fill="{tint}" opacity="{f(o)}">'
                f'<animate attributeName="opacity" values="{f(o)};{f(o * 0.15)};{f(o)}" dur="{dur:.2f}s" '
                f'begin="{beg:.2f}s" repeatCount="indefinite"/></circle>'
            )
        else:
            stars.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(r)}" fill="{tint}" opacity="{f(o)}"/>')
    svg.add("".join(stars))


def hero(cfg: dict, t: Type) -> str:
    h = cfg["hero"]
    wires = C["wires"]
    W, H = 900, 468
    clients = h["clients"]
    systems = h["systems"]
    svg = Svg(
        W,
        H,
        h["headline"],
        f'{h["subline"]} Diagram: {", ".join(clients)} connect through {", ".join(h["sleeve"])} '
        f'to {", ".join(s["label"] for s in systems)}. A service droid rides a rail beside the system '
        f"connectors and plugs loose cables back in.",
    )

    # ------------------------------------------------------------------ geometry
    label_size = 15.5
    bot_scale = 1.4
    label_gap = 20 + 30 * bot_scale + 12
    widest = max(t.medium.width(s["label"], label_size) for s in systems)
    RX = min(700.0, W - 16 - label_gap - widest)
    LX = 150.0
    RAIL = RX + 20
    SY = 290.0
    spacing = 46.0
    lane = 8.0
    hub_h = 50.0
    band_pad = 21
    band_sizes = [t.medium.width(label, 15) + 2 * band_pad for label in h["sleeve"]]
    sleeve_w = sum(band_sizes)
    SX0 = (LX + RX) / 2 - sleeve_w / 2
    SX1 = SX0 + sleeve_w
    left_y = [SY + (i - (len(clients) - 1) / 2) * spacing for i in range(len(clients))]
    right_y = [SY + (j - (len(systems) - 1) / 2) * spacing for j in range(len(systems))]
    left_lane = [SY + (i - (len(clients) - 1) / 2) * lane for i in range(len(clients))]
    right_lane = [SY + (j - (len(systems) - 1) / 2) * lane for j in range(len(systems))]

    plug_len = 12.0
    left_start = LX + 5 + plug_len    # cable leaves the left plug here
    right_end = RX - 5 - plug_len     # cable enters the right plug here
    lpull = (SX0 - left_start) * 0.55
    rpull = (right_end - SX1) * 0.55

    left_curves = [((left_start, y), (left_start + lpull, y), (SX0 - lpull, left_lane[i]), (SX0, left_lane[i]))
                   for i, y in enumerate(left_y)]
    right_curves = [((SX1, right_lane[j]), (SX1 + rpull, right_lane[j]), (right_end - rpull, y), (right_end, y))
                    for j, y in enumerate(right_y)]

    def left_d(i: int) -> str:
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = left_curves[i]
        return f"M{f(x0)} {f(y0)}C{f(x1)} {f(y1)} {f(x2)} {f(y2)} {f(x3)} {f(y3)}L{f(x3 + 16)} {f(y3)}"

    def right_d(j: int, end=None, c2=None) -> str:
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = right_curves[j]
        if end is not None:
            x3, y3 = end
        if c2 is not None:
            x2, y2 = c2
        return (f"M{f(x0 - 16)} {f(y0)}L{f(x0)} {f(y0)}C{f(x1)} {f(y1)} {f(x2)} {f(y2)} "
                f"{f(x3)} {f(y3)}")

    def droop(j: int, low: float = 0.0):
        end = (right_end - 22, right_y[j] + 28 + low)
        return end, (end[0] + 2, end[1] - 36)

    # ------------------------------------------------------------------ signal schedule
    speed = 300.0
    stagger = 1.7
    routes = h["routes"]
    cycle = stagger * len(routes)
    first = 2.7
    t_left = [(bezier_length(c) + 16) / speed for c in left_curves]
    t_right = [(bezier_length(c) + 16) / speed for c in right_curves]
    t_sleeve = sleeve_w / speed

    # Repair events: in cycle c the pulse on route repairs[c] knocks its plug loose on arrival.
    repairs = h["repairs"]
    M = cycle * len(repairs)
    tl = Timeline(M, first)
    events = []
    for c, k in enumerate(repairs):
        i, j = routes[k]
        te = c * cycle + k * stagger + t_left[i] + t_sleeve + t_right[j]
        events.append({"j": j, "te": te})
    y_park = right_y[events[-1]["j"]]
    prev_y = y_park
    for e in events:
        dist = abs(right_y[e["j"]] - prev_y)
        e["depart"] = e["te"] + 0.4
        e["arrive"] = e["depart"] + (0.45 + dist / 150 if dist else 0.25)
        e["from"], e["to"] = prev_y - y_park, right_y[e["j"]] - y_park
        e["dist"] = right_y[e["j"]] - prev_y
        e["done"] = e["arrive"] + 1.95
        prev_y = right_y[e["j"]]
    for a, b in zip(events, events[1:] + [dict(events[0], te=events[0]["te"] + M)]):
        assert b["te"] - a["done"] > 0.4, "repair events overlap; pick other routes in profile.json"
    assert events[-1]["done"] + 0.3 < M

    # ------------------------------------------------------------------ defs
    frame = plate_points(1, 1, W - 1, H - 1, 22)
    svg.defs += [
        f'<clipPath id="frame"><polygon points="{frame}"/></clipPath>',
        f'<radialGradient id="planet" cx="0.3" cy="0.75" r="0.85">'
        f'<stop offset="0" stop-color="{C["planet_core"]}"/><stop offset="1" stop-color="{C["planet_edge"]}"/>'
        f"</radialGradient>",
        hazard_pattern(),
        f'<clipPath id="revealL"><rect x="0" y="0" width="{W}" height="{H}">'
        f'{grow("width", left_start - 6, W, 0.55, 0.9)}</rect></clipPath>',
        f'<clipPath id="revealR"><rect x="0" y="0" width="{W}" height="{H}">'
        f'{grow("width", SX1 - 18, W, 1.35, 0.9)}</rect></clipPath>',
    ]

    # ------------------------------------------------------------------ space
    svg.add(f'<polygon points="{frame}" fill="{C["void"]}"/>', '<g clip-path="url(#frame)">')
    starfield(svg, W, H)
    svg.add(
        f'<circle cx="{W + 60}" cy="-150" r="296" fill="none" stroke="{C["hud"]}" stroke-opacity="0.07" '
        f'stroke-width="12"/>'
        f'<circle cx="{W + 60}" cy="-150" r="290" fill="url(#planet)" stroke="{C["hud"]}" '
        f'stroke-opacity="0.38" stroke-width="1.2"/>'
        f'<path d="M{W - 210} 30 Q{W - 120} 70 {W - 40} 66" fill="none" stroke="{C["hud"]}" '
        f'stroke-opacity="0.08" stroke-width="10"/>',
        "</g>",
    )
    # HUD brackets on the two square corners
    svg.add(
        f'<path d="M{W - 40} 12H{W - 12}V40M12 {H - 40}V{H - 12}H40" fill="none" stroke="{C["hud"]}" '
        f'stroke-opacity="0.7" stroke-width="1.5"/>'
    )
    svg.add(f'<circle cx="36" cy="31" r="3" fill="{C["led"]}">{blink(1.6)}</circle>')
    svg.add(svg.text(t.mono, "salacoste / harness online", 46, 35, 11.5, C["faint"]))

    svg.add(svg.text(t.medium, h["headline"], 34, 86, 44, C["ink"], attrs=' opacity="1"',
                     inner=fade_in(0.05, 0.7)))
    svg.add(svg.text(t.regular, h["subline"], 35, 121, 18.5, C["muted"], attrs=' opacity="1"',
                     inner=fade_in(0.3, 0.7)))

    # ------------------------------------------------------------------ racks and rail
    for x, ys, start in ((LX, left_y, 0.35), (RX, right_y, 1.5)):
        top, bottom = ys[0] - 22, ys[-1] + 22
        svg.add(
            f'<g opacity="1">{fade_in(start, 0.5)}'
            f'<rect x="{f(x - 12)}" y="{f(top)}" width="24" height="{f(bottom - top)}" rx="4" '
            f'fill="{C["panel"]}" stroke="{C["steel_hi"]}" stroke-width="1.25"/>'
            f'{rivet(x, top + 7)}{rivet(x, bottom - 7)}</g>'
        )
    rail_top, rail_bottom = right_y[0] - 30, right_y[-1] + 30
    ticks = "".join(
        f"M{f(RAIL - 5)} {f(y)}H{f(RAIL - 1)}"
        for y in [rail_top + 6 + n * spacing / 2 for n in range(int((rail_bottom - rail_top - 12) // (spacing / 2)) + 1)]
    )
    svg.add(
        f'<g opacity="1">{fade_in(1.7, 0.5)}'
        f'<path d="{ticks}" stroke="{C["steel_hi"]}" stroke-width="1"/>'
        f'<path d="M{f(RAIL)} {f(rail_top)}V{f(rail_bottom)}" stroke="{C["steel_hi"]}" stroke-width="2.5"/>'
        f'<rect x="{f(RAIL - 4)}" y="{f(rail_top - 3)}" width="8" height="4" rx="1" fill="{C["rivet"]}"/>'
        f'<rect x="{f(RAIL - 4)}" y="{f(rail_bottom - 1)}" width="8" height="4" rx="1" fill="{C["rivet"]}"/></g>'
    )

    # ------------------------------------------------------------------ cables
    svg.add('<g clip-path="url(#revealL)">')
    for i in range(len(clients)):
        svg.add(cable(left_d(i), C["core_neutral"]))
        svg.add(f'<g transform="translate({f(left_start)} {f(left_y[i])}) rotate(180)">{plug(C["core_neutral"])}</g>')
    svg.add("</g>", '<g clip-path="url(#revealR)">')
    by_j = {e["j"]: e for e in events}
    for j, system in enumerate(systems):
        colour = wires[system["wire"]]
        conn = right_d(j)
        e = by_j.get(j)
        if not e:
            svg.add(cable(conn, colour))
            svg.add(f'<g transform="translate({f(right_end)} {f(right_y[j])})">{plug(colour)}</g>')
            continue
        te, ta = e["te"], e["arrive"]
        (low_end, low_c2), (end, c2) = droop(j, 5), droop(j)
        d_anim = tl.anim("d", [
            (0, conn), (te, conn), (te + 0.3, right_d(j, low_end, low_c2), "in"),
            (te + 0.45, right_d(j, end, c2), "out"), (ta + 0.6, right_d(j, end, c2)),
            (ta + 1.2, conn, "io"), (M, conn),
        ])
        svg.add(cable(conn, colour, d_anim))
        conn_t = f"{f(right_end)} {f(right_y[j])}"
        move = tl.anim("transform", [
            (0, conn_t), (te, conn_t), (te + 0.3, f"{f(low_end[0])} {f(low_end[1])}", "in"),
            (te + 0.45, f"{f(end[0])} {f(end[1])}", "out"), (ta + 0.6, f"{f(end[0])} {f(end[1])}"),
            (ta + 1.2, conn_t, "io"), (M, conn_t),
        ], tag="animateTransform", extra=' type="translate"')
        turn = tl.anim("transform", [
            (0, "0"), (te, "0"), (te + 0.3, "80", "in"), (te + 0.45, "90", "out"),
            (ta + 0.6, "90"), (ta + 1.2, "0", "io"), (M, "0"),
        ], tag="animateTransform", extra=' type="rotate" additive="sum"')
        svg.add(f'<g transform="translate({conn_t})">{move}{turn}{plug(colour)}</g>')
    svg.add("</g>")

    # ------------------------------------------------------------------ signal pulses
    streak = 26.0
    flashes: dict[int, list[tuple[float, str]]] = {}
    left_flashes: dict[int, list[float]] = {}

    def pulse(d: str, length: float, colour: str, begin: float, travel: float) -> str:
        rest = streak + 6
        gap = length + streak + 40
        values = f"{f(rest)};{f(-(length + 6))};{f(-(length + 6))}"
        anim = (
            f'<animate attributeName="stroke-dashoffset" values="{values}" keyTimes="0;{travel / cycle:.4f};1" '
            f'dur="{cycle:.2f}s" begin="{begin:.2f}s" repeatCount="indefinite"/>'
        )
        core = mix(colour, "#ffffff", CORE_MIX)
        return (
            f'<path d="{d}" fill="none" stroke="{colour}" stroke-opacity="{GLOW}" stroke-width="9" '
            f'stroke-linecap="round" stroke-dasharray="{f(streak)} {f(gap)}" stroke-dashoffset="{f(rest)}">'
            f"{anim}</path>"
            f'<path d="{d}" fill="none" stroke="{core}" stroke-width="2.4" stroke-linecap="round" '
            f'stroke-dasharray="{f(streak)} {f(gap)}" stroke-dashoffset="{f(rest)}">{anim}</path>'
        )

    for k, (i, j) in enumerate(routes):
        start = first + k * stagger
        colour = wires[systems[j]["wire"]]
        svg.add(pulse(left_d(i), t_left[i] * speed, wires["graphite"], start, t_left[i]))
        svg.add(pulse(right_d(j), t_right[j] * speed, colour, start + t_left[i] + t_sleeve, t_right[j]))
        flashes.setdefault(j, []).append((start + t_left[i] + t_sleeve + t_right[j] - 0.05, colour))
        left_flashes.setdefault(i, []).append(start)

    # ------------------------------------------------------------------ sockets
    def socket(x: float, y: float, stroke: str, flash_list, start: float) -> str:
        anims = ""
        for begin, colour in flash_list:
            anims += (
                f'<animate attributeName="fill" values="{C["void"]};{colour};{C["void"]};{C["void"]}" '
                f'keyTimes="0;{0.06 / cycle:.4f};{0.9 / cycle:.4f};1" dur="{cycle:.2f}s" begin="{begin:.2f}s" '
                f'repeatCount="indefinite"/>'
            )
        return (
            f'<circle cx="{f(x)}" cy="{f(y)}" r="5" fill="{C["void"]}" stroke="{stroke}" '
            f'stroke-width="1.8" opacity="1">{fade_in(start, 0.4)}{anims}</circle>'
        )

    for i, y in enumerate(left_y):
        svg.add(socket(LX, y, C["core_neutral"], [(b, C["muted"]) for b in left_flashes.get(i, [])], 0.35 + i * 0.05))
    for j, y in enumerate(right_y):
        svg.add(socket(RX, y, wires[systems[j]["wire"]], flashes.get(j, []), 1.5 + j * 0.06))
    for e in events:
        y = right_y[e["j"]]
        fixed = e["arrive"] + 1.25
        blinks = []
        tt = e["te"] + 0.05
        while tt + 0.18 < fixed:
            blinks.append((tt, tt + 0.16))
            tt += 0.32
        svg.add(
            f'<circle cx="{f(RX)}" cy="{f(y)}" r="5.6" fill="{C["void"]}" opacity="0">'
            f'{tl.window("opacity", 0, 1, [(e["te"], fixed)])}</circle>'
            f'<circle cx="{f(RX)}" cy="{f(y)}" r="3.4" fill="{C["amber"]}" opacity="0">'
            f'{tl.window("opacity", 0, 1, blinks, ramp=0.01)}</circle>'
        )

    # ------------------------------------------------------------------ hub
    hub_top = SY - hub_h / 2
    svg.add(f'<g opacity="1">{fade_in(1.0, 0.5)}')
    for x in (SX0 - 8, SX1 - 2):
        svg.add(
            f'<rect x="{f(x)}" y="{f(hub_top - 5)}" width="10" height="{f(hub_h + 10)}" rx="2" '
            f'fill="{C["steel"]}" stroke="{C["steel_hi"]}" stroke-width="1.2"/>'
            f'<rect x="{f(x + 2.5)}" y="{f(hub_top - 2)}" width="5" height="{f(hub_h + 4)}" fill="url(#hazard)" '
            f'opacity="0.85"/>'
        )
    svg.add(
        f'<rect x="{f(SX0)}" y="{f(hub_top)}" width="{f(sleeve_w)}" height="{f(hub_h)}" rx="6" '
        f'fill="{C["panel_hi"]}" stroke="{C["steel_hi"]}" stroke-width="1.3"/>'
        f'<path d="M{f(SX0 + 6)} {f(hub_top + 4.5)}H{f(SX1 - 6)}" stroke="{C["steel_hi"]}" stroke-opacity="0.5"/>'
    )
    x = SX0
    for b, (label, size) in enumerate(zip(h["sleeve"], band_sizes)):
        if b:
            svg.add(
                f'<path d="M{f(x)} {f(hub_top + 13)}V{f(hub_top + hub_h - 13)}" stroke="{C["steel_hi"]}" '
                f'stroke-width="1.2"/>{rivet(x, hub_top + 7, 1.6)}{rivet(x, hub_top + hub_h - 7, 1.6)}'
            )
        svg.add(svg.text(t.medium, label, x + size / 2, SY + 5.5, 15.5, C["ink"], anchor="middle"))
        x += size
    svg.add("</g>")

    # ------------------------------------------------------------------ labels
    for i, (label, y) in enumerate(zip(clients, left_y)):
        svg.add(svg.text(t.medium, label, LX - 24, y + 5.3, label_size, C["ink"], anchor="end",
                         attrs=' opacity="1"', inner=fade_in(0.35 + i * 0.05, 0.5)))
    for j, (system, y) in enumerate(zip(systems, right_y)):
        svg.add(svg.text(t.medium, system["label"], RX + label_gap, y + 5.3, label_size, C["ink"],
                         attrs=' opacity="1"', inner=fade_in(1.5 + j * 0.06, 0.5)))
    caption_y = H - 22
    svg.add(svg.text(t.cond, h["clients_caption"].upper(), LX, caption_y, 13, C["hud"], anchor="middle",
                     tracking=0.14, attrs=' opacity="1"', inner=fade_in(0.6, 0.5)))
    svg.add(svg.text(t.cond, h["systems_caption"].upper(), RX, caption_y, 13, C["hud"], anchor="middle",
                     tracking=0.14, attrs=' opacity="1"', inner=fade_in(1.9, 0.5)))

    # ------------------------------------------------------------------ service droid
    # Drawn in its own frame: origin on the rail at the parking height, scaled up as a group.
    by = y_park
    s = bot_scale
    bx0, bx1 = 4.0, 30.0
    top, bottom = -17.0, 17.0
    S = (7.0, 3.0)

    def local(x: float, y: float) -> tuple[float, float]:
        return (x - RAIL) / s, (y - by) / s

    def arm_pose(hx: float, hy: float):
        ex = (S[0] + hx) / 2 + 3
        ey = min(S[1], hy) - 13
        return f"M{f(S[0])} {f(S[1])}L{f(ex)} {f(ey)}L{f(hx)} {f(hy)}", (hx, hy)

    rest_d, rest_h = f"M{f(S[0])} {f(S[1])}L{f(S[0] + 7)} {f(S[1] + 7)}L{f(S[0] + 14)} {f(S[1] + 2)}", (S[0] + 14, S[1] + 2)
    reach_d, reach_h = arm_pose(*local(droop(0)[0][0], by + 28))
    seat_d, seat_h = arm_pose(*local(right_end, by))
    RAIL_ABS = RAIL
    RAIL = 0.0  # wheels sit on the rail at the local origin

    move_frames = [(0, "0 0")]
    wheel_frames = [(0, 0.0)]
    arm_frames = [(0, rest_d)]
    hand_x = [(0, f(rest_h[0]))]
    hand_y = [(0, f(rest_h[1]))]
    visor = [(0, C["hud"])]
    angle = 0.0
    for e in events:
        ta = e["arrive"]
        move_frames += [(e["depart"], f"0 {f(e['from'])}"), (ta, f"0 {f(e['to'])}", "io")]
        wheel_frames += [(e["depart"], angle)]
        angle += e["dist"] / 3.6 * 180 / math.pi
        wheel_frames += [(ta, angle, "io")]
        arm_frames += [(ta, rest_d), (ta + 0.45, reach_d, "io"), (ta + 0.6, reach_d),
                       (ta + 1.2, seat_d, "io"), (ta + 1.6, seat_d), (ta + 1.95, rest_d, "io")]
        for frames, pick in ((hand_x, 0), (hand_y, 1)):
            frames += [(ta, f(rest_h[pick])), (ta + 0.45, f(reach_h[pick]), "io"), (ta + 0.6, f(reach_h[pick])),
                       (ta + 1.2, f(seat_h[pick]), "io"), (ta + 1.6, f(seat_h[pick])),
                       (ta + 1.95, f(rest_h[pick]), "io")]
        visor += [(e["te"] + 0.35, C["hud"]), (e["te"] + 0.5, C["amber"]), (e["done"], C["amber"]),
                  (e["done"] + 0.15, C["hud"])]
    move_frames.append((M, "0 0"))
    wheel_frames.append((M, angle))
    arm_frames.append((M, rest_d))
    hand_x.append((M, f(rest_h[0])))
    hand_y.append((M, f(rest_h[1])))
    visor.append((M, C["hud"]))

    def wheel(cy: float) -> str:
        frames = [(fr[0], f"{f(fr[1] / s)} {f(RAIL)} {f(cy)}", *fr[2:]) for fr in wheel_frames]
        spin = tl.anim("transform", frames, tag="animateTransform", extra=' type="rotate"')
        return (
            f'<path d="M{f(RAIL)} {f(cy)}H{f(bx0 + 1)}" stroke="{C["steel_hi"]}" stroke-width="2.4"/>'
            f'<circle cx="{f(RAIL)}" cy="{f(cy)}" r="3.8" fill="{C["void"]}" stroke="{C["muted"]}" stroke-width="1.4"/>'
            f'<path d="M{f(RAIL - 2.3)} {f(cy)}H{f(RAIL + 2.3)}M{f(RAIL)} {f(cy - 2.3)}V{f(cy + 2.3)}" '
            f'stroke="{C["muted"]}" stroke-width="1">{spin}</path>'
        )

    svg.add(
        f'<g opacity="1">{fade_in(1.9, 0.5)}<g>'
        + tl.anim("transform", move_frames, tag="animateTransform", extra=' type="translate"')
        + f'<g transform="translate({f(RAIL_ABS)} {f(by)}) scale({s})">'
        + wheel(-10) + wheel(10)
        # armoured chassis with chamfered shoulders
        + f'<polygon points="{f(bx0)},{f(top + 6)} {f(bx0 + 6)},{f(top)} {f(bx1 - 6)},{f(top)} {f(bx1)},{f(top + 6)} '
        f'{f(bx1)},{f(bottom)} {f(bx0)},{f(bottom)}" fill="{C["panel_hi"]}" stroke="{C["steel_hi"]}" stroke-width="1.2"/>'
        + f'<path d="M{f(bx0 + 3)} {f(top + 3)}L{f(bx0 + 6.5)} {f(top + 0.8)}H{f(bx1 - 6.5)}L{f(bx1 - 3)} {f(top + 3)}" '
        f'fill="none" stroke="{C["rivet"]}" stroke-width="0.7"/>'
        + f'<path d="M{f(bx0 + 1)} 5H{f(bx1 - 1)}" stroke="{C["steel_hi"]}" stroke-width="0.9"/>'
        + f'<rect x="{f(bx0 + 1.2)}" y="{f(bottom - 6)}" width="{f(bx1 - bx0 - 2.4)}" height="4.8" fill="url(#hazard)"/>'
        # sensor slit
        + f'<rect x="{f(bx0 + 5)}" y="{f(top + 6)}" width="16" height="6" rx="1.2" fill="{C["void"]}" '
        f'stroke="{C["steel_hi"]}" stroke-width="0.8"/>'
        + f'<rect x="{f(bx0 + 6.5)}" y="{f(top + 7.6)}" width="13" height="2.8" rx="1" fill="{C["hud"]}">'
        + tl.anim("fill", visor) + "</rect>"
        + f'<path d="M{f(bx1 - 8)} 8.5H{f(bx1 - 3)}M{f(bx1 - 8)} 11H{f(bx1 - 3)}" '
        f'stroke="{C["steel_hi"]}" stroke-width="0.9"/>'
        + rivet(bx0 + 4, 9, 1.1) + rivet(bx1 - 4, top + 15, 1.1)
        # beacon
        + f'<rect x="{f(bx1 - 11)}" y="{f(top - 5)}" width="7" height="5" rx="1" fill="{C["steel"]}" '
        f'stroke="{C["steel_hi"]}" stroke-width="0.9"/>'
        + f'<circle cx="{f(bx1 - 7.5)}" cy="{f(top - 5.5)}" r="2.2" fill="{C["amber"]}">{blink(1.1, 0.2)}</circle>'
        # welding arm
        + f'<path d="{rest_d}" fill="none" stroke="{C["muted"]}" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round">{tl.anim("d", arm_frames)}</path>'
        + f'<circle cx="{f(S[0])}" cy="{f(S[1])}" r="2.1" fill="{C["steel"]}" stroke="{C["muted"]}" stroke-width="0.9"/>'
        + f'<circle cx="{f(rest_h[0])}" cy="{f(rest_h[1])}" r="2.2" fill="{C["amber"]}">'
        + tl.anim("cx", hand_x) + tl.anim("cy", hand_y) + "</circle>"
        + "</g></g></g>"
    )
    RAIL = RAIL_ABS

    # ------------------------------------------------------------------ sparks and welding flashes
    for e in events:
        y = right_y[e["j"]]
        ta = e["arrive"]
        px, py = RX - 5, y
        rays = "".join(
            f'M{f(math.cos(math.radians(a)) * 3)} {f(math.sin(math.radians(a)) * 3)}'
            f'L{f(math.cos(math.radians(a)) * ln)} {f(math.sin(math.radians(a)) * ln)}'
            for a, ln in ((150, 10), (185, 12), (215, 9), (120, 8), (245, 8), (170, 7), (200, 6))
        )
        opacity = tl.anim("opacity", [
            (0, 0), (e["te"], 0), (e["te"] + 0.02, 1), (e["te"] + 0.32, 0, "out"),
            (ta + 1.2, 0), (ta + 1.22, 1), (ta + 1.3, 0.4), (ta + 1.36, 1), (ta + 1.62, 0, "out"), (M, 0),
        ])
        burst = tl.anim("transform", [
            (0, "0.4"), (e["te"], "0.4"), (e["te"] + 0.32, "1.5", "out"), (e["te"] + 0.34, "0.4"),
            (ta + 1.2, "0.4"), (ta + 1.62, "1.4", "out"), (ta + 1.64, "0.4"), (M, "0.4"),
        ], tag="animateTransform", extra=' type="scale"')
        glow = tl.anim("opacity", [
            (0, 0), (ta + 1.2, 0), (ta + 1.23, 0.9), (ta + 1.3, 0.3), (ta + 1.37, 0.9),
            (ta + 1.46, 0.45), (ta + 1.62, 0, "out"), (M, 0),
        ])
        svg.add(
            f'<g transform="translate({f(px)} {f(py)})">'
            f'<circle r="7" fill="{mix(C["amber"], "#ffffff", 0.45)}" opacity="0">{glow}</circle>'
            f'<g opacity="0">{opacity}<g>{burst}<path d="{rays}" stroke="{mix(C["amber"], "#ffffff", 0.3)}" '
            f'stroke-width="1.4" stroke-linecap="round"/></g></g></g>'
        )

    svg.add(f'<polygon points="{frame}" fill="none" stroke="{C["steel_hi"]}" stroke-width="1.5"/>')
    return svg.render()


# --------------------------------------------------------------------------- briefing


def briefing(cfg: dict, t: Type) -> str:
    b = cfg["briefing"]
    W = 900
    para_size, para_lead = 17, 26
    lines = wrap(t.regular, b["text"], para_size, 470)
    principle_size, principle_lead = 16, 30
    top = 66
    left_end = top + (len(lines) - 1) * para_lead
    right_end = top + (len(b["principles"]) - 1) * principle_lead
    row = max(left_end, right_end) + 48
    H = int(row + 34)
    svg = Svg(W, H, "Briefing",
              f'{b["text"]} Operating principles: {"; ".join(b["principles"])}. Loadout: {", ".join(b["loadout"])}.')
    svg.defs.append(hazard_pattern())
    pts = plate_points(1, 1, W - 1, H - 1, 16)
    svg.add(f'<polygon points="{pts}" fill="{C["panel"]}" stroke="{C["steel_hi"]}" stroke-width="1.5"/>')
    svg.add(f'<rect x="{W - 120}" y="7" width="86" height="4" fill="url(#hazard)" opacity="0.9"/>')
    svg.add(rivet(W - 12, 12), rivet(12, H - 12), rivet(W - 12, H - 24))

    svg.add(svg.text(t.cond, "BRIEFING", 30, 38, 14, C["hud"], tracking=0.16))
    for n, line in enumerate(lines):
        svg.add(svg.text(t.regular, line, 30, top + n * para_lead, para_size, C["ink"]))

    svg.add(f'<path d="M532 26V{f(row - 26)}" stroke="{C["steel"]}" stroke-width="1.2"/>')
    svg.add(svg.text(t.cond, "OPERATING PRINCIPLES", 560, 38, 14, C["hud"], tracking=0.16))
    for n, item in enumerate(b["principles"]):
        y = top + n * principle_lead
        svg.add(f'<path d="M560 {f(y - 10)}l6 5-6 5" fill="none" stroke="{C["amber"]}" stroke-width="2" '
                f'stroke-linecap="round" stroke-linejoin="round"/>')
        svg.add(svg.text(t.regular, item, 576, y, principle_size, C["ink"]))

    svg.add(f'<path d="M30 {f(row - 22)}H{W - 30}" stroke="{C["steel"]}" stroke-width="1.2"/>')
    svg.add(svg.text(t.cond, "LOADOUT", 30, row + 5, 14, C["hud"], tracking=0.16))
    x = 112.0
    for item in b["loadout"]:
        w = t.mono.width(item, 13) + 20
        svg.add(
            f'<rect x="{f(x)}" y="{f(row - 12)}" width="{f(w)}" height="24" rx="3" fill="{C["panel_hi"]}" '
            f'stroke="{C["steel_hi"]}" stroke-width="1"/>'
        )
        svg.add(svg.text(t.mono, item, x + 10, row + 4.5, 13, C["muted"]))
        x += w + 8
    return svg.render()


# --------------------------------------------------------------------------- section header


def section(label: str, t: Type) -> str:
    W, H = 900, 44
    svg = Svg(W, H, label, label)
    svg.add(svg.text(t.cond, label.upper(), 4, 28, 16, C["hud"], tracking=0.16))
    x0 = 4 + t.cond.width(label.upper(), 16, 0.16) + 16
    ticks = "".join(f"M{f(x)} 19V27" for x in range(int(x0) + 40, W - 40, 60))
    svg.add(
        f'<path d="M{f(x0)} 23H{W - 24}" stroke="{C["steel_hi"]}" stroke-width="1.2"/>'
        f'<path d="{ticks}" stroke="{C["steel_hi"]}" stroke-width="1"/>'
        f'<circle cx="{W - 12}" cy="23" r="3.2" fill="{C["hud"]}">{blink(2.2, 0.3)}</circle>'
    )
    return svg.render()


# --------------------------------------------------------------------------- project cards


def compact(n: int) -> str:
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def project_card(item: dict, stats: dict, t: Type, index: int) -> str:
    colour = C["wires"][item["wire"]]
    W, H = 440, 168
    x0, y0, x1, y1, cut = 30, 4, 438, 164, 12
    language = item.get("language") or stats.get("language") or ""
    stars, forks = stats.get("stars", 0), stats.get("forks", 0)
    svg = Svg(W, H, item["repo"], f'{item["blurb"]} {language}, {stars} stars, {forks} forks.')
    svg.defs.append(hazard_pattern())

    svg.add(f'<polygon points="{plate_points(x0, y0, x1, y1, cut)}" fill="{C["panel"]}" '
            f'stroke="{C["steel_hi"]}" stroke-width="1.3"/>')
    svg.add(f'<path d="M{x0 + cut + 6} {y0 + 5}H{x1 - 6}" stroke="{C["steel"]}" stroke-width="1"/>')
    svg.add(f'<rect x="{x1 - 92}" y="{y1 - 8}" width="62" height="4" fill="url(#hazard)" opacity="0.85"/>')
    svg.add(rivet(x1 - 8, y0 + 13), rivet(x0 + 8, y1 - 8))

    # The project's own conduit plugs into the plate, in the same colour as its system in the hero.
    wire_y = 50
    svg.add(cable(f"M-4 {wire_y}H{x0 - 17}", colour))
    svg.add(f'<g transform="translate({x0 - 17} {wire_y})">{plug(colour)}</g>')
    svg.add(f'<circle cx="{x0}" cy="{wire_y}" r="5" fill="{C["void"]}" stroke="{colour}" stroke-width="1.8"/>')
    svg.add(f'<circle cx="{x1 - 16}" cy="{y0 + 30}" r="3" fill="{C["led"]}">{blink(2.4, 0.25, index * 0.37)}</circle>')

    name_size = 17.0
    max_name = x1 - 34 - (x0 + 20)
    while t.mono.width(item["repo"], name_size) > max_name and name_size > 12.5:
        name_size -= 0.5
    svg.add(svg.text(t.mono, item["repo"], x0 + 20, 56, name_size, C["ink"]))
    for n, line in enumerate(wrap(t.regular, item["blurb"], 15.5, x1 - 22 - (x0 + 20))[:2]):
        svg.add(svg.text(t.regular, line, x0 + 20, 85 + n * 21, 15.5, C["muted"]))

    x = x0 + 20.0
    meta_y = 141
    if language:
        label = language.upper()
        w = t.cond.width(label, 13, 0.1) + 14
        svg.add(f'<rect x="{f(x)}" y="{meta_y - 14}" width="{f(w)}" height="19" rx="2" fill="none" '
                f'stroke="{C["amber"]}" stroke-opacity="0.8" stroke-width="1"/>')
        svg.add(svg.text(t.cond, label, x + 7, meta_y, 13, C["amber"], tracking=0.1))
        x += w + 18
    svg.add(star_icon(x + 6.4, meta_y - 4.5, C["muted"]))
    svg.add(svg.text(t.mono, compact(stars), x + 18, meta_y + 0.5, 14, C["ink"]))
    x += 18 + t.mono.width(compact(stars), 14) + 20
    svg.add(fork_icon(x + 4, meta_y - 4.5, C["muted"]))
    svg.add(svg.text(t.mono, compact(forks), x + 14, meta_y + 0.5, 14, C["ink"]))
    return svg.render()


# --------------------------------------------------------------------------- status bar


def status(data: dict, t: Type) -> str:
    W, H = 900, 78
    tot = data["totals"]
    synced = dt.datetime.strptime(data["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")
    cells = [
        ("STARS", f'{tot["stars"]:,}'),
        ("FORKS", f'{tot["forks"]:,}'),
        ("REPOSITORIES", str(tot["originals"])),
        ("FOLLOWERS", str(data["user"]["followers"])),
        ("LAST SYNC", synced.strftime("%Y-%m-%d %H:%M UTC")),
    ]
    svg = Svg(W, H, "Profile stats",
              f'{tot["stars"]} stars and {tot["forks"]} forks across {tot["originals"]} original repositories, '
              f'{data["user"]["followers"]} followers. Last sync {cells[-1][1]}. Refreshed daily.')
    svg.defs.append(hazard_pattern())
    svg.add(f'<polygon points="{plate_points(1, 1, W - 1, H - 1, 14)}" fill="{C["panel"]}" '
            f'stroke="{C["steel_hi"]}" stroke-width="1.5"/>')
    widths = [120, 120, 150, 130]
    x = 30.0
    for n, (label, value) in enumerate(cells):
        if n:
            svg.add(f'<path d="M{f(x - 22)} 18V{H - 18}" stroke="{C["steel"]}" stroke-width="1.2"/>')
        svg.add(svg.text(t.cond, label, x, 31, 12.5, C["faint"], tracking=0.16))
        if n < len(cells) - 1:
            svg.add(svg.text(t.mono, value, x, 58, 22, C["ink"]))
            x += widths[n] + 22
        else:
            svg.add(f'<circle cx="{f(x + 4)}" cy="53" r="3" fill="{C["led"]}">{blink(1.8)}</circle>')
            svg.add(svg.text(t.mono, value, x + 15, 58, 15, C["ink"]))
    svg.add(f'<rect x="{W - 112}" y="{H - 10}" width="74" height="4" fill="url(#hazard)" opacity="0.85"/>')
    return svg.render()


# --------------------------------------------------------------------------- data


def github(path: str, token: str | None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "salacoste-profile-builder"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_live(user: str, token: str | None) -> dict:
    repos = []
    page = 1
    while True:
        batch = github(f"/users/{user}/repos?per_page=100&type=owner&page={page}", token)
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    profile = github(f"/users/{user}", token)
    originals = [r for r in repos if not r["fork"] and not r.get("archived")]
    return {
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user": {"followers": profile["followers"], "public_repos": profile["public_repos"]},
        "totals": {
            "stars": sum(r["stargazers_count"] for r in originals),
            "forks": sum(r["forks_count"] for r in originals),
            "originals": len(originals),
        },
        "repos": {
            r["name"]: {"stars": r["stargazers_count"], "forks": r["forks_count"], "language": r["language"]}
            for r in repos
        },
    }


def load_data(cfg: dict, offline: bool) -> dict:
    cached = json.loads(CACHE.read_text()) if CACHE.exists() else None
    if offline:
        if not cached:
            sys.exit("data/github.json is missing; run once without --offline")
        return cached
    try:
        data = fetch_live(cfg["user"], os.environ.get("GITHUB_TOKEN"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, ValueError) as exc:
        if not cached:
            raise
        print(f"warning: live fetch failed ({exc}); using data/github.json", file=sys.stderr)
        return cached
    wanted = {item["repo"] for item in cfg["featured"]}
    data["repos"] = {name: stats for name, stats in sorted(data["repos"].items()) if name in wanted}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return data


# --------------------------------------------------------------------------- readme


def readme(cfg: dict, data: dict) -> str:
    h, b = cfg["hero"], cfg["briefing"]
    tot = data["totals"]
    hero_alt = (f'{h["headline"]} {h["subline"]} {", ".join(h["clients"])} connect through '
                f'{", ".join(h["sleeve"])} to {", ".join(s["label"] for s in h["systems"])}; '
                f"a service droid plugs loose cables back in.")
    brief_alt = f'{b["text"]} Operating principles: {"; ".join(b["principles"])}.'
    cards = []
    for item in cfg["featured"]:
        repo = item["repo"]
        stars = data["repos"].get(repo, {}).get("stars", 0)
        cards.append(
            f'<a href="https://github.com/{cfg["user"]}/{repo}"><img alt="{escape(f"{repo}: {item["blurb"]} {stars} stars.")}" '
            f'src="./assets/cards/{repo}.svg" width="49%"></a>'
        )
    pairs = "\n".join(" ".join(cards[i : i + 2]) for i in range(0, len(cards), 2))
    status_alt = (f'{tot["stars"]} stars and {tot["forks"]} forks across {tot["originals"]} original '
                  f'repositories, {data["user"]["followers"]} followers. Refreshed daily.')
    return (
        "<!-- Generated by scripts/build.py from profile.json. Edit those files, not this one. -->\n\n"
        f'<img alt="{escape(hero_alt)}" src="./assets/hero.svg" width="100%">\n\n'
        f'<img alt="{escape(brief_alt)}" src="./assets/briefing.svg" width="100%">\n\n'
        f'<img alt="Selected work" src="./assets/section-work.svg" width="100%">\n\n'
        f"<p>\n{pairs}\n</p>\n\n"
        f'<img alt="{escape(status_alt)}" src="./assets/status.svg" width="100%">\n'
    )


# --------------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="render from data/github.json only")
    args = parser.parse_args()

    cfg = json.loads(CONFIG.read_text())
    data = load_data(cfg, args.offline)
    t = Type()

    outputs: dict[Path, str] = {
        ASSETS / "hero.svg": hero(cfg, t),
        ASSETS / "briefing.svg": briefing(cfg, t),
        ASSETS / "section-work.svg": section("Selected work", t),
        ASSETS / "status.svg": status(data, t),
    }
    for n, item in enumerate(cfg["featured"]):
        stats = data["repos"].get(item["repo"], {})
        outputs[CARDS / f'{item["repo"]}.svg'] = project_card(item, stats, t, n)

    CARDS.mkdir(parents=True, exist_ok=True)
    for path, content in outputs.items():
        path.write_text(content)
    # Drop renders that no longer belong to the profile (for example a removed featured repo).
    for stale in list(ASSETS.glob("*.svg")) + list(CARDS.glob("*.svg")):
        if stale not in outputs:
            stale.unlink()

    README.write_text(readme(cfg, data))
    print(f"rendered {len(outputs)} panels; {data['totals']['stars']} stars total")


if __name__ == "__main__":
    main()

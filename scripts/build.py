#!/usr/bin/env python3
"""Build the animated SVGs and live stats for the github.com/salacoste profile README.

Everything is rendered as self-contained SVG: text is converted to glyph outlines
(shaped with HarfBuzz, so kerning survives), motion uses SMIL only, and there is
no CSS, JavaScript or external request inside the images. That is the subset that
survives GitHub's image proxy on every browser.

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
import re
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

# --------------------------------------------------------------------------- theme

THEMES = {
    "light": {
        "ink": "#1f2328",
        "muted": "#59636e",
        "faint": "#818b98",
        "line": "#d1d9e0",
        "surface": "#f6f8fa",
        "pin": "#ffffff",
        "wires": {
            "graphite": "#8c959f",
            "orange": "#d4602a",
            "blue": "#2f6fde",
            "magenta": "#b03d9f",
            "green": "#2a8a56",
            "teal": "#13808b",
        },
        "core_mix": 0.62,
        "glow": 0.26,
    },
    "dark": {
        "ink": "#e6edf3",
        "muted": "#9198a1",
        "faint": "#6e7681",
        "line": "#3d444d",
        "surface": "#151b23",
        "pin": "#0d1117",
        "wires": {
            "graphite": "#656c76",
            "orange": "#f0844d",
            "blue": "#5e95f2",
            "magenta": "#d46ac3",
            "green": "#4cbd7c",
            "teal": "#3ab3bd",
        },
        "core_mix": 0.55,
        "glow": 0.30,
    },
}


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

    def width(self, text: str, size: float) -> float:
        return sum(g[1] for g in self.shape(text)) * size / self.upem

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
    return "0" if s == "-0" else s


class Svg:
    def __init__(self, width: int, height: int, title: str, desc: str):
        self.width = width
        self.height = height
        self.title = title
        self.desc = desc
        self.glyphs: dict[str, str] = {}
        self.parts: list[str] = []

    def add(self, *chunks: str) -> None:
        self.parts.extend(chunks)

    def text(self, font: Font, s: str, x: float, y: float, size: float, fill: str,
             anchor: str = "start", attrs: str = "", inner: str = "") -> str:
        glyphs = font.shape(s)
        k = size / font.upem
        total = sum(g[1] for g in glyphs) * k
        if anchor == "end":
            x -= total
        elif anchor == "middle":
            x -= total / 2
        uses = []
        pen = 0
        for gid, adv, xo, yo in glyphs:
            if font.outline(gid):
                ref = f"{font.key}{gid}"
                self.glyphs.setdefault(ref, font.outline(gid))
                y_attr = f' y="{yo}"' if yo else ""
                uses.append(f'<use xlink:href="#{ref}" x="{pen + xo}"{y_attr}/>')
            pen += adv
        return (
            f'<g transform="translate({f(x)} {f(y)}) scale({k:.5f} {-k:.5f})" '
            f'fill="{fill}"{attrs}>{inner}{"".join(uses)}</g>'
        )

    def render(self) -> str:
        defs = "".join(f'<path id="{gid}" d="{d}"/>' for gid, d in self.glyphs.items())
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 {self.width} {self.height}" width="{self.width}" height="{self.height}" '
            f'role="img" aria-labelledby="title desc">'
            f'<title id="title">{escape(self.title)}</title><desc id="desc">{escape(self.desc)}</desc>'
            f"<defs>{defs}</defs>{''.join(self.parts)}</svg>\n"
        )


def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# --------------------------------------------------------------------------- motion helpers


def fade_in(start: float, duration: float) -> str:
    """Opacity 0 until `start`, then ease to 1 and hold. Static renderers show the final frame."""
    total = start + duration
    return (
        f'<animate attributeName="opacity" values="0;0;1" keyTimes="0;{start / total:.4f};1" '
        f'dur="{total:.2f}s" fill="freeze" calcMode="spline" keySplines="0 0 1 1;0.25 0.1 0.25 1"/>'
    )


def draw_in(length: float, start: float, duration: float) -> str:
    """Stroke reveals from its first point to its last between `start` and `start + duration`."""
    total = start + duration
    return (
        f'<animate attributeName="stroke-dashoffset" values="{f(length)};{f(length)};0" '
        f'keyTimes="0;{start / total:.4f};1" dur="{total:.2f}s" fill="freeze" '
        f'calcMode="spline" keySplines="0 0 1 1;0.3 0.6 0.2 1"/>'
    )


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


def hero(theme_name: str, cfg: dict, t: Type) -> str:
    th = THEMES[theme_name]
    wires = th["wires"]
    h = cfg["hero"]
    W, H = 900, 406
    svg = Svg(
        W,
        H,
        h["headline"],
        f'{h["subline"]} Diagram: {", ".join(h["clients"])} connect through '
        f'{", ".join(h["sleeve"])} to {", ".join(s["label"] for s in h["systems"])}.',
    )

    # Headline and subline: the only text-first moment, fades in before the harness draws.
    svg.add(svg.text(t.medium, h["headline"], 0, 56, 46, th["ink"], attrs=' opacity="1"',
                     inner=fade_in(0.05, 0.7)))
    svg.add(svg.text(t.regular, h["subline"], 1, 93, 19, th["muted"], attrs=' opacity="1"',
                     inner=fade_in(0.3, 0.7)))

    # Geometry of the harness. The sleeve is sized by its labels so every band has equal padding.
    LX, RX = 154, 706          # pin columns
    SY = 258                   # centre line of the bundle
    band_pad = 21
    band_sizes = [t.medium.width(label, 15) + 2 * band_pad for label in h["sleeve"]]
    sleeve_w = sum(band_sizes)
    SX0 = (LX + RX) / 2 - sleeve_w / 2
    SX1 = SX0 + sleeve_w
    lane = 6.0
    spacing = 46
    clients = h["clients"]
    systems = h["systems"]
    left_y = [SY + (i - (len(clients) - 1) / 2) * spacing for i in range(len(clients))]
    right_y = [SY + (j - (len(systems) - 1) / 2) * spacing for j in range(len(systems))]
    left_lane = [SY + (i - (len(clients) - 1) / 2) * lane for i in range(len(clients))]
    right_lane = [SY + (j - (len(systems) - 1) / 2) * lane for j in range(len(systems))]

    left_curves, right_curves = [], []
    left_pull = (SX0 - LX) * 0.58
    right_pull = (RX - SX1) * 0.58
    for i, y in enumerate(left_y):
        left_curves.append(((LX + 5, y), (LX + left_pull, y), (SX0 - left_pull, left_lane[i]), (SX0, left_lane[i])))
    for j, y in enumerate(right_y):
        right_curves.append(((SX1, right_lane[j]), (SX1 + right_pull, right_lane[j]), (RX - right_pull, y), (RX - 5, y)))

    def curve_d(c, lead_in: float = 0, tail: float = 0) -> str:
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = c
        d = ""
        if lead_in:
            d += f"M{f(x0 - lead_in)} {f(y0)}L{f(x0)} {f(y0)}"
        else:
            d += f"M{f(x0)} {f(y0)}"
        d += f"C{f(x1)} {f(y1)} {f(x2)} {f(y2)} {f(x3)} {f(y3)}"
        if tail:
            d += f"L{f(x3 + tail)} {f(y3)}"
        return d

    # Left wires: graphite, drawn from each client into the sleeve.
    left_len = []
    for i, c in enumerate(left_curves):
        length = bezier_length(c) + 16
        left_len.append(length)
        svg.add(
            f'<path d="{curve_d(c, tail=16)}" fill="none" stroke="{wires["graphite"]}" stroke-width="2.25" '
            f'stroke-dasharray="{f(length)} {f(length + 4)}" stroke-dashoffset="0">'
            f"{draw_in(length, 0.55 + i * 0.09, 0.85)}</path>"
        )

    # Right wires: one colour per system, drawn out of the sleeve.
    right_len = []
    for j, c in enumerate(right_curves):
        length = bezier_length(c) + 16
        right_len.append(length)
        colour = wires[systems[j]["wire"]]
        svg.add(
            f'<path d="{curve_d(c, lead_in=16)}" fill="none" stroke="{colour}" stroke-width="2.25" '
            f'stroke-dasharray="{f(length)} {f(length + 4)}" stroke-dashoffset="0">'
            f"{draw_in(length, 1.35 + j * 0.09, 0.85)}</path>"
        )

    # Signal pulses: each route travels client -> sleeve (hidden) -> system, then the pin lights up.
    speed = 300.0
    stagger = 1.7
    routes = h["routes"]
    cycle = stagger * len(routes)
    first = 2.7
    streak = 26.0
    flashes: dict[int, list[tuple[float, str]]] = {}
    left_flashes: dict[int, list[float]] = {}

    def pulse(d: str, length: float, colour: str, begin: float, travel: float) -> str:
        # The streak waits fully before the path start and finishes fully past its end,
        # so its round caps never leave a dot behind while idle.
        rest = streak + 6
        gap = length + streak + 40
        values = f"{f(rest)};{f(-(length + 6))};{f(-(length + 6))}"
        key_times = f"0;{travel / cycle:.4f};1"
        anim = (
            f'<animate attributeName="stroke-dashoffset" values="{values}" keyTimes="{key_times}" '
            f'dur="{cycle:.2f}s" begin="{begin:.2f}s" repeatCount="indefinite"/>'
        )
        core = mix(colour, "#ffffff", th["core_mix"])
        return (
            f'<path d="{d}" fill="none" stroke="{colour}" stroke-opacity="{th["glow"]}" stroke-width="8" '
            f'stroke-linecap="round" stroke-dasharray="{f(streak)} {f(gap)}" stroke-dashoffset="{f(rest)}">'
            f"{anim}</path>"
            f'<path d="{d}" fill="none" stroke="{core}" stroke-width="2.25" stroke-linecap="round" '
            f'stroke-dasharray="{f(streak)} {f(gap)}" stroke-dashoffset="{f(rest)}">{anim}</path>'
        )

    for k, (i, j) in enumerate(routes):
        start = first + k * stagger
        t_left = left_len[i] / speed
        t_sleeve = (SX1 - SX0) / speed
        t_right = right_len[j] / speed
        colour = wires[systems[j]["wire"]]
        svg.add(pulse(curve_d(left_curves[i], tail=16), left_len[i], wires["graphite"], start, t_left))
        svg.add(pulse(curve_d(right_curves[j], lead_in=16), right_len[j], colour,
                      start + t_left + t_sleeve, t_right))
        flashes.setdefault(j, []).append((start + t_left + t_sleeve + t_right - 0.08, colour))
        left_flashes.setdefault(i, []).append(start)

    # Sleeve: the harness itself, with three heat-shrink bands.
    sleeve_h = 38
    svg.add(
        f'<g opacity="1">{fade_in(1.0, 0.5)}'
        f'<rect x="{f(SX0)}" y="{f(SY - sleeve_h / 2)}" width="{f(SX1 - SX0)}" height="{sleeve_h}" '
        f'rx="{f(sleeve_h / 2)}" '
        f'fill="{th["surface"]}" stroke="{th["line"]}" stroke-width="1.25"/>'
    )
    x = SX0
    for b, (label, size) in enumerate(zip(h["sleeve"], band_sizes)):
        if b:
            svg.add(f'<path d="M{f(x)} {f(SY - sleeve_h / 2 + 9)}V{f(SY + sleeve_h / 2 - 9)}" '
                    f'stroke="{th["line"]}" stroke-width="1.25"/>')
        svg.add(svg.text(t.medium, label, x + size / 2, SY + 5.3, 15, th["ink"], anchor="middle"))
        x += size
    svg.add("</g>")

    # Connector housings around each pin column.
    for x, ys, start in ((LX, left_y, 0.35), (RX, right_y, 1.6)):
        svg.add(
            f'<rect x="{x - 12}" y="{f(ys[0] - 20)}" width="24" height="{f(ys[-1] - ys[0] + 40)}" rx="12" '
            f'fill="none" stroke="{th["line"]}" stroke-width="1.25" opacity="1">{fade_in(start, 0.5)}</rect>'
        )

    def pin(x: float, y: float, stroke: str, flash_list: list[tuple[float, str]], start: float) -> str:
        anims = ""
        for begin, colour in flash_list:
            k1 = 0.06 / cycle
            k2 = 0.9 / cycle
            anims += (
                f'<animate attributeName="fill" values="{th["pin"]};{colour};{th["pin"]};{th["pin"]}" '
                f'keyTimes="0;{k1:.4f};{k2:.4f};1" dur="{cycle:.2f}s" begin="{begin:.2f}s" '
                f'repeatCount="indefinite"/>'
            )
        return (
            f'<circle cx="{f(x)}" cy="{f(y)}" r="4.5" fill="{th["pin"]}" stroke="{stroke}" '
            f'stroke-width="1.75" opacity="1">{fade_in(start, 0.4)}{anims}</circle>'
        )

    for i, (label, y) in enumerate(zip(clients, left_y)):
        svg.add(pin(LX, y, th["faint"], [(b, th["muted"]) for b in left_flashes.get(i, [])], 0.35 + i * 0.05))
        svg.add(svg.text(t.medium, label, LX - 24, y + 5.3, 15.5, th["ink"], anchor="end",
                         attrs=' opacity="1"', inner=fade_in(0.35 + i * 0.05, 0.5)))
    for j, (system, y) in enumerate(zip(systems, right_y)):
        colour = wires[system["wire"]]
        svg.add(pin(RX, y, colour, flashes.get(j, []), 1.6 + j * 0.06))
        svg.add(svg.text(t.medium, system["label"], RX + 24, y + 5.3, 15.5, th["ink"],
                         attrs=' opacity="1"', inner=fade_in(1.6 + j * 0.06, 0.5)))

    caption_y = 386
    svg.add(svg.text(t.regular, h["clients_caption"], LX, caption_y, 14, th["faint"], anchor="middle",
                     attrs=' opacity="1"', inner=fade_in(0.6, 0.5)))
    svg.add(svg.text(t.regular, h["systems_caption"], RX, caption_y, 14, th["faint"], anchor="middle",
                     attrs=' opacity="1"', inner=fade_in(1.9, 0.5)))
    return svg.render()


# --------------------------------------------------------------------------- project cards


def compact(n: int) -> str:
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def star_icon(cx: float, cy: float, stroke: str) -> str:
    pts = []
    for n in range(10):
        r = 6.6 if n % 2 == 0 else 2.9
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


def card(theme_name: str, item: dict, stats: dict, t: Type) -> str:
    th = THEMES[theme_name]
    colour = th["wires"][item["wire"]]
    W, H = 440, 160
    language = item.get("language") or stats.get("language") or ""
    stars, forks = stats.get("stars", 0), stats.get("forks", 0)
    svg = Svg(W, H, item["repo"], f'{item["blurb"]} {language}, {stars} stars, {forks} forks.')

    svg.add(
        f'<rect x="14.5" y="5.5" width="{W - 15}" height="{H - 11}" rx="12" '
        f'fill="{th["surface"]}" stroke="{th["line"]}"/>'
    )
    # The project's wire enters from the left edge and terminates in a ring, matching the hero.
    svg.add(
        f'<path d="M0 49H25.5" stroke="{colour}" stroke-width="2.25"/>'
        f'<circle cx="30" cy="49" r="4.5" fill="{th["pin"]}" stroke="{colour}" stroke-width="1.75"/>'
    )

    name_size = 18
    max_name = W - 46 - 18
    while t.mono.width(item["repo"], name_size) > max_name and name_size > 13:
        name_size -= 0.5
    svg.add(svg.text(t.mono, item["repo"], 46, 55, name_size, th["ink"]))

    for n, line in enumerate(wrap(t.regular, item["blurb"], 15.5, W - 46 - 24)[:2]):
        svg.add(svg.text(t.regular, line, 46, 85 + n * 21, 15.5, th["muted"]))

    x = 46
    meta_y = 136
    if language:
        svg.add(svg.text(t.medium, language, x, meta_y, 14, th["muted"]))
        x += t.medium.width(language, 14) + 22
    svg.add(star_icon(x + 6.6, meta_y - 5, th["muted"]))
    svg.add(svg.text(t.medium, compact(stars), x + 18, meta_y, 14, th["muted"]))
    x += 18 + t.medium.width(compact(stars), 14) + 20
    svg.add(fork_icon(x + 4, meta_y - 5, th["muted"]))
    svg.add(svg.text(t.medium, compact(forks), x + 14, meta_y, 14, th["muted"]))
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
    # Keep only what the README needs so the cache diff stays readable.
    wanted = {item["repo"] for item in cfg["featured"]}
    data["repos"] = {name: stats for name, stats in data["repos"].items() if name in wanted}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return data


# --------------------------------------------------------------------------- readme


def replace_block(text: str, name: str, body: str) -> str:
    pattern = re.compile(rf"(<!-- {name}:START -->)(.*?)(<!-- {name}:END -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"README.md is missing the <!-- {name}:START --> / <!-- {name}:END --> markers")
    return pattern.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(3)}", text)


def readme_cards(cfg: dict, data: dict) -> str:
    rows = []
    for item in cfg["featured"]:
        repo = item["repo"]
        stats = data["repos"].get(repo, {})
        alt = escape(f'{repo}: {item["blurb"]} {stats.get("stars", 0)} stars.')
        rows.append(
            f'<a href="https://github.com/{cfg["user"]}/{repo}"><picture>'
            f'<source media="(prefers-color-scheme: dark)" srcset="./assets/cards/{repo}-dark.svg">'
            f'<img alt="{alt}" src="./assets/cards/{repo}-light.svg" width="49%">'
            f"</picture></a>"
        )
    pairs = [" ".join(rows[i : i + 2]) for i in range(0, len(rows), 2)]
    return "<p>\n" + "\n".join(pairs) + "\n</p>"


def readme_stats(data: dict) -> str:
    tot = data["totals"]
    return (
        f'<sub>{tot["stars"]:,} stars and {tot["forks"]:,} forks across {tot["originals"]} original '
        f"repositories. Cards and numbers refresh daily.</sub>"
    )


# --------------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="render from data/github.json only")
    args = parser.parse_args()

    cfg = json.loads(CONFIG.read_text())
    data = load_data(cfg, args.offline)
    t = Type()

    ASSETS.mkdir(exist_ok=True)
    CARDS.mkdir(exist_ok=True)
    for theme in THEMES:
        (ASSETS / f"hero-{theme}.svg").write_text(hero(theme, cfg, t))
        for item in cfg["featured"]:
            stats = data["repos"].get(item["repo"], {})
            (CARDS / f'{item["repo"]}-{theme}.svg').write_text(card(theme, item, stats, t))

    text = README.read_text()
    text = replace_block(text, "CARDS", readme_cards(cfg, data))
    text = replace_block(text, "STATS", readme_stats(data))
    README.write_text(text)
    print(f"rendered hero + {len(cfg['featured'])} cards x {len(THEMES)} themes; "
          f"{data['totals']['stars']} stars total")


if __name__ == "__main__":
    main()

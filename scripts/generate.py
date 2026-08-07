"""Profile OS — generator.

Reads config.yaml, optimizes whatever portrait is dropped into
assets/portrait/, and renders the hero SVG + README.

Design rules (see the vision doc):
  - identity-agnostic: nothing personal is hardcoded here
  - lightweight: images are resized/compressed to the frame's needs
  - the frame is permanent, the content is fluid
"""

from __future__ import annotations

import base64
import html
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

PORTRAIT_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

FONT_UI = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
FONT_MONO = "'SF Mono','Cascadia Code',Consolas,'Liberation Mono',Menlo,monospace"


def load_config(path: str | Path = "config.yaml") -> dict:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def fill(template: str, **tokens) -> str:
    """Substitute {token} placeholders. Deliberately not str.format —
    a stray brace in someone's config must stay literal, never raise."""
    out = str(template)
    for key, value in tokens.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def label(cfg: dict, path: str, default: str) -> str:
    """Read labels.<panel>.<key>, falling back to the built-in wording.
    Every visible string in the OS goes through here, so a fork can be
    renamed end to end without touching this file."""
    node = cfg.get("labels") or {}
    for part in path.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return str(node)


def day_index(n: int) -> int:
    """Pick one of n things, changing daily. The profile is regenerated
    on a schedule, so repeat visitors see it shift."""
    import datetime
    return datetime.date.today().toordinal() % max(n, 1)


# ----------------------------------------------------------------
# Live data: GitHub API, refreshed by the scheduled Action.
# Never breaks the build — degrades to config-only on any failure.
# ----------------------------------------------------------------

def fetch_github(cfg: dict) -> dict | None:
    if not cfg.get("data", {}).get("github_api"):
        return None
    user = cfg["identity"]["github_username"]

    def get(url):
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "profile-os-generator",
        })
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    try:
        u = get(f"https://api.github.com/users/{user}")
        repos = get(f"https://api.github.com/users/{user}"
                    f"/repos?per_page=100&sort=pushed")
        live = {
            "followers": u.get("followers", 0),
            "public_repos": u.get("public_repos", 0),
            "stars": sum(r.get("stargazers_count", 0) for r in repos),
            "repo_stars": {r["name"]: r.get("stargazers_count", 0) for r in repos},
            "repos": [{
                "name": r.get("name", ""),
                "desc": (r.get("description") or "").strip(),
                "language": r.get("language") or "",
                "topics": r.get("topics") or [],
                "stars": r.get("stargazers_count", 0),
                "created": (r.get("created_at") or "")[:4],
                "pushed": r.get("pushed_at") or "",
                "fork": bool(r.get("fork")),
                "archived": bool(r.get("archived")),
            } for r in repos],
        }
        print(f"  live data: {live['public_repos']} repos, "
              f"{live['stars']} stars, {live['followers']} followers")
        return live
    except Exception as e:  # noqa: BLE001
        print(f"  live data: unavailable ({type(e).__name__}) — using config only")
        return None


def auto_projects(cfg: dict, gh: dict | None) -> list:
    """Turn real repositories into explorer entries. Forks are dropped,
    then sorted by stars and, within equal stars, most recently pushed.
    Returns [] when live data is off so the config fallback takes over."""
    import datetime

    # live stats can stay on while the project list stays curated —
    # useful when repositories have no descriptions worth showing
    if not cfg.get("data", {}).get("auto_projects", True):
        return []
    if not gh or not gh.get("repos"):
        return []

    repos = [r for r in gh["repos"] if not r["fork"]]
    repos.sort(key=lambda r: r["pushed"], reverse=True)
    repos.sort(key=lambda r: r["stars"], reverse=True)   # stable: ties stay by date

    today = datetime.date.today()
    fresh_days = int(cfg.get("data", {}).get("active_within_days", 90))
    no_desc = label(cfg, "explorer.no_desc", "no description set on this repository")

    out = []
    for r in repos[:8]:
        try:
            pushed = datetime.date.fromisoformat(r["pushed"][:10])
            dormant = (today - pushed).days > fresh_days
        except ValueError:
            dormant = False
        if r["archived"]:
            status = label(cfg, "explorer.status_archived", "archived")
        elif dormant:
            status = label(cfg, "explorer.status_dormant", "dormant")
        else:
            status = label(cfg, "explorer.status_active", "active")

        stack = r["topics"][:4] or ([r["language"]] if r["language"] else [])
        out.append({
            "name": r["name"],
            "type": r["language"].lower() if r["language"] else "repository",
            "since": r["created"] or "",
            "status": status,
            "desc": r["desc"] or no_desc,
            "stack": stack,
            "stars": r["stars"],
        })
    print(f"  projects: {len(out)} pulled from GitHub (forks excluded)")
    return out


# ----------------------------------------------------------------
# Portrait pipeline: any image in -> optimized, framed image out
# ----------------------------------------------------------------

def find_portrait(cfg: dict) -> Path | None:
    src = ROOT / cfg["portrait"]["source_dir"]
    if not src.is_dir():
        return None
    candidates = [p for p in src.iterdir() if p.suffix.lower() in PORTRAIT_EXTS]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def embed_portrait(path: Path, cfg: dict) -> str:
    """Cover-crop to the frame's aspect, resize to 2x display size
    (retina-sharp), compress, and return a data URI."""
    from PIL import Image

    p = cfg["portrait"]
    tw, th = p["display_width"] * 2, p["display_height"] * 2

    img = Image.open(path)
    # A cutout render (transparent background) would otherwise flatten to
    # pure black and read as a darker block inside its frame. Composite
    # it onto the frame colour so the portrait sits in the interface.
    if img.mode in ("RGBA", "LA", "P") or "transparency" in img.info:
        img = img.convert("RGBA")
        glass = cfg["theme"].get("glass", "#101018").lstrip("#")
        rgb = tuple(int(glass[i:i + 2], 16) for i in (0, 2, 4))
        img = Image.alpha_composite(
            Image.new("RGBA", img.size, rgb + (255,)), img).convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")

    scale = max(tw / img.width, th / img.height)
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    left = (img.width - tw) // 2
    top = (img.height - th) // 2
    img = img.crop((max(left, 0), max(top, 0),
                    min(left + tw, img.width), min(top + th, img.height)))

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=p["quality"], optimize=True, progressive=True)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    print(f"  portrait: {path.name} -> {len(buf.getvalue()) // 1024} KB embedded")
    return f"data:image/jpeg;base64,{data}"


# ----------------------------------------------------------------
# Hero SVG
# ----------------------------------------------------------------

W, H = 1000, 460
PAD_L = 52          # left text margin
BAR_H = 52          # window title bar height


def hero_css(t: dict) -> str:
    return """
    text { font-family: """ + FONT_UI + """; }
    .mono { font-family: """ + FONT_MONO + """; }

    .in    { opacity: 0; animation: rise .6s cubic-bezier(.22,1,.36,1) forwards; }
    .fade  { opacity: 0; animation: fade .8s ease-out forwards; }
    @keyframes rise { from { opacity: 0; transform: translateY(7px); }
                      to   { opacity: 1; transform: translateY(0); } }
    @keyframes fade { to { opacity: 1; } }

    .cursor { animation: blink 1.1s steps(1) infinite; }
    @keyframes blink { 0%,55% { opacity: 1; } 56%,100% { opacity: 0; } }

    .pulse { transform-origin: center; transform-box: fill-box;
             animation: pulse 2.6s ease-in-out infinite; }
    @keyframes pulse { 0%,100% { opacity: .9; } 50% { opacity: .35; } }

    .sheen { animation: sweep 9s ease-in-out infinite; }
    @keyframes sweep { 0%, 55% { transform: translateX(-1100px); }
                       85%, 100% { transform: translateX(1100px); } }

    .frame-in { opacity: 0; transform-origin: center; transform-box: fill-box;
                animation: frame .9s cubic-bezier(.22,1,.36,1) .5s forwards; }
    @keyframes frame { from { opacity: 0; transform: scale(.975) translateY(6px); }
                       to   { opacity: 1; transform: scale(1) translateY(0); } }

    @media (prefers-reduced-motion: reduce) {
      .in, .fade, .frame-in { animation: none; opacity: 1; }
      .sheen { animation: none; opacity: 0; }
    }
    """


def build_boot_lines(cfg: dict, t: dict) -> str:
    lines = cfg["boot"]["lines"]
    if len(lines) > 5:
        print(f"  warning: {len(lines)} boot lines — only 5 fit above the "
              "name block; extra lines will collide. Trimming to 5.")
        lines = lines[:5]
    parts = []
    y = 108
    delay = 0.35
    for line in lines:
        parts.append(
            f'<text class="mono in" style="animation-delay:{delay:.2f}s" '
            f'x="{PAD_L}" y="{y}" font-size="12.5" fill="{t["text_dim"]}">'
            f'<tspan fill="{t["accent"]}">&#9656;</tspan>  {esc(line)}</text>'
        )
        y += 24
        delay += 0.5
    # prompt line with blinking cursor
    prompt = fill(label(cfg, "hero.prompt", "{handle}@github"),
                  handle=cfg["identity"]["handle"])
    symbol = label(cfg, "hero.symbol", "%")
    cursor_x = PAD_L + (len(prompt) + len(symbol) + 4) * 7.5
    parts.append(
        f'<g class="in" style="animation-delay:{delay + 0.15:.2f}s">'
        f'<text class="mono" x="{PAD_L}" y="{y + 6}" font-size="12.5" '
        f'fill="{t["text_faint"]}">{esc(prompt)} '
        f'<tspan fill="{t["accent"]}">~</tspan> {esc(symbol)}</text>'
        f'<rect class="cursor" x="{cursor_x:.0f}" y="{y - 5}" '
        f'width="7.5" height="14" rx="1.5" fill="{t["accent"]}"/></g>'
    )
    return "\n".join(parts), delay + 0.15


def build_identity_block(cfg: dict, t: dict, base_delay: float) -> str:
    ident = cfg["identity"]
    d1, d2, d3 = base_delay + 0.45, base_delay + 0.62, base_delay + 0.80

    # text must never collide with the portrait frame
    avail = W - cfg["portrait"]["display_width"] - 68 - 14 - PAD_L - 26

    name_raw = str(ident["name"])
    name_size = 42
    if 0.56 * name_size * len(name_raw) > avail:
        name_size = max(20, int(avail / (0.56 * len(name_raw))))
    name = esc(name_raw)
    role = esc(str(ident["role"]).upper())
    tagline = esc(ident["tagline"])

    note = str(ident["status_note"])
    max_note = int((avail - 52) / 6.6) - len(str(ident["status"])) - 5
    if len(note) > max_note:
        note = note[: max_note - 1].rstrip() + "…"
    chip_text = esc(f'{ident["status"]}  —  {note}')
    chip_chars = len(f'{ident["status"]}  —  {note}')
    chip_w = round(chip_chars * 6.6) + 52
    chip_y = 366

    return f"""
    <text class="in" style="animation-delay:{d1:.2f}s" x="{PAD_L}" y="286"
          font-size="{name_size}" font-weight="700" letter-spacing="0.5"
          fill="{t['text']}">{name}</text>
    <text class="in" style="animation-delay:{d2:.2f}s" x="{PAD_L}" y="316"
          font-size="16" font-weight="500" letter-spacing="2.5"
          fill="{t['text_dim']}">{role}</text>
    <text class="in mono" style="animation-delay:{d2 + 0.1:.2f}s" x="{PAD_L}" y="340"
          font-size="12.5" font-style="italic"
          fill="{t['text_faint']}">// {tagline}</text>

    <g class="in" style="animation-delay:{d3:.2f}s">
      <rect x="{PAD_L}" y="{chip_y}" width="{chip_w}" height="30" rx="15"
            fill="{t['glass']}" stroke="{t['line']}" stroke-opacity="0.09"/>
      <circle class="pulse" cx="{PAD_L + 17}" cy="{chip_y + 15}" r="3.6" fill="{t['ok']}"/>
      <circle cx="{PAD_L + 17}" cy="{chip_y + 15}" r="7.5" fill="{t['ok']}" opacity="0.14"/>
      <text class="mono" x="{PAD_L + 31}" y="{chip_y + 19.5}" font-size="11"
            letter-spacing="1" fill="{t['text_dim']}">{chip_text}</text>
    </g>"""


def build_portrait_frame(cfg: dict, t: dict, data_uri: str | None) -> str:
    p = cfg["portrait"]
    fw, fh = p["display_width"], p["display_height"]
    fx, fy = W - fw - 68, (H - fh - 26) // 2 + 8
    label = esc(p["label"])

    if data_uri:
        content = (
            f'<image href="{data_uri}" x="{fx}" y="{fy}" width="{fw}" height="{fh}" '
            f'preserveAspectRatio="xMidYMid slice" clip-path="url(#portraitClip)"/>'
            # cool top-light + warm base wash so ANY image sits in the theme
            f'<rect x="{fx}" y="{fy}" width="{fw}" height="{fh}" rx="18" '
            f'fill="url(#portraitWash)" opacity="0.5"/>'
        )
    else:
        monogram = esc(p["label"][:1].upper() or cfg["identity"]["name"][:1].upper())
        content = f"""
        <rect x="{fx}" y="{fy}" width="{fw}" height="{fh}" rx="18" fill="url(#placeholderBg)"/>
        <circle cx="{fx + fw / 2}" cy="{fy + fh / 2 - 12}" r="64" fill="none"
                stroke="{t['accent']}" stroke-opacity="0.25" stroke-width="1"/>
        <circle cx="{fx + fw / 2}" cy="{fy + fh / 2 - 12}" r="78" fill="none"
                stroke="{t['line']}" stroke-opacity="0.06" stroke-width="1"/>
        <text x="{fx + fw / 2}" y="{fy + fh / 2 + 10}" text-anchor="middle"
              font-size="58" font-weight="600" fill="{t['accent']}"
              fill-opacity="0.85">{monogram}</text>
        <text class="mono" x="{fx + fw / 2}" y="{fy + fh - 26}" text-anchor="middle"
              font-size="9.5" letter-spacing="1.5" fill="{t['text_faint']}">AWAITING PORTRAIT</text>"""

    return f"""
    <g class="frame-in">
      <rect x="{fx - 14}" y="{fy - 14}" width="{fw + 28}" height="{fh + 28}" rx="26"
            fill="{t['glass']}" stroke="{t['line']}" stroke-opacity="0.08"/>
      <ellipse cx="{fx + fw / 2}" cy="{fy + fh / 2}" rx="{fw * 0.75}" ry="{fh * 0.62}"
               fill="{t['accent']}" opacity="0.055" filter="url(#soft)"/>
      <clipPath id="portraitClip">
        <rect x="{fx}" y="{fy}" width="{fw}" height="{fh}" rx="18"/>
      </clipPath>
      {content}
      <rect x="{fx}" y="{fy}" width="{fw}" height="{fh}" rx="18" fill="none"
            stroke="{t['accent']}" stroke-opacity="0.35" stroke-width="1.2"/>
      <rect x="{fx + 0.5}" y="{fy + 0.5}" width="{fw - 1}" height="{fh - 1}" rx="17.5"
            fill="none" stroke="{t['line']}" stroke-opacity="0.10" stroke-width="1"/>
      <circle class="pulse" cx="{fx + fw - 16}" cy="{fy + 16}" r="4" fill="{t['ok']}"
              stroke="{t['bg']}" stroke-width="2.5"/>
      <text class="mono" x="{fx + fw / 2}" y="{fy + fh + 34}" text-anchor="middle"
            font-size="11" letter-spacing="2"
            fill="{t['text_faint']}">identity<tspan fill="{t['accent']}"> : </tspan>{label}</text>
    </g>"""


def build_hero(cfg: dict, portrait_uri: str | None) -> str:
    t = cfg["theme"]
    os_title = esc(f'{cfg["os"]["title"]} — v{cfg["os"]["version"]}')

    boot_svg, boot_end = build_boot_lines(cfg, t)
    identity_svg = build_identity_block(cfg, t, boot_end)
    portrait_svg = build_portrait_frame(cfg, t, portrait_uri)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}"
     xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     role="img" aria-label="{esc(cfg['identity']['name'])} &#8212; {esc(cfg['os']['title'])}">
  <style>{hero_css(t)}</style>

  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.9" y2="1">
      <stop offset="0" stop-color="{t['bg']}"/>
      <stop offset="1" stop-color="{t['bg_deep']}"/>
    </linearGradient>
    <radialGradient id="glowTL" cx="0.12" cy="0.05" r="0.75">
      <stop offset="0" stop-color="{t['accent']}" stop-opacity="0.085"/>
      <stop offset="1" stop-color="{t['accent']}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="glowBR" cx="0.92" cy="1" r="0.8">
      <stop offset="0" stop-color="#4A5BDC" stop-opacity="0.06"/>
      <stop offset="1" stop-color="#4A5BDC" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="sheenG" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#fff" stop-opacity="0"/>
      <stop offset="0.5" stop-color="#fff" stop-opacity="0.035"/>
      <stop offset="1" stop-color="#fff" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="portraitWash" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#8FA3FF" stop-opacity="0.10"/>
      <stop offset="0.45" stop-color="#000000" stop-opacity="0"/>
      <stop offset="1" stop-color="{t['accent']}" stop-opacity="0.12"/>
    </linearGradient>
    <radialGradient id="placeholderBg" cx="0.5" cy="0.38" r="0.9">
      <stop offset="0" stop-color="#1A1118"/>
      <stop offset="1" stop-color="#0C0A10"/>
    </radialGradient>
    <filter id="soft" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="26"/>
    </filter>
    <clipPath id="window"><rect width="{W}" height="{H}" rx="22"/></clipPath>
  </defs>

  <!-- ===== window ===== -->
  <g clip-path="url(#window)">
    <rect width="{W}" height="{H}" fill="url(#bg)"/>
    <rect width="{W}" height="{H}" fill="url(#glowTL)"/>
    <rect width="{W}" height="{H}" fill="url(#glowBR)"/>
    <rect class="sheen" x="-260" y="-40" width="520" height="{H + 80}"
          fill="url(#sheenG)" transform="skewX(-18)"/>

    <!-- title bar -->
    <circle cx="34" cy="{BAR_H / 2}" r="5.5" fill="#FF5F57" opacity="0.85"/>
    <circle cx="56" cy="{BAR_H / 2}" r="5.5" fill="#FEBC2E" opacity="0.85"/>
    <circle cx="78" cy="{BAR_H / 2}" r="5.5" fill="#28C840" opacity="0.85"/>
    <text class="mono" x="{W / 2}" y="{BAR_H / 2 + 4}" text-anchor="middle"
          font-size="12" letter-spacing="1.5" fill="{t['text_faint']}">{os_title}</text>
    <line x1="0" y1="{BAR_H}" x2="{W}" y2="{BAR_H}"
          stroke="{t['line']}" stroke-opacity="0.06"/>

    {boot_svg}
    {identity_svg}
    {portrait_svg}
  </g>

  <!-- outer hairline -->
  <rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="21.5"
        fill="none" stroke="{t['line']}" stroke-opacity="0.10"/>
</svg>"""


# ----------------------------------------------------------------
# Repository explorer — projects are browsed, not read
# ----------------------------------------------------------------

def wrap_text(text: str, width: int) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def build_explorer(cfg: dict, gh: dict | None = None) -> str:
    t = cfg["theme"]
    # real repositories when live data is on, the config list otherwise
    projects = auto_projects(cfg, gh) or cfg.get("projects", [])[:8]
    if not projects:
        return ""
    # featured project rotates daily — the profile changes between visits
    featured_i = day_index(len(projects))
    featured = projects[featured_i]

    bar_h = 46
    tree_x, tree_top = 44, bar_h + 44
    row_h = 27
    h = max(tree_top + row_h * (len(projects) + 1) + 46, 300)
    split = 330  # sidebar | detail divider

    # ---- sidebar tree ----
    rows = [
        f'<text class="in mono" style="animation-delay:.25s" x="{tree_x}" '
        f'y="{tree_top}" font-size="12.5" fill="{t["text_dim"]}">'
        f'<tspan fill="{t["accent"]}">&#9662;</tspan>  '
        f'{esc(label(cfg, "explorer.root", "~/projects"))}</text>'
    ]
    y = tree_top + row_h
    for i, p in enumerate(projects):
        branch = "&#9492;&#9472;&#9472;" if i == len(projects) - 1 else "&#9500;&#9472;&#9472;"
        sel = i == featured_i
        delay = 0.4 + i * 0.14
        if sel:
            rows.append(
                f'<g class="in" style="animation-delay:{delay:.2f}s">'
                f'<rect x="{tree_x - 12}" y="{y - 15}" width="{split - tree_x - 20}" '
                f'height="24" rx="7" fill="{t["accent"]}" opacity="0.09"/>'
                f'<rect x="{tree_x - 12}" y="{y - 12}" width="2.5" height="18" rx="1.25" '
                f'fill="{t["accent"]}"/>'
                f'<text class="mono" x="{tree_x + 4}" y="{y}" font-size="12.5" '
                f'fill="{t["text"]}">{branch} {esc(p["name"])}</text></g>'
            )
        else:
            rows.append(
                f'<text class="in mono" style="animation-delay:{delay:.2f}s" '
                f'x="{tree_x + 4}" y="{y}" font-size="12.5" '
                f'fill="{t["text_faint"]}">{branch} {esc(p["name"])}</text>'
            )
        y += row_h

    # ---- detail pane (featured project as an app) ----
    dx = split + 42
    name = esc(featured["name"])
    status = esc(str(featured.get("status", "active")))
    desc_lines = wrap_text(featured.get("desc", ""), 56)[:3]
    d = 0.75

    detail = [
        f'<text class="in" style="animation-delay:{d:.2f}s" x="{dx}" y="{bar_h + 52}" '
        f'font-size="21" font-weight="650" letter-spacing="0.3" '
        f'fill="{t["text"]}">{name}</text>'
    ]
    st_w = round(len(featured.get("status", "active")) * 6.2) + 34
    detail.append(
        f'<g class="in" style="animation-delay:{d + 0.1:.2f}s">'
        f'<rect x="{dx + len(featured["name"]) * 12 + 22}" y="{bar_h + 35}" '
        f'width="{st_w}" height="22" rx="11" fill="{t["glass"]}" '
        f'stroke="{t["line"]}" stroke-opacity="0.09"/>'
        f'<circle class="pulse" cx="{dx + len(featured["name"]) * 12 + 22 + 13}" '
        f'cy="{bar_h + 46}" r="2.8" fill="{t["ok"]}"/>'
        f'<text class="mono" x="{dx + len(featured["name"]) * 12 + 22 + 23}" '
        f'y="{bar_h + 50}" font-size="10" letter-spacing="1" '
        f'fill="{t["text_dim"]}">{status}</text></g>'
    )
    dy = bar_h + 84
    for i, line in enumerate(desc_lines):
        detail.append(
            f'<text class="in" style="animation-delay:{d + 0.18 + i * 0.08:.2f}s" '
            f'x="{dx}" y="{dy}" font-size="13.5" fill="{t["text_dim"]}">{esc(line)}</text>'
        )
        dy += 21

    # stack chips
    cx = dx
    dy += 16
    for i, tech in enumerate(featured.get("stack", [])[:5]):
        cw = round(len(str(tech)) * 6.4) + 26
        if cx + cw > W - 40:
            break
        detail.append(
            f'<g class="in" style="animation-delay:{d + 0.45 + i * 0.09:.2f}s">'
            f'<rect x="{cx}" y="{dy - 15}" width="{cw}" height="24" rx="12" '
            f'fill="{t["glass"]}" stroke="{t["accent"]}" stroke-opacity="0.22"/>'
            f'<text class="mono" x="{cx + cw / 2}" y="{dy + 1}" text-anchor="middle" '
            f'font-size="10.5" fill="{t["text_dim"]}">{esc(tech)}</text></g>'
        )
        cx += cw + 10

    meta = f'type: {featured.get("type", "project")}   ·   since {featured.get("since", "—")}'
    stars = featured.get("stars")
    if stars is None and gh:
        stars = gh.get("repo_stars", {}).get(featured["name"])
    if stars is not None:
        meta += f'   ·   ★ {stars}'
    detail.append(
        f'<text class="in mono" style="animation-delay:{d + 0.7:.2f}s" x="{dx}" '
        f'y="{dy + 40}" font-size="11" letter-spacing="0.5" '
        f'fill="{t["text_faint"]}">{esc(meta)}</text>'
    )

    if gh:
        footer = esc(fill(label(cfg, "explorer.footer_live",
                                "{repos} public repositories · ★ {stars} collected · "
                                "{followers} followers · featured rotates daily"),
                          repos=gh["public_repos"], stars=gh["stars"],
                          followers=gh["followers"]))
    else:
        footer = esc(fill(label(cfg, "explorer.footer",
                                "{count} repositories mounted · featured rotates "
                                "daily · {os}"),
                          count=len(projects), os=cfg["os"]["title"]))

    divider = (f'<line x1="{split}" y1="{bar_h}" x2="{split}" y2="{h - 40}" '
               f'stroke="{t["line"]}" stroke-opacity="0.05"/>')
    return window(cfg, "exp",
                  label(cfg, "explorer.title", "repository_explorer"),
                  label(cfg, "explorer.right", "~/projects"),
                  h, divider + "".join(rows) + "".join(detail), footer)


# ----------------------------------------------------------------
# Package manager — skills without fake percentage bars
# ----------------------------------------------------------------

def build_packages(cfg: dict) -> str:
    t = cfg["theme"]
    groups = cfg.get("skills", [])[:3]
    if not groups:
        return ""
    if len(cfg.get("skills", [])) > 3:
        print("  warning: more than 3 skill groups — only 3 columns fit; trimming.")

    # channel colours belong to the theme — a monochrome palette must not
    # get an amber and a purple dropped into it
    channel_color = {
        "stable": t.get("channel_stable", t["ok"]),
        "learning": t.get("channel_learning", "#FEBC2E"),
        "experimental": t.get("channel_experimental", "#B48CFF"),
    }

    bar_h = 46
    col_x = [44, 372, 700]
    top = bar_h + 78
    row_h = 26
    max_items = max(len(g["items"]) for g in groups)
    h = top + 24 + max_items * row_h + 52

    body = [
        f'<text class="in mono" style="animation-delay:.2s" x="44" y="{bar_h + 40}" '
        f'font-size="12.5" fill="{t["text_dim"]}">'
        f'<tspan fill="{t["accent"]}">$</tspan> '
        f'{esc(label(cfg, "packages.command", "pkg list --installed"))}</text>'
    ]
    total = 0
    for ci, group in enumerate(groups):
        x = col_x[ci]
        body.append(
            f'<text class="in mono" style="animation-delay:{0.35 + ci * 0.12:.2f}s" '
            f'x="{x}" y="{top}" font-size="11" letter-spacing="2" '
            f'fill="{t["text_faint"]}">{esc(str(group["group"]).upper())}/</text>'
        )
        y = top + 28
        for ri, item in enumerate(group["items"][:10]):
            total += 1
            ch = str(item.get("channel", "stable"))
            color = channel_color.get(ch, t["text_dim"])
            body.append(
                f'<text class="in mono" style="animation-delay:{0.45 + ci * 0.12 + ri * 0.07:.2f}s" '
                f'x="{x}" y="{y}" font-size="13" fill="{t["text"]}">'
                f'<tspan fill="{t["accent"]}" fill-opacity="0.7">&#9642;</tspan>  {esc(item["name"])}'
                f'  <tspan font-size="10.5" fill="{color}">@{esc(ch)}</tspan></text>'
            )
            y += row_h

    footer = esc(fill(label(cfg, "packages.footer",
                            "{count} packages installed · 0 vulnerabilities · "
                            "channels: stable / learning / experimental"),
                      count=total))

    return window(cfg, "pkg",
                  label(cfg, "packages.title", "package_manager"),
                  fill(label(cfg, "packages.right", "pkg v{version}"),
                       version=cfg["os"]["version"]),
                  h, "".join(body), footer)


# ----------------------------------------------------------------
# System log — the timeline as a story
# ----------------------------------------------------------------

def build_timeline(cfg: dict) -> str:
    t = cfg["theme"]
    entries = cfg.get("timeline", [])[:7]
    if not entries:
        return ""

    bar_h = 46
    top = bar_h + 46
    row_h = 58
    line_x = 64
    h = top + row_h * len(entries) + 34

    body = []
    first_y = top + 4
    last_y = top + row_h * (len(entries) - 1) + 4
    body.append(
        f'<line class="fade" style="animation-delay:.3s" x1="{line_x}" y1="{first_y}" '
        f'x2="{line_x}" y2="{last_y}" stroke="{t["line"]}" stroke-opacity="0.12"/>'
    )
    for i, e in enumerate(entries):
        y = top + i * row_h
        last = i == len(entries) - 1
        delay = 0.35 + i * 0.16
        dot_color = t["ok"] if last else t["accent"]
        dot = (
            f'<circle class="pulse" cx="{line_x}" cy="{y}" r="4.5" fill="{dot_color}"/>'
            f'<circle cx="{line_x}" cy="{y}" r="9" fill="{dot_color}" opacity="0.15"/>'
            if last else
            f'<circle cx="{line_x}" cy="{y}" r="3.5" fill="{dot_color}" fill-opacity="0.8"/>'
            f'<circle cx="{line_x}" cy="{y}" r="3.5" fill="none" stroke="{t["bg"]}" stroke-width="1"/>'
        )
        title_fill = t["text"] if last else t["text_dim"]
        body.append(
            f'<g class="in" style="animation-delay:{delay:.2f}s">{dot}'
            f'<text class="mono" x="{line_x + 28}" y="{y + 4}" font-size="11.5" '
            f'letter-spacing="1" fill="{t["accent"]}" fill-opacity="0.85">[{esc(e["year"])}]</text>'
            f'<text x="{line_x + 100}" y="{y + 4.5}" font-size="14.5" font-weight="600" '
            f'fill="{title_fill}">{esc(e["title"])}</text>'
            f'<text x="{line_x + 100}" y="{y + 25}" font-size="12.5" '
            f'fill="{t["text_faint"]}">{esc(e.get("detail", ""))}</text></g>'
        )

    return window(cfg, "log",
                  label(cfg, "timeline.title", "system_log"),
                  label(cfg, "timeline.right", "journal --follow"),
                  h, "".join(body))


# ----------------------------------------------------------------
# Footer — contact, then EOF
# ----------------------------------------------------------------

def build_footer(cfg: dict) -> str:
    t = cfg["theme"]
    contact = cfg.get("contact", {})
    h = 108
    chips = []
    x = 44

    entries = []
    if contact.get("email"):
        entries.append((label(cfg, "contact.email_label", "mail"), contact["email"]))
    for link in contact.get("links", []):
        if link.get("url"):
            display = link["url"].replace("https://", "").replace("http://", "").rstrip("/")
            entries.append((str(link.get("label", "link")), display))

    for i, (key, value) in enumerate(entries):
        cw = round(len(f"{key}: {value}") * 6.6) + 34
        if x + cw > W - 40:
            break
        chips.append(
            f'<g class="in" style="animation-delay:{0.3 + i * 0.12:.2f}s">'
            f'<rect x="{x}" y="{h / 2 - 1}" width="{cw}" height="28" rx="14" '
            f'fill="{t["glass"]}" stroke="{t["line"]}" stroke-opacity="0.09"/>'
            f'<text class="mono" x="{x + 17}" y="{h / 2 + 17}" font-size="11" '
            f'fill="{t["text_dim"]}"><tspan fill="{t["accent"]}">{esc(key)}</tspan>'
            f': {esc(value)}</text></g>'
        )
        x += cw + 12

    command = esc(label(cfg, "contact.command", "contact --open"))
    eof = esc(fill(label(cfg, "contact.eof", "EOF · {os}"), os=cfg["os"]["title"]))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="{W}" height="{h}" viewBox="0 0 {W} {h}"
     xmlns="http://www.w3.org/2000/svg" role="img" aria-label="contact">
  <style>{hero_css(t)}</style>
  <defs>
    <linearGradient id="bgeof" x1="0" y1="0" x2="0.9" y2="1">
      <stop offset="0" stop-color="{t['bg']}"/>
      <stop offset="1" stop-color="{t['bg_deep']}"/>
    </linearGradient>
    <clipPath id="cpeof"><rect width="{W}" height="{h}" rx="22"/></clipPath>
  </defs>
  <g clip-path="url(#cpeof)">
    <rect width="{W}" height="{h}" fill="url(#bgeof)"/>
    <text class="in mono" style="animation-delay:.15s" x="44" y="34" font-size="12.5"
          fill="{t['text_dim']}"><tspan fill="{t['accent']}">$</tspan> {command}</text>
    {"".join(chips)}
    <text class="mono" x="{W - 30}" y="{h - 20}" text-anchor="end" font-size="10"
          letter-spacing="1.5" fill="{t['text_faint']}">{eof}</text>
  </g>
  <rect x="0.5" y="0.5" width="{W - 1}" height="{h - 1}" rx="21.5"
        fill="none" stroke="{t['line']}" stroke-opacity="0.10"/>
</svg>"""


# ----------------------------------------------------------------
# Hidden layers — only seen by visitors who open them
# ----------------------------------------------------------------

def window(cfg: dict, uid: str, title: str, right: str, h: int, body: str,
           footer: str = "") -> str:
    """Shared OS window chrome, so every panel belongs to the same system."""
    t = cfg["theme"]
    bar_h = 46
    foot = ""
    if footer:
        foot = (f'<line x1="0" y1="{h - 40}" x2="{W}" y2="{h - 40}" '
                f'stroke="{t["line"]}" stroke-opacity="0.05"/>'
                f'<text class="mono" x="{W / 2}" y="{h - 16}" text-anchor="middle" '
                f'font-size="10" letter-spacing="1" fill="{t["text_faint"]}">{footer}</text>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="{W}" height="{h}" viewBox="0 0 {W} {h}"
     xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{esc(title)}">
  <style>{hero_css(t)}</style>
  <defs>
    <linearGradient id="bg{uid}" x1="0" y1="0" x2="0.9" y2="1">
      <stop offset="0" stop-color="{t['bg']}"/>
      <stop offset="1" stop-color="{t['bg_deep']}"/>
    </linearGradient>
    <radialGradient id="gl{uid}" cx="0.5" cy="0" r="0.9">
      <stop offset="0" stop-color="{t['accent']}" stop-opacity="0.045"/>
      <stop offset="1" stop-color="{t['accent']}" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="cp{uid}"><rect width="{W}" height="{h}" rx="22"/></clipPath>
  </defs>
  <g clip-path="url(#cp{uid})">
    <rect width="{W}" height="{h}" fill="url(#bg{uid})"/>
    <rect width="{W}" height="{h}" fill="url(#gl{uid})"/>
    <text class="mono" x="30" y="{bar_h / 2 + 4.5}" font-size="12" letter-spacing="1.5"
          fill="{t['text_dim']}"><tspan fill="{t['accent']}">&#9656;</tspan>  {esc(title)}</text>
    <text class="mono" x="{W - 30}" y="{bar_h / 2 + 4.5}" text-anchor="end"
          font-size="11.5" fill="{t['text_faint']}">{esc(right)}</text>
    <line x1="0" y1="{bar_h}" x2="{W}" y2="{bar_h}" stroke="{t['line']}" stroke-opacity="0.06"/>
    {body}
    {foot}
  </g>
  <rect x="0.5" y="0.5" width="{W - 1}" height="{h - 1}" rx="21.5"
        fill="none" stroke="{t['line']}" stroke-opacity="0.10"/>
</svg>"""


def build_manual(cfg: dict) -> str:
    t = cfg["theme"]
    man = cfg.get("hidden", {}).get("manual", {})
    sections = man.get("sections", [])[:5]
    if not sections:
        return ""

    y, body, d = 92, [], 0.2
    for sec in sections:
        body.append(
            f'<text class="in mono" style="animation-delay:{d:.2f}s" x="44" y="{y}" '
            f'font-size="11.5" font-weight="700" letter-spacing="2" '
            f'fill="{t["accent"]}">{esc(sec["heading"])}</text>'
        )
        y += 24
        d += 0.1
        for line in sec.get("lines", [])[:6]:
            body.append(
                f'<text class="in mono" style="animation-delay:{d:.2f}s" x="88" y="{y}" '
                f'font-size="12.5" fill="{t["text_dim"]}">{esc(line)}</text>'
            )
            y += 21
            d += 0.06
        y += 16

    return window(cfg, "man",
                  label(cfg, "manual.title", "manual_page"),
                  label(cfg, "manual.right", "man(1)"),
                  y + 14, "".join(body))


def build_system(cfg: dict) -> str:
    t = cfg["theme"]
    sysc = cfg.get("hidden", {}).get("system", {})
    specs = sysc.get("specs", [])[:10]
    if not specs:
        return ""

    # two columns, key right-aligned to a gutter — reads like neofetch
    rows_per_col = (len(specs) + 1) // 2
    body, d = [], 0.2
    for i, spec in enumerate(specs):
        col, row = divmod(i, rows_per_col)
        x = 44 + col * 480
        y = 96 + row * 30
        body.append(
            f'<g class="in" style="animation-delay:{d:.2f}s">'
            f'<text class="mono" x="{x}" y="{y}" font-size="12" letter-spacing="1" '
            f'fill="{t["accent"]}" fill-opacity="0.8">{esc(spec["key"])}</text>'
            f'<text class="mono" x="{x + 96}" y="{y}" font-size="12.5" '
            f'fill="{t["text_dim"]}">{esc(spec["value"])}</text></g>'
        )
        d += 0.08

    h = 96 + rows_per_col * 30 + 34
    return window(cfg, "sys",
                  label(cfg, "system.title", "system_info"),
                  label(cfg, "system.right", "uname -a"),
                  h, "".join(body))


def build_secret(cfg: dict) -> str:
    t = cfg["theme"]
    sec = cfg.get("hidden", {}).get("secret", {})
    fortunes = sec.get("fortunes", [])
    if not fortunes:
        return ""
    fortune = fortunes[day_index(len(fortunes))]
    lines = wrap_text(fortune, 62)[:3]

    body = [
        f'<text class="in mono" style="animation-delay:.15s" x="44" y="86" '
        f'font-size="11.5" letter-spacing="1.5" fill="{t["text_faint"]}">'
        f'&#9656; {esc(label(cfg, "secret.intro", "you found it."))}</text>'
    ]
    y = 122
    for i, line in enumerate(lines):
        body.append(
            f'<text class="in" style="animation-delay:{0.3 + i * 0.12:.2f}s" x="44" '
            f'y="{y}" font-size="15" font-style="italic" '
            f'fill="{t["text"]}">{esc(line)}</text>'
        )
        y += 25
    note = label(cfg, "secret.note", "a different one appears tomorrow")
    body.append(
        f'<g class="in" style="animation-delay:.75s">'
        f'<text class="mono" x="44" y="{y + 18}" font-size="11" '
        f'fill="{t["text_faint"]}">{esc(note)}</text>'
        f'<rect class="cursor" x="{44 + len(note) * 6.6 + 8:.0f}" y="{y + 8}" '
        f'width="7" height="13" rx="1.5" fill="{t["accent"]}"/></g>'
    )

    return window(cfg, "sec",
                  label(cfg, "secret.title", "secrets"),
                  label(cfg, "secret.right", "cat .secrets"),
                  y + 46, "".join(body))


# ----------------------------------------------------------------
# README
# ----------------------------------------------------------------

def build_readme(cfg: dict, sections: list, hidden: list, tail: list,
                 src: str = "output") -> str:
    def stack(items):
        return "\n  ".join(
            f'<img src="{src}/{name}.svg" width="100%" alt="{esc(alt)}"/>'
            for name, alt in items
        )

    imgs = stack(sections)

    # progressive disclosure: each hidden panel is a terminal command
    blocks = []
    for name, summary, alt in hidden:
        blocks.append(
            f'<details>\n'
            f'<summary><code>&#9656;&nbsp; $ {esc(summary)}</code></summary>\n'
            f'<br/>\n'
            f'<div align="center">\n'
            f'  <img src="output/{name}.svg" width="100%" alt="{esc(alt)}"/>\n'
            f'</div>\n'
            f'</details>\n'
        )
    hidden_md = ("\n" + "\n".join(blocks)) if blocks else ""

    # clickable mirror of the footer (SVGs inside <img> can't be links)
    # raw <a>, not markdown — GitHub does not parse markdown inside HTML blocks
    contact = cfg.get("contact", {})
    links = []
    if contact.get("email"):
        links.append(f'<a href="mailto:{esc(contact["email"])}">mail</a>')
    for link in contact.get("links", []):
        if link.get("url"):
            links.append(f'<a href="{esc(link["url"])}">'
                         f'{esc(link.get("label", "link"))}</a>')
    link_line = f'\n<p align="center">{" &nbsp;&#183;&nbsp; ".join(links)}</p>\n' if links else ""
    tail_md = f'\n<div align="center">\n  {stack(tail)}\n</div>\n' if tail else ""

    # the deepest layer: only visible to someone reading the raw markdown
    note = (cfg.get("readme") or {}).get("source_note", "")
    egg = f'<!--\n{note.rstrip()}\n-->\n\n' if str(note).strip() else ""
    if "-->" in str(note):  # never let config break out of the comment
        egg = ""

    return (f'{egg}<div align="center">\n  {imgs}\n</div>\n'
            f'{hidden_md}{tail_md}{link_line}')


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Generate a profile from a config file. Point --config at "
                    "someone else's config and the same code builds their OS.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="output", help="directory for the SVGs")
    ap.add_argument("--readme", default="README.md")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out_dir = ROOT / args.out
    readme_path = ROOT / args.readme
    out_dir.mkdir(parents=True, exist_ok=True)
    readme_path.parent.mkdir(parents=True, exist_ok=True)
    src = os.path.relpath(out_dir, readme_path.parent).replace("\\", "/")
    (ROOT / cfg["portrait"]["source_dir"]).mkdir(parents=True, exist_ok=True)

    print(f"generating {cfg['os']['title']} ...")
    portrait_path = find_portrait(cfg)
    portrait_uri = embed_portrait(portrait_path, cfg) if portrait_path else None
    if not portrait_path:
        print("  portrait: none found -> vector placeholder "
              f"(drop an image into {cfg['portrait']['source_dir']}/)")

    gh = fetch_github(cfg)
    total = 0

    def emit(bucket: list, name: str, svg: str, alt: str, summary: str = ""):
        nonlocal total
        if not svg:
            return
        (out_dir / f"{name}.svg").write_text(svg, encoding="utf-8")
        size = len(svg.encode())
        total += size
        print(f"  {name}.svg: {size // 1024} KB")
        bucket.append((name, summary, alt) if summary else (name, alt))

    sections, hidden, tail = [], [], []

    emit(sections, "hero", build_hero(cfg, portrait_uri),
         f'{cfg["identity"]["name"]} — {cfg["os"]["title"]}')
    emit(sections, "explorer", build_explorer(cfg, gh), "repository explorer")
    emit(sections, "packages", build_packages(cfg), "installed packages")
    emit(sections, "timeline", build_timeline(cfg), "system log")

    h = cfg.get("hidden", {})
    emit(hidden, "manual", build_manual(cfg), "manual page",
         h.get("manual", {}).get("summary", "man"))
    emit(hidden, "system", build_system(cfg), "system info",
         h.get("system", {}).get("summary", "uname -a"))
    emit(hidden, "secret", build_secret(cfg), "secrets",
         h.get("secret", {}).get("summary", "cat .secrets"))

    emit(tail, "footer", build_footer(cfg), "contact")

    readme_path.write_text(
        build_readme(cfg, sections, hidden, tail, src), encoding="utf-8")
    print(f"  {readme_path.name} written — {total // 1024} KB total")
    return 0


if __name__ == "__main__":
    sys.exit(main())

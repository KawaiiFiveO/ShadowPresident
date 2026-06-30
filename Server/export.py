#!/usr/bin/env python3
"""Export a Shadow President run log (.jsonl) to a self-contained, retro-themed HTML page.

The JSONL is an interleaved narrative record of three entry types:
  - dialogue   : {type, run_id, turn, text}            one transcript line
  - decision   : {type, run_id, timestamp, turn, step, fragment, decision_type,
                  choices[], choice_index, reasoning, prompt_tokens, completion_tokens,
                  model_name}
  - checkpoint : {type, run_id, timestamp, turn, step, fragment}   autosave boundary

The output is ONE portable .html file: all CSS/JS inline, transcript rendered server-side,
and the browser handles search, turn/conversation jump-nav, expandable per-decision
reasoning (shown off to the side, timeline-style), and an editable "final analysis" box
that persists to localStorage.

Usage:
    python export.py [path/to/run.jsonl] [--name NAME] [--out FILE.html] [--analysis FILE]

If no path is given, the active run from logs/last_run.json is used. --name sets the
operator label that replaces the "Player" speaker (defaults to the model parsed from the
filename, e.g. DeepSeek-R1-Distill-Qwen-14B).
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime

# ── decision_type → (badge label, css class) ────────────────────────────────────
BADGE = {
    "dialogue":       ("CHOICE",  "dt-dialogue"),
    "bill":           ("BILL",    "dt-bill"),
    "paged_decision": ("POLICY",  "dt-paged"),
    "decision_panel": ("EVENT",   "dt-event"),
    "decree":         ("DECREE",  "dt-decree"),
}


def esc(s):
    return html.escape(s or "", quote=True)


def clean(s):
    """Strip the irrecoverable U+FFFD replacement chars left by upstream smart-quote
    corruption, plus trailing/leading whitespace. Internal newlines are preserved
    (narration can be multi-paragraph; the CSS renders them with white-space:pre-wrap)."""
    if not s:
        return ""
    return s.replace("�", "").strip()


def pretty_fragment(frag):
    """Turn01_Start_Inauguration -> 'Start Inauguration'.  Empty -> 'Intro'."""
    if not frag:
        return "Intro"
    m = re.match(r"Turn\d+_(.*)", frag)
    rest = m.group(1) if m else frag
    rest = rest.replace("_", " ")
    rest = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", rest)   # split camelCase
    rest = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", rest)    # split trailing digits
    # Strip leading Articy tags: 1–2 char capitalised codes (e.g. "Sn", "O") or bare numbers
    tokens = rest.strip().split()
    stripped = 0
    while stripped < 4 and len(tokens) > 1:
        if re.fullmatch(r"[A-Z]+[a-z]?|\d+", tokens[0]):
            tokens.pop(0)
            stripped += 1
        else:
            break
    return " ".join(tokens) or "Intro"


def model_from_filename(path):
    """run_20260625_144631_deepseek-r1-distill-qwen-14b.jsonl -> deepseek-r1-distill-qwen-14b"""
    base = os.path.basename(path)
    base = re.sub(r"\.jsonl$", "", base, flags=re.IGNORECASE)
    m = re.match(r"run_\d{8}_\d{6}_(.+)$", base)
    return m.group(1) if m else base


def prettify_model(model):
    """deepseek-r1-distill-qwen-14b -> 'DeepSeek-R1-Distill-Qwen-14B' (best-effort)."""
    def cap(tok):
        if re.fullmatch(r"r\d+", tok):
            return tok.upper()
        if re.fullmatch(r"\d+b", tok):
            return tok.upper()
        if tok == "deepseek":
            return "DeepSeek"
        if tok == "qwen":
            return "Qwen"
        if tok == "qat":
            return "QAT"
        return tok[:1].upper() + tok[1:]
    return "-".join(cap(t) for t in model.split("-"))


# ── load ─────────────────────────────────────────────────────────────────────────

def resolve_input(arg_path):
    if arg_path:
        return arg_path
    here = os.path.dirname(os.path.abspath(__file__))
    last = os.path.join(here, "logs", "last_run.json")
    if os.path.exists(last):
        with open(last, encoding="utf-8") as f:
            meta = json.load(f)
        rel = meta.get("jsonl")
        if rel:
            return os.path.join(here, rel)
    sys.exit("No input file given and logs/last_run.json not found.")


def load_entries(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


# ── rendering ─────────────────────────────────────────────────────────────────────

def render_dialogue(o, player_name):
    text = o.get("text", "")
    who, _, body = text.partition(":")
    if not body and ":" not in text:
        who, body = "", text
    who = who.strip()
    body = clean(body if (body or ":" in text) else text)

    cls = "line"
    label = who
    if who in ("Player", "Player_Italic"):
        label = player_name
        cls += " spk-player"
        if who == "Player_Italic":
            cls += " italic"
    elif who == "Narrator":
        cls += " spk-narrator"
    elif who == "[AUTO]":
        label = ""
        cls += " spk-narrator auto"
    elif who:
        cls += " spk-char"
    else:
        cls += " spk-narrator"

    data = (label + " " + body).lower()
    who_html = f'<span class="who">{esc(label)}</span>' if label else ""
    return (f'<div class="entry {cls}" data-s="{esc(data)}">'
            f'{who_html}<span class="txt">{esc(body)}</span></div>')


def render_decision(o, player_name, idx):
    dtype = o.get("decision_type", "")
    badge, dcls = BADGE.get(dtype, (dtype.upper() or "DECISION", "dt-dialogue"))
    choices = o.get("choices") or []
    ci = o.get("choice_index")
    is_human = str(o.get("model_name")) == "human"

    chosen_txt = ""
    opt_items = []
    for c in choices:
        txt = clean(c.get("text", ""))
        chosen = (c.get("index") == ci)
        if chosen:
            chosen_txt = txt
        mark = "▶" if chosen else "·"
        ocls = "opt chosen" if chosen else "opt"
        opt_items.append(f'<li class="{ocls}"><span class="mk">{mark}</span>'
                         f'<span class="ot">{esc(txt)}</span></li>')
    if not opt_items and ci is not None:
        opt_items.append(f'<li class="opt chosen"><span class="mk">▶</span>'
                         f'<span class="ot">[choice #{esc(str(ci))}]</span></li>')

    reasoning = clean(o.get("reasoning", ""))
    ptok = o.get("prompt_tokens") or 0
    ctok = o.get("completion_tokens") or 0
    ts = o.get("timestamp", "")
    try:
        tlabel = datetime.fromisoformat(ts).strftime("%H:%M:%S") if ts else ""
    except ValueError:
        tlabel = ""

    if is_human:
        side = '<div class="reasoning human">[ HUMAN DECISION — no model reasoning ]</div>'
        toggle = '<button class="r-toggle" disabled>HUMAN</button>'
    elif reasoning:
        side = f'<div class="reasoning">{esc(reasoning)}</div>'
        toggle = '<button class="r-toggle">&gt; REASONING</button>'
    else:
        side = '<div class="reasoning empty">[ no reasoning recorded ]</div>'
        toggle = '<button class="r-toggle">&gt; REASONING</button>'

    meta_bits = []
    if tlabel:
        meta_bits.append(f'<span class="t">{esc(tlabel)}</span>')
    if not is_human and (ptok or ctok):
        meta_bits.append(f'<span class="tok">p{ptok} / c{ctok}</span>')
    meta_html = " ".join(meta_bits)

    data = (badge + " " + chosen_txt + " " + reasoning).lower()
    return (
        f'<div class="entry decision {dcls}" data-s="{esc(data)}" id="dec-{idx}">'
        f'  <div class="dec-main">'
        f'    <div class="dec-head"><span class="badge">{esc(badge)}</span>'
        f'      <span class="who">{esc(player_name)}</span>{meta_html}</div>'
        f'    <ul class="opts">{"".join(opt_items)}</ul>'
        f'  </div>'
        f'  <aside class="dec-side">{toggle}{side}</aside>'
        f'</div>'
    )


def latest_manifesto(entries):
    """Return the text of the last manifesto entry in the run (its final form), or ''."""
    text = ""
    for o in entries:
        if o.get("type") == "manifesto":
            text = clean(o.get("text", ""))
    return text


def build(entries, player_name):
    """Returns (toc_html, body_html, sections_by_turn). Sections are delimited by
    checkpoints (conversation/fragment boundaries) and grouped by turn for the nav."""
    sections = []           # list of dicts: {id, turn, title, html_parts}
    cur = {"turn": entries[0].get("turn", 1) if entries else 1,
           "title": None, "parts": [], "has_epilogue": False, "has_prologue": False}
    dec_idx = 0

    def close(frag, turn, stats=None, economy=None):
        nonlocal cur
        if cur["parts"] or cur["title"] is not None:
            if cur["has_prologue"]:
                cur["title"] = "Prologue"
            elif cur["title"] is None:
                cur["title"] = pretty_fragment(frag)
            cur["id"] = f"sec-{len(sections)}"
            if turn is not None:
                cur["turn"] = turn
            cur["stats"] = stats
            cur["economy"] = economy
            sections.append(cur)
        cur = {"turn": turn or 1, "title": None, "parts": [],
               "has_epilogue": False, "has_prologue": False}

    for o in entries:
        t = o.get("type")
        if t == "dialogue":
            txt = o.get("text", "")
            if txt.startswith("[CHOICE]"):
                continue   # echo of the decision card; fold it away
            cur["parts"].append(render_dialogue(o, player_name))
        elif t == "decision":
            if o.get("phase") == "epilogue":
                cur["has_epilogue"] = True
            elif o.get("phase") == "prologue":
                cur["has_prologue"] = True
            cur["parts"].append(render_decision(o, player_name, dec_idx))
            dec_idx += 1
        elif t == "manifesto":
            continue   # manifesto is consolidated into one collapsed end panel, not inline
        elif t == "checkpoint":
            close(o.get("fragment", ""), o.get("turn"),
                  o.get("stats"), o.get("economy"))

    # trailing unterminated section (run ended mid-fragment, or epilogue which has no checkpoint)
    if cur["parts"]:
        cur["title"] = "Epilogue" if cur["has_epilogue"] else "In Progress"
        cur["id"] = f"sec-{len(sections)}"
        sections.append(cur)

    # Deduplicate sections that share the same (turn, title) — e.g. Turn01_Start_Inauguration
    # fires two checkpoints: prologue setup and the re-entered conversation.
    title_counts = {}
    for s in sections:
        key = (s["turn"], s["title"])
        title_counts[key] = title_counts.get(key, 0) + 1
    seen_idx = {}
    for s in sections:
        key = (s["turn"], s["title"])
        if title_counts[key] > 1:
            seen_idx[key] = seen_idx.get(key, 0) + 1
            if seen_idx[key] > 1:
                s["title"] += f" ({seen_idx[key]})"

    # body
    body_parts = []
    toc_groups = {}        # turn -> [(id, title)]
    last_turn = None
    for s in sections:
        turn = s["turn"]
        if turn != last_turn:
            body_parts.append(f'<h2 class="turn-marker" id="turn-{turn}">'
                              f'<span class="th-l">TURN</span> '
                              f'<span class="th-n">{turn:02d}</span></h2>')
            last_turn = turn
        econ_strip = render_econ_strip(s.get("stats"), s.get("economy"))
        body_parts.append(
            f'<section class="section" id="{s["id"]}" data-turn="{turn}">'
            f'<h3 class="sec-head"><span class="sec-dot">■</span> {esc(s["title"])}'
            f'<span class="sec-meta">T{turn:02d}</span></h3>'
            f'{econ_strip}'
            f'<div class="sec-body">{"".join(s["parts"])}</div>'
            f'</section>'
        )
        toc_groups.setdefault(turn, []).append((s["id"], s["title"]))

    toc_parts = []
    for turn in sorted(toc_groups):
        links = "".join(
            f'<a class="toc-link" href="#{sid}" data-target="{sid}">{esc(title)}</a>'
            for sid, title in toc_groups[turn]
        )
        toc_parts.append(
            f'<div class="toc-turn"><a class="toc-turn-h" href="#turn-{turn}">'
            f'TURN {turn:02d}</a>{links}</div>'
        )

    return "".join(toc_parts), "".join(body_parts), len(sections)


def compute_stats(entries):
    turns = set()
    dtypes = {}
    completion = 0
    prompt = 0
    n_dec = 0
    n_ckpt = 0
    timestamps = []
    run_id = ""
    for o in entries:
        run_id = run_id or o.get("run_id", "")
        if o.get("turn") is not None:
            turns.add(o["turn"])
        ts = o.get("timestamp")
        if ts:
            timestamps.append(ts)
        if o.get("type") == "decision":
            n_dec += 1
            dt = o.get("decision_type", "?")
            dtypes[dt] = dtypes.get(dt, 0) + 1
            completion += o.get("completion_tokens") or 0
            prompt += o.get("prompt_tokens") or 0
        elif o.get("type") == "checkpoint":
            n_ckpt += 1
    duration = ""
    if len(timestamps) >= 2:
        try:
            a = datetime.fromisoformat(min(timestamps))
            b = datetime.fromisoformat(max(timestamps))
            secs = int((b - a).total_seconds())
            h, rem = divmod(secs, 3600)
            m, s = divmod(rem, 60)
            duration = f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"
        except ValueError:
            pass
    return {
        "run_id": run_id,
        "turns": max(turns) if turns else 0,
        "decisions": n_dec,
        "checkpoints": n_ckpt,
        "dtypes": dtypes,
        "completion": completion,
        "prompt": prompt,
        "duration": duration,
    }


# ── economy parsing + chart ─────────────────────────────────────────────────────

def parse_stats(s):
    """'Government Budget: 7 | Personal Wealth: 2 | Economy: Unstable' ->
    {'budget': 7, 'wealth': 2, 'label': 'Unstable'}.  Missing fields -> None."""
    if not s:
        return {}
    out = {}
    mb = re.search(r"Government Budget:\s*(-?\d+)", s)
    mw = re.search(r"Personal Wealth:\s*(-?\d+)", s)
    ml = re.search(r"Economy:\s*([A-Za-z ]+)", s)
    if mb:
        out["budget"] = int(mb.group(1))
    if mw:
        out["wealth"] = int(mw.group(1))
    if ml:
        out["label"] = ml.group(1).strip()
    return out


def parse_econ_now(e):
    """'Economic Stability: ... (now 3, chg -1)' -> 3.  Missing -> None."""
    if not e:
        return None
    m = re.search(r"now (-?\d+)", e)
    return int(m.group(1)) if m else None


def compute_economy_series(entries):
    """Walk checkpoints in order, building a per-checkpoint series of
    {turn, budget, wealth, stability}.  Used by the chart; values may be None."""
    series = []
    for o in entries:
        if o.get("type") != "checkpoint":
            continue
        st = parse_stats(o.get("stats"))
        if not st and o.get("economy") is None:
            continue
        series.append({
            "turn": o.get("turn"),
            "budget": st.get("budget"),
            "wealth": st.get("wealth"),
            "stability": parse_econ_now(o.get("economy")),
        })
    return series


def render_econ_strip(stats, economy):
    """Compact per-section economy bar: Budget / Wealth / Stability chips + label."""
    st = parse_stats(stats)
    if not st:
        return ""
    chips = []
    if "budget" in st:
        chips.append(f'<span class="ec-chip ec-budget">BUDGET <b>{st["budget"]}</b></span>')
    if "wealth" in st:
        chips.append(f'<span class="ec-chip ec-wealth">WEALTH <b>{st["wealth"]}</b></span>')
    stab = parse_econ_now(economy)
    if stab is not None:
        chips.append(f'<span class="ec-chip ec-stab">STABILITY <b>{stab}</b></span>')
    if "label" in st:
        lab = st["label"].lower().replace(" ", "-")
        chips.append(f'<span class="ec-chip ec-label ec-{esc(lab)}">{esc(st["label"])}</span>')
    if not chips:
        return ""
    return f'<div class="econ-strip">{"".join(chips)}</div>'


def render_economy_chart(series):
    """Inline SVG multi-line chart of Budget / Personal Wealth / Economic Stability
    over the run (one x-step per checkpoint).  Handles negative values via a 0 line."""
    pts = [p for p in series if any(p[k] is not None for k in ("budget", "wealth", "stability"))]
    if len(pts) < 2:
        return '<div class="econ-empty">// no economy data recorded for this run</div>'

    W, H = 860, 348
    padL, padR, padT, padB = 46, 24, 24, 64   # padB leaves room for turn labels + legend row
    plotW = W - padL - padR
    plotH = H - padT - padB
    x0, y0 = padL, padT
    x1, y1 = padL + plotW, padT + plotH

    vals = [p[k] for p in pts for k in ("budget", "wealth", "stability") if p[k] is not None]
    vmin, vmax = min(vals), max(vals)
    if vmin == vmax:
        vmin -= 1
        vmax += 1
    pad = max(1, (vmax - vmin) * 0.08)
    vmin -= pad
    vmax += pad

    n = len(pts)
    def sx(i):
        return x0 + (plotW * i / (n - 1) if n > 1 else 0)
    def sy(v):
        return y1 - (v - vmin) / (vmax - vmin) * plotH

    grid = "#0c2a33"
    dim = "#155563"
    series_defs = [
        ("budget",    "Gov. Budget",         "#ffb000"),
        ("wealth",    "Personal Wealth",     "#36ffc2"),
        ("stability", "Economic Stability",  "#ff5fd2"),
    ]
    fm = "font-family=\"'Cascadia Mono','Consolas',monospace\""

    o = [f'<svg viewBox="0 0 {W} {H}" style="display:block;width:100%;height:auto" '
         f'xmlns="http://www.w3.org/2000/svg">']
    o.append(f'<rect x="{x0}" y="{y0}" width="{plotW}" height="{plotH}" '
             f'fill="#030b0f" stroke="{dim}" stroke-width="1"/>')

    # horizontal gridlines + y labels (~5 integer-ish ticks)
    span = vmax - vmin
    step = max(1, round(span / 5))
    tick = int(vmin // step) * step
    while tick <= vmax:
        if vmin <= tick <= vmax:
            gy = sy(tick)
            is_zero = (tick == 0)
            o.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" '
                     f'stroke="{"#3a1f60" if is_zero else grid}" '
                     f'stroke-width="{"1.5" if is_zero else "1"}"/>')
            o.append(f'<text x="{x0-8}" y="{gy+3:.1f}" text-anchor="end" fill="#1f8294" '
                     f'{fm} font-size="10">{tick}</text>')
        tick += step

    # vertical turn separators + turn labels where the turn increments
    last_turn = None
    for i, p in enumerate(pts):
        t = p["turn"]
        if t is not None and t != last_turn:
            gx = sx(i)
            o.append(f'<line x1="{gx:.1f}" y1="{y0}" x2="{gx:.1f}" y2="{y1}" '
                     f'stroke="{grid}" stroke-width="1" stroke-dasharray="2 4"/>')
            o.append(f'<text x="{gx:.1f}" y="{y1+14}" text-anchor="middle" fill="#1f8294" '
                     f'{fm} font-size="9">T{t:02d}</text>')
            last_turn = t

    # series polylines + end-dots
    for key, _, col in series_defs:
        seg = []
        for i, p in enumerate(pts):
            v = p[key]
            if v is None:
                continue
            seg.append(f"{sx(i):.1f},{sy(v):.1f}")
        if len(seg) >= 2:
            o.append(f'<polyline points="{" ".join(seg)}" fill="none" stroke="{col}" '
                     f'stroke-width="1.8" opacity="0.9"/>')
        if seg:
            ex, ey = seg[-1].split(",")
            o.append(f'<circle cx="{ex}" cy="{ey}" r="3" fill="{col}"/>')

    # legend (centered row beneath the plot, so it can't overflow the viewBox)
    items = []
    for key, name, col in series_defs:
        cur = next((p[key] for p in reversed(pts) if p[key] is not None), None)
        label = name + (f" ({cur})" if cur is not None else "")
        items.append((label, col))
    widths = [24 + len(lab) * 6 + 26 for lab, _ in items]   # swatch + text + trailing gap
    lx = (W - sum(widths)) / 2
    ly = y1 + 44
    for (lab, col), w in zip(items, widths):
        o.append(f'<line x1="{lx:.1f}" y1="{ly}" x2="{lx+18:.1f}" y2="{ly}" stroke="{col}" stroke-width="2.4"/>')
        o.append(f'<circle cx="{lx+9:.1f}" cy="{ly}" r="2.6" fill="{col}"/>')
        o.append(f'<text x="{lx+24:.1f}" y="{ly+3.5:.1f}" fill="{col}" {fm} font-size="10">{esc(lab)}</text>')
        lx += w

    o.append('</svg>')
    return f'<div class="econ-chart">{"".join(o)}</div>'


# ── static assets (no f-strings: keep literal { } intact) ──────────────────────────

CSS = r"""
@font-face{
  font-family:"Cascadia Mono";
  src:local("Cascadia Mono"),local("CascadiaMono"),
      url("https://cdn.jsdelivr.net/npm/@fontsource/cascadia-mono/files/cascadia-mono-latin-400-normal.woff2") format("woff2");
  font-weight:400;font-style:normal;font-display:swap;
}
:root{
  --bg:#010609; --bg2:#06121a; --fg:#33e9ff; --dim:#1f8294; --dimmer:#155563;
  --amber:#ffb000; --cyan:#36ffc2; --red:#ff5151; --pink:#ff5fd2; --lavender:#c0aaff;
  --line:#0c2a33; --panel:#040d11;
  --mono:"Cascadia Mono","Consolas","DejaVu Sans Mono","Courier New",monospace;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;background:var(--bg);color:var(--fg);font-family:var(--mono);
  font-size:14px;line-height:1.5;letter-spacing:.02em;
}
/* CRT scanline + vignette overlay (cheap; pointer-events off) */
body::before{
  content:"";position:fixed;inset:0;z-index:9999;pointer-events:none;
  background:repeating-linear-gradient(to bottom,rgba(0,0,0,0)0,rgba(0,0,0,0)2px,
    rgba(0,0,0,.22)3px,rgba(0,0,0,0)4px);
  mix-blend-mode:multiply;
}
body::after{
  content:"";position:fixed;inset:0;z-index:9998;pointer-events:none;
  background:radial-gradient(ellipse at center,rgba(0,0,0,0)55%,rgba(0,0,0,.55)100%);
}
a{color:var(--cyan);text-decoration:none}
a:hover{color:#fff;text-shadow:0 0 6px var(--cyan)}

header.top{
  position:sticky;top:0;z-index:50;background:linear-gradient(180deg,#02141a,#010609);
  border-bottom:1px solid var(--dim);padding:10px 16px;
  box-shadow:0 0 18px rgba(51,233,255,.15);
}
.title{font-weight:700;letter-spacing:.18em;color:var(--fg);
  text-shadow:0 0 8px rgba(51,233,255,.6);font-size:18px}
.title .blink{animation:blink 1.05s steps(1) infinite;color:var(--amber)}
@keyframes blink{50%{opacity:0}}
.subtitle{color:var(--dim);font-size:12px;letter-spacing:.12em;margin-top:2px}
.statbar{display:flex;flex-wrap:wrap;gap:14px;margin-top:8px;font-size:12px}
.statbar .s{color:var(--dim)}
.statbar .s b{color:var(--amber);font-weight:600}
.controls{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
#search{
  background:#02161c;border:1px solid var(--dim);color:var(--fg);font-family:var(--mono);
  font-size:13px;padding:6px 10px;width:min(420px,60vw);outline:none;
}
#search:focus{border-color:var(--fg);box-shadow:0 0 8px rgba(51,233,255,.4)}
#search::placeholder{color:var(--dimmer)}
.btn{
  background:#02161c;border:1px solid var(--dim);color:var(--fg);font-family:var(--mono);
  font-size:12px;padding:6px 10px;cursor:pointer;letter-spacing:.08em;
}
.btn:hover{border-color:var(--fg);background:#03252f}
.btn.active{background:#033540;border-color:var(--fg);color:#fff}
#count{color:var(--amber);font-size:12px;margin-left:4px}

.layout{display:grid;grid-template-columns:248px 1fr;gap:0;align-items:start;max-width:1400px;margin:0 auto}
nav.toc{
  position:sticky;top:118px;align-self:start;height:calc(100vh - 118px);overflow:auto;
  border-right:1px solid var(--line);padding:14px 10px 40px;background:var(--panel);
}
.toc-turn{margin-bottom:10px}
.toc-turn-h{display:block;color:var(--amber);font-weight:700;letter-spacing:.12em;
  font-size:12px;padding:3px 6px;border-bottom:1px dotted var(--dimmer)}
.toc-link{display:block;color:var(--dim);font-size:12px;padding:3px 6px 3px 16px;
  border-left:2px solid transparent;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toc-link:hover{color:var(--fg);background:#03212a}
.toc-link.active{color:#fff;border-left-color:var(--amber);background:#042830;
  text-shadow:0 0 6px var(--amber)}

main{padding:18px 26px 120px;max-width:1100px}

/* editable markdown panels (final analysis + human notes) */
.md-panel{border:1px solid var(--dimmer);background:#040d11;margin-bottom:22px}
.md-panel.accent-amber{border-color:var(--amber);background:#0a0700;box-shadow:0 0 16px rgba(255,176,0,.12)}
.md-panel.accent-cyan{border-color:var(--cyan);background:#02110f;box-shadow:0 0 16px rgba(54,255,194,.10)}
.md-panel h3{margin:0;padding:8px 12px;letter-spacing:.14em;font-size:13px;
  border-bottom:1px solid var(--dimmer);display:flex;align-items:center;gap:10px}
.md-panel.accent-amber h3{color:var(--amber);background:#120c00}
.md-panel.accent-cyan h3{color:var(--cyan);background:#04120f}
.md-toggle{margin-left:auto;background:#02161c;border:1px solid var(--dim);color:var(--fg);
  font-family:var(--mono);font-size:11px;letter-spacing:.1em;padding:3px 10px;cursor:pointer}
.md-toggle:hover{border-color:var(--fg);background:#03252f}
.md-view{padding:12px 16px;min-height:60px;font-size:13px;line-height:1.6;color:#d6f1f5}
.md-view .ph{color:#52666a;font-style:italic;white-space:pre-wrap}
.md-view h1,.md-view h2,.md-view h3,.md-view h4{color:var(--amber);letter-spacing:.05em;
  margin:.7em 0 .3em;line-height:1.3}
.md-view h1{font-size:18px}.md-view h2{font-size:16px}.md-view h3{font-size:14px}.md-view h4{font-size:13px}
.md-panel.accent-cyan .md-view h1,.md-panel.accent-cyan .md-view h2,
.md-panel.accent-cyan .md-view h3,.md-panel.accent-cyan .md-view h4{color:var(--cyan)}
.md-panel.accent-violet{border-color:var(--lavender);background:#070512;box-shadow:0 0 16px rgba(192,170,255,.10)}
.md-panel.accent-violet h3{color:var(--lavender);background:#070512}
.md-panel.accent-violet .md-view h1,.md-panel.accent-violet .md-view h2,
.md-panel.accent-violet .md-view h3,.md-panel.accent-violet .md-view h4{color:var(--lavender)}
.md-panel.accent-pink{border-color:var(--pink);background:#120007;box-shadow:0 0 16px rgba(255,95,210,.10)}
.md-panel.accent-pink h3{color:var(--pink);background:#120007}
.md-panel.accent-pink .md-view h1,.md-panel.accent-pink .md-view h2,
.md-panel.accent-pink .md-view h3,.md-panel.accent-pink .md-view h4{color:var(--pink)}
.md-panel.accent-red{border-color:var(--red);background:#120004;box-shadow:0 0 16px rgba(255,81,81,.10)}
.md-panel.accent-red h3{color:var(--red);background:#120004}
.md-panel.accent-red .md-view h1,.md-panel.accent-red .md-view h2,
.md-panel.accent-red .md-view h3,.md-panel.accent-red .md-view h4{color:var(--red)}
.md-view strong{color:#fff;font-weight:700}
.md-view em{color:#ffe0a0;font-style:italic}
.md-view code{background:#02161c;border:1px solid var(--line);padding:0 4px;color:var(--cyan)}
.md-view a{color:var(--cyan)}
.md-view ul,.md-view ol{margin:.3em 0 .6em;padding-left:22px}
.md-view li{margin:2px 0}
.md-view p{margin:.4em 0}
.md-view hr{border:none;border-top:1px dashed var(--dimmer);margin:.8em 0}
.md-view table{border-collapse:collapse;margin:.4em 0;font-size:13px}
.md-view th,.md-view td{border:1px solid var(--dimmer);padding:4px 12px;text-align:left;vertical-align:top}
.md-view th{color:var(--amber);background:#120c00}
.md-edit{display:none;width:100%;min-height:170px;resize:vertical;background:#02141a;
  border:none;border-top:1px solid var(--dimmer);color:#d6f1f5;font-family:var(--mono);
  font-size:13px;line-height:1.55;padding:12px 16px;outline:none}
.md-panel.editing .md-view{display:none}
.md-panel.editing .md-edit{display:block}
.md-hint{padding:4px 16px 10px;color:#52666a;font-size:11px;letter-spacing:.06em}
.md-collapse{margin-right:6px;background:#02161c;border:1px solid var(--dim);color:var(--fg);
  font-family:var(--mono);font-size:11px;letter-spacing:.1em;padding:3px 10px;cursor:pointer}
.md-collapse:hover{border-color:var(--fg);background:#03252f}
.md-panel.collapsed .md-view,
.md-panel.collapsed .md-edit,
.md-panel.collapsed .md-hint{display:none}
.md-panel.plain-text .md-view{white-space:pre-wrap}
.postgame-header{margin:50px 0 20px;padding:10px 0 8px;border-top:2px solid var(--amber);
  color:var(--amber);font-size:13px;letter-spacing:.22em;font-weight:700;
  text-shadow:0 0 10px rgba(255,176,0,.35)}

/* turn + section headers */
.turn-marker{margin:30px 0 6px;padding:6px 0;border-top:1px solid var(--dim);
  color:var(--fg);letter-spacing:.2em;font-size:20px;scroll-margin-top:130px}
.turn-marker .th-l{color:var(--dim);font-size:13px}
.turn-marker .th-n{color:var(--amber);text-shadow:0 0 10px rgba(255,176,0,.5)}
.section{scroll-margin-top:130px;margin:0 0 4px}
.sec-head{position:sticky;top:112px;z-index:5;background:linear-gradient(180deg,#03141a,#010609);
  margin:14px 0 8px;padding:5px 8px;color:var(--cyan);font-size:13px;letter-spacing:.1em;
  border-left:3px solid var(--cyan);display:flex;align-items:center;gap:8px}
.sec-head .sec-dot{color:var(--cyan);font-size:10px}
.sec-head .sec-meta{margin-left:auto;color:var(--dim);font-size:11px}
.sec-body{border-left:1px solid var(--line);margin-left:6px;padding-left:14px}

/* transcript lines */
.line{padding:3px 0;color:var(--fg)}
.line .who{color:var(--cyan);font-weight:600;margin-right:8px}
.line .txt{white-space:pre-wrap}
.line.spk-narrator{color:#8fd6df}
.line.spk-narrator .txt{color:#7fc6cf;font-style:normal}
.line.auto .txt{color:#5fa6ae;font-style:italic}
.line.spk-player .who{color:var(--amber);text-shadow:0 0 6px rgba(255,176,0,.4)}
.line.spk-player .txt{color:#ffe0a0}
.line.italic .txt{font-style:italic;color:#d8b878}
.line.spk-char .who{color:var(--lavender)}
.line.spk-char .txt{color:#d0e8ec}

/* decision cards (timeline node + reasoning rail) */
.decision{display:flex;gap:14px;align-items:flex-start;margin:10px 0;
  border:1px solid var(--line);background:#03141a;padding:8px 10px;position:relative;
  scroll-margin-top:130px}
.decision::before{content:"";position:absolute;left:-15px;top:14px;width:8px;height:8px;
  background:var(--amber);border-radius:50%;box-shadow:0 0 8px var(--amber)}
.dec-main{flex:1;min-width:0}
.dec-head{display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap}
.badge{font-size:10px;letter-spacing:.14em;padding:2px 7px;border:1px solid currentColor}
.dt-dialogue .badge{color:var(--cyan)}
.dt-bill .badge{color:var(--red)}
.dt-paged .badge{color:var(--amber)}
.dt-event .badge{color:var(--pink)}
.dt-decree .badge{color:#9b87ff}
.dec-head .who{color:var(--amber);font-weight:600}
.dec-head .t{color:var(--dim);font-size:11px}
.dec-head .tok{color:var(--dimmer);font-size:11px}
.opts{list-style:none;margin:0;padding:0}
.opts .opt{display:flex;gap:8px;padding:2px 0;color:var(--dimmer)}
.opts .opt .mk{color:var(--dimmer);width:12px;flex:none}
.opts .opt.chosen{color:#ffe0a0}
.opts .opt.chosen .mk{color:var(--amber)}
.opts .opt.chosen .ot{text-shadow:0 0 6px rgba(255,176,0,.25)}

.dec-side{width:330px;flex:none}
@media(max-width:1000px){.decision{flex-direction:column}.dec-side{width:100%}
  nav.toc{display:none}.layout{grid-template-columns:1fr}}
.r-toggle{background:#0a1c20;border:1px solid var(--dim);color:var(--cyan);
  font-family:var(--mono);font-size:11px;letter-spacing:.1em;padding:4px 8px;cursor:pointer;width:100%}
.r-toggle:hover:not([disabled]){border-color:var(--cyan);color:#fff}
.r-toggle[disabled]{color:var(--dim);cursor:default;opacity:.7}
.reasoning{display:none;margin-top:6px;padding:8px 10px;background:#02161c;
  border:1px solid var(--dimmer);border-left:2px solid var(--cyan);color:#a8e0e8;
  font-size:12px;line-height:1.55;white-space:pre-wrap}
.reasoning.human,.reasoning.empty{color:var(--dim);font-style:italic}
.decision.open .reasoning,body.show-reasoning .reasoning{display:block}
body.show-reasoning .r-toggle:not([disabled]){border-color:var(--cyan)}

/* per-section economy strip (state at the scene boundary) */
.econ-strip{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 8px 6px;padding-left:14px}
.ec-chip{font-size:11px;letter-spacing:.08em;padding:2px 9px;border:1px solid currentColor;
  color:var(--dim);background:#03141a;display:inline-flex;gap:6px;align-items:center}
.ec-chip b{font-weight:700;font-size:12px}
.ec-budget{color:var(--amber)}
.ec-wealth{color:var(--cyan)}
.ec-stab{color:var(--pink)}
.ec-label{letter-spacing:.12em}
.ec-stable{color:var(--cyan)}
.ec-unstable{color:var(--amber)}
.ec-danger{color:var(--red)}

/* whole-run economy chart */
.econ-chart{padding:8px 14px 6px}
.econ-empty{padding:14px 16px;color:#52666a;font-style:italic}

.entry.hidden{display:none}
.section.hidden,.turn-marker.hidden{display:none}
mark{background:var(--amber);color:#000;padding:0 1px}
footer{color:var(--dimmer);font-size:11px;text-align:center;padding:24px;
  border-top:1px solid var(--line)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:#010609}
::-webkit-scrollbar-thumb{background:#0c3a40;border:1px solid #061a20}
.align-plot{padding:14px 16px 8px;display:flex;justify-content:center}
"""

JS = r"""
(function(){
  var body=document.body;
  var search=document.getElementById('search');
  var count=document.getElementById('count');
  var entries=Array.prototype.slice.call(document.querySelectorAll('.entry'));

  // pre-store original txt html for highlight restore
  entries.forEach(function(e){
    var tx=e.querySelector('.txt');
    if(tx) e.setAttribute('data-orig', tx.innerHTML);
  });

  // ── expandable reasoning ──
  document.querySelectorAll('.r-toggle').forEach(function(btn){
    if(btn.disabled) return;
    btn.addEventListener('click',function(){
      btn.closest('.decision').classList.toggle('open');
    });
  });
  var allBtn=document.getElementById('toggle-reasoning');
  allBtn.addEventListener('click',function(){
    body.classList.toggle('show-reasoning');
    allBtn.classList.toggle('active',body.classList.contains('show-reasoning'));
  });

  // ── search ──
  var HILITE_MAX=800;
  function clearHi(){
    entries.forEach(function(e){
      var tx=e.querySelector('.txt');
      if(tx && e.getAttribute('data-orig')!==tx.innerHTML) tx.innerHTML=e.getAttribute('data-orig');
    });
  }
  function esc(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}
  function run(){
    var q=search.value.trim().toLowerCase();
    clearHi();
    if(!q){
      // Scroll anchor: find the first visible entry in the viewport so we can
      // restore its screen position after all the hidden entries reappear.
      var anchor=null, anchorTop=0;
      for(var i=0;i<entries.length;i++){
        if(!entries[i].classList.contains('hidden')){
          var r=entries[i].getBoundingClientRect();
          if(r.bottom>150){anchor=entries[i];anchorTop=r.top;break;}
        }
      }
      entries.forEach(function(e){e.classList.remove('hidden');});
      document.querySelectorAll('.section,.turn-marker').forEach(function(s){s.classList.remove('hidden');});
      count.textContent='';
      if(anchor){window.scrollBy(0,anchor.getBoundingClientRect().top-anchorTop);}
      return;
    }
    var n=0;
    entries.forEach(function(e){
      var hit=(e.getAttribute('data-s')||'').indexOf(q)>=0;
      e.classList.toggle('hidden',!hit);
      if(hit) n++;
    });
    // hide empty sections / turn markers
    document.querySelectorAll('.section').forEach(function(s){
      var any=s.querySelector('.entry:not(.hidden)');
      s.classList.toggle('hidden',!any);
    });
    document.querySelectorAll('.turn-marker').forEach(function(tm){
      var t=tm.id.replace('turn-','');
      var any=document.querySelector('.section[data-turn="'+t+'"]:not(.hidden)');
      tm.classList.toggle('hidden',!any);
    });
    count.textContent=n+' match'+(n===1?'':'es');
    // highlight only when result set is small
    if(n>0 && n<=HILITE_MAX){
      var re=new RegExp(esc(q),'ig');
      entries.forEach(function(e){
        if(e.classList.contains('hidden')) return;
        var tx=e.querySelector('.txt'); if(!tx) return;
        var orig=e.getAttribute('data-orig');
        tx.innerHTML=orig.replace(re,function(m){return '<mark>'+m+'</mark>';});
      });
    }
  }
  var deb;
  search.addEventListener('input',function(){clearTimeout(deb);deb=setTimeout(run,140);});
  search.addEventListener('keydown',function(ev){if(ev.key==='Escape'){search.value='';run();}});

  // ── active section in TOC via IntersectionObserver ──
  var links={};
  var tocNav=document.querySelector('nav.toc');
  document.querySelectorAll('.toc-link').forEach(function(a){links[a.getAttribute('data-target')]=a;});
  var io=new IntersectionObserver(function(ents){
    ents.forEach(function(en){
      if(en.isIntersecting){
        var a=links[en.target.id];
        if(a){
          Object.keys(links).forEach(function(k){links[k].classList.remove('active');});
          a.classList.add('active');
          // Scroll the nav directly — scrollIntoView() walks all ancestors and can
          // hijack an in-progress window smooth-scroll when it reaches the window.
          var aTop=a.getBoundingClientRect().top-tocNav.getBoundingClientRect().top+tocNav.scrollTop;
          var aBot=aTop+a.offsetHeight;
          if(aTop<tocNav.scrollTop+8) tocNav.scrollTop=aTop-8;
          else if(aBot>tocNav.scrollTop+tocNav.clientHeight-8) tocNav.scrollTop=aBot-tocNav.clientHeight+8;
        }
      }
    });
  },{rootMargin:'-120px 0px -70% 0px'});
  document.querySelectorAll('.section').forEach(function(s){io.observe(s);});

  // ── minimal markdown renderer (headings, bold/italic/code, lists, links, hr) ──
  function mdEsc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function mdInline(s){
    s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
    s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
    s=s.replace(/__([^_]+)__/g,'<strong>$1</strong>');
    s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
    s=s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
    return s;
  }
  function md(src){
    var lines=src.replace(/\r\n/g,'\n').split('\n'),out=[],para=[],list=null;
    function fp(){if(para.length){out.push('<p>'+mdInline(para.join(' '))+'</p>');para=[];}}
    function cl(){if(list){out.push('</'+list+'>');list=null;}}
    for(var i=0;i<lines.length;i++){
      var ln=lines[i];
      if(/^\s*$/.test(ln)){fp();cl();continue;}
      var h=/^(#{1,6})\s+(.*)$/.exec(ln);
      if(h){fp();cl();var l=h[1].length;out.push('<h'+l+'>'+mdInline(mdEsc(h[2]))+'</h'+l+'>');continue;}
      if(/^\s*([-*+])\s+/.test(ln)){fp();if(list!=='ul'){cl();out.push('<ul>');list='ul';}
        out.push('<li>'+mdInline(mdEsc(ln.replace(/^\s*[-*+]\s+/,'')))+'</li>');continue;}
      if(/^\s*([-*_] *){3,}$/.test(ln)){fp();cl();out.push('<hr>');continue;}
      if(/^\s*\|/.test(ln)){
        fp();cl();
        var rows=[],sepIdx=-1;
        while(i<lines.length && /^\s*\|/.test(lines[i])){
          var row=lines[i].replace(/^\s*\||\|\s*$/g,'').split('|').map(function(c){return c.trim();});
          if(row.every(function(c){return /^:?-+:?$/.test(c);})){sepIdx=rows.length;}
          else{rows.push(row);}
          i++;
        }
        i--;
        out.push('<table>');
        for(var ri=0;ri<rows.length;ri++){
          var tag=sepIdx>=0&&ri<sepIdx?'th':'td';
          out.push('<tr>');
          rows[ri].forEach(function(cell){out.push('<'+tag+'>'+mdInline(mdEsc(cell))+'</'+tag+'>');});
          out.push('</tr>');
        }
        out.push('</table>');
        continue;
      }
      para.push(mdEsc(ln));
    }
    fp();cl();return out.join('');
  }

  // ── editable markdown panel (analysis + human notes) ──
  function setupPanel(id){
    var wrap=document.getElementById(id); if(!wrap) return;
    var view=wrap.querySelector('.md-view'),ta=wrap.querySelector('.md-edit'),btn=wrap.querySelector('.md-toggle');
    var key='sp_'+id+'_'+(window.RUN_META&&RUN_META.run_id||'run');
    var saved=null; try{saved=localStorage.getItem(key);}catch(e){}
    if(saved!==null) ta.value=saved;
    function render(){
      view.innerHTML = ta.value.trim() ? md(ta.value)
        : '<span class="ph">'+(view.getAttribute('data-ph')||'')+'</span>';
    }
    render();
    btn.addEventListener('click',function(){
      if(wrap.classList.contains('editing')){
        try{localStorage.setItem(key,ta.value);}catch(e){}
        render(); wrap.classList.remove('editing'); btn.textContent='EDIT';
      }else{
        wrap.classList.add('editing'); btn.textContent='SAVE'; ta.focus();
      }
    });
  }
  function setupPlainPanel(id){
    var wrap=document.getElementById(id); if(!wrap) return;
    var view=wrap.querySelector('.md-view'),ta=wrap.querySelector('.md-edit'),btn=wrap.querySelector('.md-toggle');
    var key='sp_'+id+'_'+(window.RUN_META&&RUN_META.run_id||'run');
    var saved=null; try{saved=localStorage.getItem(key);}catch(e){}
    if(saved!==null) ta.value=saved;
    function render(){
      if(ta.value.trim()){view.textContent=ta.value;}
      else{view.innerHTML='<span class="ph">'+(view.getAttribute('data-ph')||'')+'</span>';}
    }
    render();
    btn.addEventListener('click',function(){
      if(wrap.classList.contains('editing')){
        try{localStorage.setItem(key,ta.value);}catch(e){}
        render(); wrap.classList.remove('editing'); btn.textContent='EDIT';
      }else{
        wrap.classList.add('editing'); btn.textContent='SAVE'; ta.focus();
      }
    });
  }
  function setupCollapse(id){
    var wrap=document.getElementById(id); if(!wrap) return;
    var btn=wrap.querySelector('.md-collapse'); if(!btn) return;
    btn.addEventListener('click',function(){
      var col=wrap.classList.toggle('collapsed');
      btn.textContent=col?'[+] EXPAND':'[−] COLLAPSE';
    });
  }
  setupPanel('analysis');
  setupPanel('notes');
  setupPanel('sysprompt');
  setupPanel('end1');
  setupPanel('end2');
  setupPlainPanel('memory');
  setupCollapse('memory');
  setupPanel('manifesto');
  setupCollapse('manifesto');
})();
"""


def render_align_plot(ax, ay, label, has_dot):
    """Inline SVG 2D ideological alignment compass for the Suzerain end-screen.

    ax ∈ [-1, 1]  -1 = Malenyevism (left),       +1 = Arcasian Capitalism (right)
    ay ∈ [-1, 1]  -1 = Sordish Reformism (bottom), +1 = Sollism (top)
    """
    W, H = 400, 360
    cx, cy = 200, 185   # centre of plot area
    r = 110             # half-size (plot spans cx±r, cy±r)
    x0, y0 = cx - r, cy - r    # top-left  (90, 75)
    x1, y1 = cx + r, cy + r    # bot-right (310, 295)

    lav    = "#c0aaff"
    dim    = "#180d2a"
    axcol  = "#3a1f60"
    borcol = "#5a2a90"
    amber  = "#ffb000"
    fm     = "font-family=\"'Cascadia Mono','Consolas',monospace\""

    o = [f'<svg viewBox="0 0 {W} {H}" style="display:block;margin:0 auto;'
         f'max-width:{W}px;width:100%" xmlns="http://www.w3.org/2000/svg">']

    # Plot background
    o.append(f'<rect x="{x0}" y="{y0}" width="{2*r}" height="{2*r}" '
             f'fill="#030209" stroke="{borcol}" stroke-width="1"/>')

    # Grid lines at ±0.5 (quarters) and 0 (axes)
    for i in (1, 2, 3):
        gx = x0 + (2 * r * i) // 4
        gy = y0 + (2 * r * i) // 4
        col = axcol if i == 2 else dim
        sw  = "1.5" if i == 2 else "1"
        o.append(f'<line x1="{gx}" y1="{y0}" x2="{gx}" y2="{y1}" stroke="{col}" stroke-width="{sw}"/>')
        o.append(f'<line x1="{x0}" y1="{gy}" x2="{x1}" y2="{gy}" stroke="{col}" stroke-width="{sw}"/>')

    # Axis labels
    o.append(f'<text x="{cx}" y="{y0 - 8}" text-anchor="middle" fill="{lav}" {fm} '
             f'font-size="10" letter-spacing="3">SOLLISM</text>')
    o.append(f'<text x="{cx}" y="{y1 + 18}" text-anchor="middle" fill="{lav}" {fm} '
             f'font-size="10" letter-spacing="2">SORDISH REFORMISM</text>')
    o.append(f'<text x="{x0 - 14}" y="{cy}" text-anchor="middle" fill="{lav}" {fm} '
             f'font-size="10" letter-spacing="2" '
             f'transform="rotate(-90,{x0-14},{cy})">MALENYEVISM</text>')
    o.append(f'<text x="{x1 + 14}" y="{cy}" text-anchor="middle" fill="{lav}" {fm} '
             f'font-size="10" letter-spacing="1" '
             f'transform="rotate(90,{x1+14},{cy})">ARCASIAN CAPITALISM</text>')

    if has_dot:
        ax = max(-1.0, min(1.0, float(ax)))
        ay = max(-1.0, min(1.0, float(ay)))
        dx = cx + ax * r
        dy = cy - ay * r    # SVG y is inverted relative to the ideological axis

        # Glow rings
        o.append(f'<circle cx="{dx:.1f}" cy="{dy:.1f}" r="16" fill="{amber}" opacity="0.10"/>')
        o.append(f'<circle cx="{dx:.1f}" cy="{dy:.1f}" r="9"  fill="{amber}" opacity="0.28"/>')
        o.append(f'<circle cx="{dx:.1f}" cy="{dy:.1f}" r="4.5" fill="{amber}"/>')

        # Label tag — flip to the left side when the dot is in the right portion
        lw = len(label) * 7 + 14
        lh = 16
        tx = (dx - 10 - lw) if ax > 0.55 else (dx + 10)
        ry = dy - 24    # rect top
        ty = dy - 11    # text baseline

        o.append(f'<rect x="{tx:.1f}" y="{ry:.1f}" width="{lw}" height="{lh}" '
                 f'fill="#0a0518" stroke="{amber}" stroke-width="1"/>')
        o.append(f'<text x="{tx + lw/2:.1f}" y="{ty:.1f}" text-anchor="middle" '
                 f'fill="{amber}" {fm} font-size="10" font-weight="bold" letter-spacing="1">'
                 f'{esc(label)}</text>')

    o.append('</svg>')
    return f'<div class="align-plot">{"".join(o)}</div>'


def main():
    ap = argparse.ArgumentParser(description="Export a Shadow President run log to retro HTML.")
    ap.add_argument("path", nargs="?", help="run .jsonl (defaults to logs/last_run.json)")
    ap.add_argument("--name", help="operator name replacing the 'Player' speaker")
    ap.add_argument("--out", help="output .html path")
    ap.add_argument("--analysis", help="text/markdown file to prefill the analysis box")
    ap.add_argument("--notes", help="text/markdown file to prefill the human-notes box")
    ap.add_argument("--sysprompt", help="text file to prefill the system prompt box")
    ap.add_argument("--end1", help="markdown file for the first end-of-game summary panel")
    ap.add_argument("--end2", help="markdown file for the second end-of-game summary panel")
    ap.add_argument("--memory", help="text file containing the AI memory log for this run")
    ap.add_argument("--manifesto", help="text/markdown file for the manifesto panel "
                    "(defaults to the final manifesto entry in the log)")
    ap.add_argument("--align-x", type=float, metavar="X",
                    help="ideological X position [-1 Malenyevism … +1 Arcasian Capitalism]")
    ap.add_argument("--align-y", type=float, metavar="Y",
                    help="ideological Y position [-1 Sordish Reformism … +1 Sollism]")
    ap.add_argument("--align-label", metavar="LABEL",
                    help="dot label (default: model name from filename)")
    args = ap.parse_args()

    path = resolve_input(args.path)
    if not os.path.exists(path):
        sys.exit(f"Input not found: {path}")

    entries = load_entries(path)
    if not entries:
        sys.exit("No entries parsed from log.")

    model = model_from_filename(path)
    player_name = args.name or prettify_model(model)
    stats = compute_stats(entries)
    toc_html, body_html, n_sections = build(entries, player_name)

    def read_prefill(p):
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return esc(f.read())   # textarea content: entities are decoded back by the parser
        return ""

    analysis_prefill = read_prefill(args.analysis)
    notes_prefill = read_prefill(args.notes)
    sysprompt_prefill = read_prefill(args.sysprompt)
    end1_prefill = read_prefill(args.end1)
    end2_prefill = read_prefill(args.end2)
    memory_prefill = read_prefill(args.memory)
    # Manifesto: explicit file wins, else the final manifesto entry from the log.
    manifesto_prefill = read_prefill(args.manifesto) or esc(latest_manifesto(entries))

    economy_series = compute_economy_series(entries)
    economy_chart_html = render_economy_chart(economy_series)

    has_dot = args.align_x is not None and args.align_y is not None
    align_label = args.align_label or player_name
    align_plot_html = render_align_plot(
        args.align_x or 0.0, args.align_y or 0.0, align_label, has_dot
    )

    run_date = ""
    m = re.search(r"(\d{8})_(\d{6})", os.path.basename(path))
    if m:
        try:
            run_date = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass

    dtype_str = " · ".join(f"{v} {k}" for k, v in sorted(stats["dtypes"].items(), key=lambda x: -x[1]))
    run_meta = json.dumps({"run_id": stats["run_id"]})

    statbar = (
        f'<span class="s">OPERATOR <b>{esc(player_name)}</b></span>'
        f'<span class="s">MODEL <b>{esc(model)}</b></span>'
        f'<span class="s">TURNS <b>{stats["turns"]}</b></span>'
        f'<span class="s">DECISIONS <b>{stats["decisions"]}</b></span>'
        f'<span class="s">CHECKPOINTS <b>{stats["checkpoints"]}</b></span>'
        f'<span class="s">COMPLETION TOK <b>{stats["completion"]:,}</b></span>'
    )
    if stats["duration"]:
        statbar += f'<span class="s">RUNTIME <b>{esc(stats["duration"])}</b></span>'
    if run_date:
        statbar += f'<span class="s">DATE <b>{esc(run_date)}</b></span>'

    an_ph = ("// Paste the AI's final self-analysis of this run here. Markdown is supported "
             "(headings, **bold**, lists, etc.). Click EDIT to write; saved in this browser. "
             "To hard-code, search this file for PASTE-FINAL-ANALYSIS.")
    notes_ph = ("// Your own notes on this run: manual interventions, observations, anomalies. "
                "Markdown supported. e.g. intervened once to sign 3 decrees. "
                "Click EDIT to write; saved in this browser. Marker: HUMAN-NOTES.")
    sysprompt_ph = ("// Paste the system prompt used for this run. "
                    "Useful for comparing AI behaviour across different prompts. "
                    "Provide via --sysprompt or click EDIT. Saved to this browser.")
    end1_ph = ("// First end-of-game summary panel. Paste or provide via --end1. "
               "Markdown supported. Saved to this browser.")
    end2_ph = ("// Second end-of-game summary panel. Paste or provide via --end2. "
               "Markdown supported. Saved to this browser.")
    memory_ph = ("// AI memory at the end of this run. Paste the contents of memory.txt "
                 "or provide via --memory. Useful for comparing what the AI retained across runs.")
    manifesto_ph = ("// The AI's living strategy manifesto. Defaults to the final version "
                    "recorded in the log; override with --manifesto. Markdown supported.")

    html_out = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>SHADOW PRESIDENT :: RUN {esc(stats['run_id'])}</title>"
f"<style>{CSS}</style></head><body>"
        f"<script>window.RUN_META={run_meta};</script>"
        # header
        "<header class=\"top\">"
        "<div class=\"title\">SHADOW&nbsp;PRESIDENT&nbsp;//&nbsp;RUN&nbsp;LOG"
        "<span class=\"blink\">_</span></div>"
        f"<div class=\"subtitle\">AUTONOMOUS PLAYTHROUGH ARCHIVE &nbsp;·&nbsp; {esc(dtype_str)}</div>"
        f"<div class=\"statbar\">{statbar}</div>"
        "<div class=\"controls\">"
        "<input id=\"search\" type=\"text\" placeholder=\"&gt; search transcript &amp; reasoning… (Esc clears)\" autocomplete=\"off\">"
        "<span id=\"count\"></span>"
        "<button id=\"toggle-reasoning\" class=\"btn\">REASONING: ALL</button>"
        "</div></header>"
        # layout
        "<div class=\"layout\">"
        f"<nav class=\"toc\">{toc_html}</nav>"
        "<main>"
        # PASTE-FINAL-ANALYSIS
        "<div id=\"analysis\" class=\"md-panel accent-amber\">"
        "<h3>&gt;&gt; FINAL ANALYSIS<button class=\"md-toggle\">EDIT</button></h3>"
        f"<div class=\"md-view\" data-ph=\"{esc(an_ph)}\"></div>"
        f"<textarea class=\"md-edit\" spellcheck=\"false\">{analysis_prefill}</textarea>"
        "<div class=\"md-hint\">markdown · editable · saved to this browser · set via --analysis</div>"
        "</div>"
        # HUMAN-NOTES
        "<div id=\"notes\" class=\"md-panel accent-cyan\">"
        "<h3>&gt;&gt; HUMAN INTERVENTIONS / NOTES<button class=\"md-toggle\">EDIT</button></h3>"
        f"<div class=\"md-view\" data-ph=\"{esc(notes_ph)}\"></div>"
        f"<textarea class=\"md-edit\" spellcheck=\"false\">{notes_prefill}</textarea>"
        "<div class=\"md-hint\">markdown · editable · saved to this browser · set via --notes</div>"
        "</div>"
        # SYSTEM PROMPT
        "<div id=\"sysprompt\" class=\"md-panel accent-red\">"
        "<h3>&gt;&gt; SYSTEM PROMPT<button class=\"md-toggle\">EDIT</button></h3>"
        f"<div class=\"md-view\" data-ph=\"{esc(sysprompt_ph)}\"></div>"
        f"<textarea class=\"md-edit\" spellcheck=\"false\">{sysprompt_prefill}</textarea>"
        "<div class=\"md-hint\">markdown · editable · saved to this browser · set via --sysprompt</div>"
        "</div>"
        # ECONOMY TRAJECTORY chart (whole-run overview)
        "<div id=\"economy\" class=\"md-panel accent-cyan\">"
        "<h3>&gt;&gt; ECONOMY TRAJECTORY</h3>"
        f"{economy_chart_html}"
        "<div class=\"md-hint\">government budget · personal wealth · economic stability, per checkpoint</div>"
        "</div>"
        f"{body_html}"
        # POST-GAME section header + end panels
        "<h2 class=\"postgame-header\">■■ POST-GAME ■■</h2>"
        "<div id=\"end1\" class=\"md-panel accent-violet\">"
        "<h3>&gt;&gt; END SUMMARY — PANEL I<button class=\"md-toggle\">EDIT</button></h3>"
        f"{align_plot_html}"
        f"<div class=\"md-view\" data-ph=\"{esc(end1_ph)}\"></div>"
        f"<textarea class=\"md-edit\" spellcheck=\"false\">{end1_prefill}</textarea>"
        "<div class=\"md-hint\">markdown · editable · saved to this browser · set via --end1</div>"
        "</div>"
        "<div id=\"end2\" class=\"md-panel accent-pink\">"
        "<h3>&gt;&gt; END SUMMARY — PANEL II<button class=\"md-toggle\">EDIT</button></h3>"
        f"<div class=\"md-view\" data-ph=\"{esc(end2_ph)}\"></div>"
        f"<textarea class=\"md-edit\" spellcheck=\"false\">{end2_prefill}</textarea>"
        "<div class=\"md-hint\">markdown · editable · saved to this browser · set via --end2</div>"
        "</div>"
        "<div id=\"memory\" class=\"md-panel plain-text collapsed\">"
        "<h3>&gt;&gt; AI MEMORY"
        "<button class=\"md-collapse\">[+] EXPAND</button>"
        "<button class=\"md-toggle\">EDIT</button></h3>"
        f"<div class=\"md-view\" data-ph=\"{esc(memory_ph)}\"></div>"
        f"<textarea class=\"md-edit\" spellcheck=\"false\">{memory_prefill}</textarea>"
        "<div class=\"md-hint\">plain text · editable · saved to this browser · set via --memory</div>"
        "</div>"
        # MANIFESTO — one collapsed panel, final version (markdown)
        "<div id=\"manifesto\" class=\"md-panel accent-violet collapsed\">"
        "<h3>&gt;&gt; MANIFESTO"
        "<button class=\"md-collapse\">[+] EXPAND</button>"
        "<button class=\"md-toggle\">EDIT</button></h3>"
        f"<div class=\"md-view\" data-ph=\"{esc(manifesto_ph)}\"></div>"
        f"<textarea class=\"md-edit\" spellcheck=\"false\">{manifesto_prefill}</textarea>"
        "<div class=\"md-hint\">markdown · editable · saved to this browser · set via --manifesto</div>"
        "</div>"
        "</main></div>"
        f"<footer>SHADOW PRESIDENT run log · {esc(stats['run_id'])} · "
        f"{len(entries):,} entries · {n_sections} sections · generated by export.py</footer>"
        f"<script>{JS}</script>"
        "</body></html>"
    )

    out = args.out or os.path.splitext(path)[0] + ".html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"Wrote {out}  ({len(html_out):,} bytes, {len(entries):,} entries, {n_sections} sections)")


if __name__ == "__main__":
    main()

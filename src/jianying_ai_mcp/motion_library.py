"""Reusable motion-graphics recipes rendered through the built-in OpenFilm runtime.

Each recipe is a deterministic HTML page driven by ``window.film.frame(t)``, so a
clip can be rendered at any frame rate and checked frame by frame. Recipes take
plain data (titles, items, numbers) and share one design-token vocabulary, which
keeps a series of episodes visually consistent.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .managed_fonts import FONT_REVISION, font_path
from .openfilm import render_openfilm
from .production_graphics import ProductionError
from .production_styles import StylePreset, get_production_style
from .video_animation import create_video_animation


MAX_ITEMS = 5
FONT_SANS = '"Noto Sans SC","Microsoft YaHei","PingFang SC",system-ui,sans-serif'
FONT_SERIF = '"Noto Serif SC","Songti SC","SimSun",Georgia,serif'
MOTION_CACHE_VERSION = 1


@dataclass(frozen=True)
class Tokens:
    background: str
    panel: str
    foreground: str
    muted: str
    accent: str
    accent_text: str


@dataclass(frozen=True)
class Recipe:
    name: str
    label: str
    summary: str
    data_fields: tuple[str, ...]
    default_duration: float
    render: Callable[[dict[str, Any], Tokens, float], tuple[str, str, str]]
    # opener / transition / recap / emphasis / closer. Recap cards restate points the
    # speaker makes; as full-screen beats they belong in the summary section only.
    role: str = "emphasis"


RECIPES: dict[str, Recipe] = {}
ROLE_GUIDE = {
    "opener": "片头定调，全屏使用。",
    "transition": "小节转场只放编号和标题，不复述内容。",
    "recap": "复述要点。小节中请放进口播分屏随讲随出（layout.with.graphic，items[].at 对齐口播）；全屏只用于总结段。",
    "emphasis": "强调一个数字、金句或关键词；可全屏短停，也可放进分屏。",
    "closer": "片尾署名与行动号召，全屏使用。",
}


def _recipe(**kwargs: Any) -> Callable[[Callable[..., tuple[str, str, str]]], Callable[..., tuple[str, str, str]]]:
    def register(function: Callable[..., tuple[str, str, str]]) -> Callable[..., tuple[str, str, str]]:
        RECIPES[kwargs["name"]] = Recipe(**kwargs, render=function)
        return function
    return register


# ---------------------------------------------------------------- helpers


def _esc(value: Any, limit: int = 120) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > limit:
        raise ProductionError(f"文字最多 {limit} 个字符：{text[:20]}…")
    return html.escape(text)


def _lines(value: Any, limit: int = 60) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [_esc(part, limit) for part in text.splitlines() if part.strip()]


def _fit(lines: list[str], max_vh: float, width_ratio: float = 0.81) -> str:
    """Largest font size that keeps every CJK line inside the text column."""
    longest = max((len(line) for line in lines), default=1)
    return f"min({max_vh}vh,calc({width_ratio * 100:.0f}vw / {longest}))"


def _cssf(template: str, fit: str) -> str:
    """Fill the ``__FIT__`` placeholder without turning whole stylesheets into f-strings."""
    return template.replace("__FIT__", fit)


def _items(data: dict[str, Any], label: str = "items") -> list[dict[str, Any]]:
    raw = data.get(label) or []
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise ProductionError(f"{label} 需要 0–{MAX_ITEMS} 项列表")
    items = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            items.append({"title": _esc(item, 40), "detail": "", "at": None})
        elif isinstance(item, dict):
            at = item.get("at")
            if at is not None and (type(at) not in (int, float) or not 0 <= at <= 60):
                raise ProductionError(f"{label} 第 {index} 项的 at 需为 0–60 秒")
            items.append({
                "title": _esc(item.get("title") or item.get("label"), 40),
                "detail": _esc(item.get("detail") or item.get("value"), 60),
                "at": None if at is None else float(at),
            })
        else:
            raise ProductionError(f"{label} 第 {index} 项需要字符串或对象")
    return items


def motion_item_starts(recipe: str, items: list[str | dict[str, Any]]) -> list[float]:
    """Use the same local cue times for the HTML renderer and its review points."""
    if recipe == "list":
        first, gap = 0.12, 0.46 / max(1, len(items))
    elif recipe == "steps":
        first, gap = 0.1, 0.55
    elif recipe == "checklist":
        first, gap = 0.12, 0.5
    else:
        return []
    times, previous = [], None
    for index, item in enumerate(items):
        time = item.get("at") if isinstance(item, dict) else None
        if time is None:
            time = first + index * gap if previous is None else previous + gap
        if previous is not None and time < previous:
            raise ProductionError("items[].at 需按出现顺序递增")
        times.append(round(time, 3))
        previous = time
    return times


def _css(tokens: Tokens) -> str:
    return f"""
:root{{
  --bg:{tokens.background};--panel:{tokens.panel};--fg:{tokens.foreground};
  --muted:{tokens.muted};--accent:{tokens.accent};--accent-text:{tokens.accent_text};
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;overflow:hidden;background:var(--bg);color:var(--fg);
  font-family:{FONT_SANS};-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}}
.stage{{position:absolute;inset:0;display:flex;flex-direction:column;padding:10vh 8.5vw 11vh}}
@media (min-aspect-ratio:4/5){{.stage{{padding:8vh 7vw}}}}
.eyebrow{{display:flex;align-items:center;gap:1.6vw;color:var(--muted);
  font-size:2.55vh;font-weight:600;letter-spacing:.22em;text-transform:uppercase}}
.eyebrow::before{{content:"";width:2.6vw;height:2.6vw;background:var(--accent);border-radius:50%;flex:none}}
.hairline{{height:1px;background:color-mix(in srgb,var(--fg) 22%,transparent);transform-origin:left}}
.muted{{color:var(--muted)}}
.accent{{color:var(--accent)}}
"""


def _script(duration: float, body: str) -> str:
    return f"""
<script>
const plan={{duration:{duration:.3f}}};
const clamp=x=>Math.min(1,Math.max(0,x));
const ease=x=>1-Math.pow(1-x,3);
const easeInOut=x=>x<.5?4*x*x*x:1-Math.pow(-2*x+2,3)/2;
const seg=(t,a,b)=>clamp((t-a)/(b-a));
function frame(t){{
  const time=Math.max(0,Math.min(t,plan.duration));
{body}
}}
window.film={{duration:plan.duration,frame}};
frame(0);
if(!window.filmHost){{
  const start=performance.now();
  const loop=now=>{{const t=(now-start)/1000;frame(t);if(t<plan.duration)requestAnimationFrame(loop);}};
  requestAnimationFrame(loop);
}}
</script>
"""


# ---------------------------------------------------------------- recipes


@_recipe(
    name="hook", label="开场问题卡", summary="一句提问压满画面，适合片头 2–3 秒定调。", role="opener",
    data_fields=("eyebrow", "title", "subtitle"), default_duration=3.0,
)
def _hook(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    lines = _lines(data.get("title"), 24)
    if not lines:
        raise ProductionError("hook 需要 title，可用换行分成两行")
    subtitle = _esc(data.get("subtitle"), 40)
    eyebrow = _esc(data.get("eyebrow"), 16)
    blocks = "".join(f'<span class="mask"><span class="inner">{line}</span></span>' for line in lines)
    body = f"""
<div class="stage hook">
  {'<p class="eyebrow">' + eyebrow + '</p>' if eyebrow else ''}
  <h1 class="title">{blocks}</h1>
  <div class="bar"></div>
  {f'<p class="sub">{subtitle}</p>' if subtitle else ''}
</div>"""
    css = f"""
.hook{{justify-content:center;gap:4.4vh}}
.title{{font-size:{_fit(lines, 14.6)};font-weight:850;line-height:1.06;letter-spacing:-.02em}}
.mask{{display:block;overflow:hidden;padding-bottom:.06em;margin-bottom:-.06em}}
.inner{{display:block;will-change:transform}}
.bar{{width:0;height:1.1vh;background:var(--accent);border-radius:1vh;margin-top:2.2vh}}
.sub{{font-size:3.4vh;color:var(--muted);line-height:1.55;margin-top:1.6vh;max-width:16em;text-wrap:balance}}
"""
    js = """
const inners=[...document.querySelectorAll('.inner')];
inners.forEach((el,i)=>{const p=ease(seg(time,0.12+i*0.16,0.86+i*0.16));el.style.transform=`translateY(${(1-p)*112}%)`;});
const bar=document.querySelector('.bar');
bar.style.width=ease(seg(time,0.62,1.28))*38+'%';
const sub=document.querySelector('.sub');
if(sub)sub.style.opacity=ease(seg(time,0.9,1.5));
"""
    return css, body, js


@_recipe(
    name="chapter", label="章节卡", summary="大编号加章节名，做段落切换和节奏断点。只放标题，不写内容摘要。", role="transition",
    data_fields=("index", "title"), default_duration=2.2,
)
def _chapter(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    index = _esc(data.get("index") or "01", 3)
    title = _esc(data.get("title"), 14)
    if not title:
        raise ProductionError("chapter 需要 title")
    if set(data) - {"index", "title"}:
        raise ProductionError(
            "chapter 只放编号和标题。小节要点不要写在转场卡上：小节里放进口播分屏，"
            "全屏要点卡留给总结段"
        )
    body = f"""
<div class="stage chapter">
  <div class="num">{index}</div>
  <div class="rule"></div>
  <h1 class="title"><span class="mask"><span class="inner">{title}</span></span></h1>
</div>"""
    css = f"""
.chapter{{justify-content:center;gap:3.2vh}}
.num{{font-size:26vh;font-weight:850;line-height:.82;letter-spacing:-.04em;color:transparent;
  -webkit-text-stroke:2px var(--accent);opacity:0}}
.rule{{height:.9vh;width:0;background:var(--accent);border-radius:1vh}}
.title{{font-size:{_fit([title], 11.4)};font-weight:800;line-height:1.12}}
.mask{{display:block;overflow:hidden;padding-bottom:.1em;margin-bottom:-.1em}}
.inner{{display:block}}
"""
    js = """
const num=document.querySelector('.num');
num.style.opacity=String(ease(seg(time,0.05,0.6)));
num.style.transform=`translateY(${(1-ease(seg(time,0.05,0.7)))*-4}vh)`;
document.querySelector('.rule').style.width=ease(seg(time,0.18,0.85))*26+'%';
const inner=document.querySelector('.inner');
inner.style.transform=`translateY(${(1-ease(seg(time,0.34,1.0)))*110}%)`;
"""
    return css, body, js


@_recipe(
    name="list", label="要点列表", summary="编号要点逐条入场，用于三件事、理由和清单。", role="recap",
    data_fields=("eyebrow", "title", "items"), default_duration=4.2,
)
def _items_recipe(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    items = _items(data)
    if not items:
        raise ProductionError("list 需要 items")
    eyebrow = _esc(data.get("eyebrow"), 16)
    title = _esc(data.get("title"), 24)
    rows = "".join(
        f"""<li class="row"><span class="idx">{i:02d}</span>
        <span class="copy"><span class="t">{it['title']}</span>
        {f'<span class="d">{it["detail"]}</span>' if it["detail"] else ''}</span></li>"""
        for i, it in enumerate(items, start=1)
    )
    body = f"""
<div class="stage list">
  <header>
    {'<p class="eyebrow">' + eyebrow + '</p>' if eyebrow else ''}
    {f'<h1 class="title">{title}</h1>' if title else ''}
  </header>
  <ul class="rows">{rows}</ul>
</div>"""
    css = _cssf("""
.list{gap:5vh}
header{display:flex;flex-direction:column;gap:2.4vh}
.title{font-size:__FIT__;font-weight:800;line-height:1.16;letter-spacing:-.01em}
.rows{list-style:none;display:flex;flex-direction:column;gap:0;flex:1;justify-content:center}
.row{display:flex;align-items:baseline;gap:3.4vw;padding:3.1vh 0;border-top:1px solid color-mix(in srgb,var(--fg) 14%,transparent)}
.row:last-child{border-bottom:1px solid color-mix(in srgb,var(--fg) 14%,transparent)}
.idx{font-size:3.1vh;font-weight:700;font-variant-numeric:tabular-nums;width:7vw;flex:none;
  color:var(--accent-text);background:var(--accent);border-radius:.4vh;text-align:center}
.copy{display:flex;flex-direction:column;gap:.9vh}
.t{font-size:5.5vh;font-weight:750;line-height:1.24}
.d{font-size:3.5vh;color:var(--muted);line-height:1.4}
""", _fit([title or " "], 7.4))
    js = f"const starts={json.dumps(motion_item_starts('list', items))};" + """
const rows=[...document.querySelectorAll('.row')];
rows.forEach((row,i)=>{
  const a=starts[i];
  const u=seg(time,a,a+0.42);
  const p=u*u*(3-2*u);
  row.style.opacity=String(p);
  row.style.transform=`translateX(${(1-p)*-2}vw)`;
});
"""
    return css, body, js


@_recipe(
    name="compare", label="对照卡", summary="两栏对照同一个问题，例如外包与自养。", role="recap",
    data_fields=("title", "left", "right"), default_duration=4.6,
)
def _compare(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    title = _esc(data.get("title"), 24)

    def panel(raw: Any, side: str) -> str:
        if not isinstance(raw, dict) or not raw.get("title"):
            raise ProductionError(f"compare 的 {side} 需要 title")
        points = _items({"items": raw.get("items") or []})
        bullets = "".join(f'<li>{p["title"]}</li>' for p in points)
        return (
            f'<section class="panel {side}"><h2>{_esc(raw["title"], 12)}</h2>'
            f'<ul>{bullets}</ul></section>'
        )

    body = f"""
<div class="stage compare">
  {f'<h1 class="title">{title}</h1>' if title else ''}
  <div class="grid">
    {panel(data.get("left"), "left")}
    {panel(data.get("right"), "right")}
  </div>
</div>"""
    css = _cssf("""
.compare{gap:4vh;justify-content:center}
.title{font-size:__FIT__;font-weight:800;line-height:1.18}
.grid{display:flex;flex-direction:column;gap:2.4vh;flex:1;justify-content:center}
.panel{border-radius:2.2vh;padding:3.8vh 5vw;opacity:0;transform:translateY(3vh)}
.panel h2{font-size:4.6vh;font-weight:800;letter-spacing:.02em;margin-bottom:2vh}
.panel ul{list-style:none;display:flex;flex-direction:column;gap:1.3vh}
.panel li{font-size:3.6vh;line-height:1.45;padding-left:1.6em;position:relative}
.panel li::before{content:"";position:absolute;left:0;top:.62em;width:.62em;height:.62em;border-radius:50%;background:currentColor;opacity:.45}
.left{background:var(--panel);color:var(--fg)}
.right{background:var(--fg);color:var(--bg)}
.right li::before{background:var(--accent);opacity:1}
""", _fit([title or " "], 6.6))
    js = """
const panels=[...document.querySelectorAll('.panel')];
panels.forEach((p,i)=>{const e=ease(seg(time,0.16+i*0.5,0.86+i*0.5));p.style.opacity=String(e);p.style.transform=`translateY(${(1-e)*3}vh)`;});
"""
    return css, body, js


@_recipe(
    name="quote", label="金句卡", summary="衬线大字金句，用于立场表达和转场停顿。",
    data_fields=("text", "source"), default_duration=3.2,
)
def _quote(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    lines = _lines(data.get("text"), 22)
    if not lines:
        raise ProductionError("quote 需要 text")
    source = _esc(data.get("source"), 16)
    blocks = "".join(f'<span class="mask"><span class="inner">{line}</span></span>' for line in lines)
    body = f"""
<div class="stage quote">
  <div class="mark">“</div>
  <blockquote>{blocks}</blockquote>
  {f'<p class="source">— {source}</p>' if source else ''}
</div>"""
    css = f"""
.stage.quote{{background:var(--fg);color:var(--bg);justify-content:center;gap:3vh;font-family:{FONT_SERIF}}}
.mark{{font-size:18vh;line-height:.6;color:var(--accent);opacity:.9}}
blockquote{{font-size:{_fit(lines, 9.2)};font-weight:600;line-height:1.34;letter-spacing:.01em}}
.mask{{display:block;overflow:hidden;padding-bottom:.14em;margin-bottom:-.14em}}
.inner{{display:block}}
.source{{font-family:{FONT_SANS};font-size:3.4vh;opacity:.72;letter-spacing:.06em}}
"""
    js = """
document.querySelector('.mark').style.opacity=String(.9*ease(seg(time,0,0.4)));
const inners=[...document.querySelectorAll('.inner')];
inners.forEach((el,i)=>{const p=ease(seg(time,0.12+i*0.22,0.86+i*0.22));el.style.transform=`translateY(${(1-p)*114}%)`;});
const src=document.querySelector('.source');
if(src)src.style.opacity=String(.72*ease(seg(time,0.9,1.4)));
"""
    return css, body, js


@_recipe(
    name="number", label="数字卡", summary="大数字滚动计数，用于规模、时长和对比量级。",
    data_fields=("value", "unit", "label", "note"), default_duration=3.0,
)
def _number(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    value = data.get("value")
    if type(value) is not int or abs(value) > 99_999_999:
        raise ProductionError("number 的 value 需要整数")
    unit = _esc(data.get("unit"), 6)
    label = _esc(data.get("label"), 20)
    note = _esc(data.get("note"), 40)
    body = f"""
<div class="stage number">
  <p class="label">{label}</p>
  <div class="row"><span class="value">0</span><span class="unit">{unit}</span></div>
  {f'<p class="note">{note}</p>' if note else ''}
  <div class="line"></div>
</div>"""
    # Digits in the bold stack run roughly 0.58em wide with tabular figures; size the
    # counter so the widest frame ("9,999,999") still clears the unit badge.
    digits = len(f"{value:,}")
    budget = 74 if unit else 84
    fit = f"min(27vh,calc({budget}vw / {digits * 0.58:.2f}))"
    css = _cssf("""
.number{justify-content:center;gap:2.4vh}
.label{font-size:4.2vh;color:var(--muted);font-weight:600;letter-spacing:.04em}
.row{display:flex;align-items:baseline;gap:1.4vw;max-width:100%}
.value{font-size:__FIT__;font-weight:850;line-height:.92;letter-spacing:-.045em;font-variant-numeric:tabular-nums;white-space:nowrap}
.unit{font-size:7.4vh;font-weight:750;color:var(--accent-text);background:var(--accent);
  padding:.06em .3em;border-radius:.5vh}
.note{font-size:3.7vh;color:var(--muted);line-height:1.5;max-width:16em}
.line{height:.7vh;background:var(--accent);width:0;border-radius:1vh;margin-top:2vh}
""", fit)
    js = f"""
const target={value};
const value=document.querySelector('.value');
const p=ease(seg(time,0.1,0.95));
value.textContent=Math.round(target*p).toLocaleString('zh-CN');
document.querySelector('.line').style.width=(p*44)+'%';
const note=document.querySelector('.note');
if(note)note.style.opacity=ease(seg(time,0.75,1.3));
"""
    return css, body, js


@_recipe(
    name="steps", label="流程链", summary="三步因果链，用于把问题拆成先后关系。", role="recap",
    data_fields=("title", "items"), default_duration=4.4,
)
def _steps(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    items = _items(data)
    if len(items) < 2:
        raise ProductionError("steps 至少需要 2 项")
    title = _esc(data.get("title"), 24)
    parts = []
    for item in items:
        detail = f'<span class="d">{item["detail"]}</span>' if item["detail"] else ""
        parts.append(
            f'<li class="step"><span class="dot"></span>'
            f'<span class="t">{item["title"]}</span>{detail}</li>'
        )
    blocks = "".join(parts)
    body = f"""
<div class="stage steps">
  {f'<h1 class="title">{title}</h1>' if title else ''}
  <ol class="chain">{blocks}</ol>
</div>"""
    css = _cssf("""
.steps{gap:5vh;justify-content:center}
.title{font-size:__FIT__;font-weight:800;line-height:1.16}
.chain{list-style:none;display:flex;flex-direction:column;gap:2.2vh;position:relative;padding-left:5vw}
.chain::before{content:"";position:absolute;left:1.05vw;top:2.4vh;bottom:2.4vh;width:2px;
  background:color-mix(in srgb,var(--fg) 16%,transparent)}
.step{position:relative;display:flex;flex-direction:column;gap:.7vh;padding:1.6vh 0;opacity:0}
.dot{position:absolute;left:-4.35vw;top:2.7vh;width:2.2vw;height:2.2vw;border-radius:50%;
  background:var(--bg);border:2px solid color-mix(in srgb,var(--fg) 30%,transparent)}
.step.on .dot{background:var(--accent);border-color:var(--accent)}
.t{font-size:5.2vh;font-weight:750;line-height:1.3}
.d{font-size:3.5vh;color:var(--muted);line-height:1.45}
""", _fit([title or " "], 7))
    js = f"const starts={json.dumps(motion_item_starts('steps', items))};" + """
const steps=[...document.querySelectorAll('.step')];
steps.forEach((s,i)=>{
  const a=starts[i];
  const p=ease(seg(time,a,a+0.5));
  s.style.opacity=String(p);s.style.transform=`translateY(${(1-p)*2}vh)`;
  s.classList.toggle('on',time>a+0.28);
});
"""
    return css, body, js


@_recipe(
    name="checklist", label="条件清单", summary="逐条打勾的判断条件，用于“什么时候该……”。", role="recap",
    data_fields=("title", "items"), default_duration=4.0,
)
def _checklist(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    items = _items(data)
    if not items:
        raise ProductionError("checklist 需要 items")
    title = _esc(data.get("title"), 24)
    rows = "".join(
        f'<li class="item"><span class="box"><svg viewBox="0 0 24 24"><path d="M4 12.5l5 5L20 6.5"/></svg></span>'
        f'<span class="t">{it["title"]}</span></li>'
        for it in items
    )
    body = f"""
<div class="stage checklist">
  {f'<h1 class="title">{title}</h1>' if title else ''}
  <ul class="items">{rows}</ul>
</div>"""
    css = _cssf("""
.checklist{gap:5vh;justify-content:center}
.title{font-size:__FIT__;font-weight:800;line-height:1.16}
.items{list-style:none;display:flex;flex-direction:column;gap:2.6vh}
.item{display:flex;align-items:center;gap:3.6vw;opacity:0}
.box{width:7vw;height:7vw;border-radius:1.6vw;border:2px solid color-mix(in srgb,var(--fg) 26%,transparent);
  flex:none;display:grid;place-items:center}
.item.on .box{background:var(--accent);border-color:var(--accent)}
.box svg{width:4.4vw;height:4.4vw;fill:none;stroke:var(--accent-text);stroke-width:3;
  stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:26;stroke-dashoffset:26}
.t{font-size:5vh;font-weight:700;line-height:1.35}
""", _fit([title or " "], 7))
    js = f"const starts={json.dumps(motion_item_starts('checklist', items))};" + """
const items=[...document.querySelectorAll('.item')];
const paths=[...document.querySelectorAll('.box path')];
items.forEach((el,i)=>{
  const a=starts[i];
  el.style.opacity=String(ease(seg(time,a,a+0.4)));
  el.style.transform=`translateX(${(1-ease(seg(time,a,a+0.45)))*-3}vw)`;
  el.classList.toggle('on',time>a+0.2);
  paths[i].style.strokeDashoffset=String(26*(1-easeInOut(seg(time,a+0.24,a+0.62))));
});
"""
    return css, body, js


@_recipe(
    name="end", label="片尾卡", summary="品牌署名加一句行动号召，收住整条片子。", role="closer",
    data_fields=("brand", "slogan", "cta"), default_duration=3.4,
)
def _end(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    brand = _esc(data.get("brand"), 14)
    slogan = _lines(data.get("slogan"), 22)
    cta = _esc(data.get("cta"), 28)
    if not brand:
        raise ProductionError("end 需要 brand")
    slogan_html = "".join(f'<span class="mask"><span class="inner">{line}</span></span>' for line in slogan)
    body = f"""
<div class="stage end">
  <div class="top">
    <p class="brand">{brand}</p>
    <h1 class="slogan">{slogan_html}</h1>
  </div>
  {f'<div class="cta">{cta}</div>' if cta else ''}
</div>"""
    css = f"""
.end{{justify-content:space-between;padding-top:16vh;padding-bottom:14vh}}
.brand{{font-size:3.2vh;letter-spacing:.34em;color:var(--muted);font-weight:600}}
.slogan{{font-size:{_fit(slogan, 9.4)};font-weight:850;line-height:1.16;margin-top:3.6vh;letter-spacing:-.015em}}
.mask{{display:block;overflow:hidden;padding-bottom:.08em;margin-bottom:-.08em}}
.inner{{display:block}}
.cta{{display:inline-flex;align-self:flex-start;align-items:center;gap:1.6vw;
  background:var(--accent);color:var(--accent-text);font-size:4vh;font-weight:800;
  padding:1.9vh 5.2vw;border-radius:99px;opacity:0}}
"""
    js = """
const inners=[...document.querySelectorAll('.inner')];
inners.forEach((el,i)=>{const p=ease(seg(time,0.1+i*0.18,0.8+i*0.18));el.style.transform=`translateY(${(1-p)*112}%)`;});
const cta=document.querySelector('.cta');
if(cta){const p=ease(seg(time,0.9,1.4));cta.style.opacity=String(p);cta.style.transform=`translateY(${(1-p)*2}vh)`;}
"""
    return css, body, js


# ---------------------------------------------------------------- rendering


@_recipe(
    name="headline", label="标题条", summary="小标签加一到两行大标题，口播分屏里标出这段在讲什么。",
    data_fields=("eyebrow", "title"), default_duration=6.0,
)
def _headline(data: dict[str, Any], tokens: Tokens, duration: float) -> tuple[str, str, str]:
    lines = _lines(data.get("title"), 16)
    if not lines or len(lines) > 2:
        raise ProductionError("headline 需要一到两行 title")
    eyebrow = _esc(data.get("eyebrow"), 16)
    blocks = "".join(f'<span class="mask"><span class="inner">{line}</span></span>' for line in lines)
    body = f"""
<div class="stage headline">
  {'<p class="eyebrow">' + eyebrow + '</p>' if eyebrow else ''}
  <h1 class="title">{blocks}</h1>
  <div class="bar"></div>
</div>"""
    css = f"""
.headline{{justify-content:center;gap:3.2vh}}
.title{{font-size:{_fit(lines, 13)};font-weight:850;line-height:1.1;letter-spacing:-.015em}}
.mask{{display:block;overflow:hidden;padding-bottom:.06em;margin-bottom:-.06em}}
.inner{{display:block}}
.bar{{width:0;height:1vh;background:var(--accent);border-radius:1vh}}
"""
    js = """
const inners=[...document.querySelectorAll('.inner')];
inners.forEach((el,i)=>{const p=ease(seg(time,0.1+i*0.14,0.8+i*0.14));el.style.transform=`translateY(${(1-p)*112}%)`;});
document.querySelector('.bar').style.width=ease(seg(time,0.5,1.1))*30+'%';
const eyebrow=document.querySelector('.eyebrow');
if(eyebrow)eyebrow.style.opacity=String(ease(seg(time,0,0.5)));
"""
    return css, body, js


def list_motion_recipes() -> dict[str, Any]:
    return {"recipes": [
        {
            "id": item.name, "label": item.label, "summary": item.summary, "role": item.role,
            "fields": list(item.data_fields), "default_seconds": item.default_duration,
        }
        for item in RECIPES.values()
    ], "roles": ROLE_GUIDE,
        "panels": "任意配方都可放进口播分屏 layout.with.graphic，会按栏位尺寸渲染；list/steps/checklist 的 items[].at 可对齐口播出现时间。"}


def _tokens(style_preset: StylePreset | None) -> Tokens:
    profile = get_production_style(style_preset)
    if profile is None:
        profile = get_production_style("editorial")
    assert profile is not None
    palette = profile.palette
    return Tokens(
        background=palette["background"], panel=palette["panel"], foreground=palette["foreground"],
        muted=palette["muted"], accent=palette["accent"], accent_text=palette["accent_text"],
    )


def scene_html(recipe: str, data: dict[str, Any], *, duration: float = 0, style_preset: StylePreset | None = None) -> str:
    item = RECIPES.get(recipe)
    if item is None:
        raise ProductionError(f"未知动效 {recipe!r}。可用：{', '.join(RECIPES)}")
    if not isinstance(data, dict):
        raise ProductionError("data 必须是对象")
    tokens = _tokens(style_preset)
    seconds = duration or item.default_duration
    css, body, js = item.render(data, tokens, seconds)
    return f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<title>{html.escape(item.label)}</title>
<style>{_css(tokens)}{css}</style>
{body}
{_script(seconds, js)}
</html>"""


def _cache_key(recipe: str, scene: str, fps: int, width: int, height: int) -> str:
    """Content-address a rendered clip by the page it draws.

    Hashing the generated scene — not the recipe inputs — means editing a recipe's
    markup or CSS invalidates its cached clips instead of silently shipping the old
    render.
    """
    return hashlib.sha256(json.dumps({
        "v": MOTION_CACHE_VERSION, "recipe": recipe, "scene": scene,
        "fps": fps, "width": width, "height": height,
        "fonts": FONT_REVISION if font_path(False).is_file() and font_path(True).is_file() else "system",
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def render_motion(
    settings: Settings,
    recipe: str,
    data: dict[str, Any],
    *,
    duration: float = 0,
    fps: int = 30,
    width: int = 1080,
    height: int = 1920,
    style_preset: StylePreset | None = None,
    render: bool = True,
) -> dict[str, Any]:
    """Build one motion clip and, unless ``render`` is false, render it to MP4."""
    item = RECIPES.get(recipe)
    if item is None:
        raise ProductionError(f"未知动效 {recipe!r}。可用：{', '.join(RECIPES)}")
    seconds = float(duration or item.default_duration)
    if not 0.6 <= seconds <= 30:
        raise ProductionError("动效时长需在 0.6–30 秒")
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ProductionError("fps 需为 1–120 的整数")

    scene = scene_html(recipe, data, duration=seconds, style_preset=style_preset)
    cache_key = _cache_key(recipe, scene, fps, width, height)
    cache_folder = settings.work_root / "production" / "motion_cache" / f"{recipe}-v{MOTION_CACHE_VERSION}"
    cache_folder.mkdir(parents=True, exist_ok=True)
    cached_mp4 = cache_folder / f"{cache_key}.mp4"

    project = create_video_animation(
        settings, scene_html=scene, bundle_fonts=True,
        width=width, height=height, duration=seconds,
    )
    result: dict[str, Any] = {
        "recipe": recipe, "label": item.label, "duration": seconds, "fps": fps,
        "width": width, "height": height, "project_path": project["project_path"],
    }
    if render:
        if cached_mp4.is_file():
            result["mp4_path"] = str(cached_mp4)
            result["cached"] = True
        else:
            try:
                rendered = render_openfilm(settings, project["project_path"], fps=fps)
            except Exception as exc:
                raise ProductionError(
                    f"动效 {recipe} 已生成工程，但 OpenFilm 渲染失败：{exc}。"
                    "可先调用 setup_openfilm_runtime(install=true) 检查运行时。"
                ) from exc
            source_mp4 = Path(rendered["output_path"])
            try:
                source_mp4.replace(cached_mp4)
            except OSError:
                pass
            result["mp4_path"] = str(cached_mp4 if cached_mp4.is_file() else source_mp4)
            result["cached"] = False
    return result

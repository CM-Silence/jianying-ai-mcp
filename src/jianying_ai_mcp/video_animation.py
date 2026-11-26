from __future__ import annotations

import json
import math
import re
import shutil
from html import escape
from pathlib import Path
from typing import Any, get_args

from .config import Settings
from .managed_fonts import font_face
from .production_graphics import PALETTES, absolute_file, create_output_folder, validate_canvas
from .production_styles import AnimationMotion, StylePreset, get_production_style


_MARKS = {
    "rule": "width:16vw;height:2.2vh;border-radius:0.75em",
    "dot": "width:3.5vw;height:3.5vw;border-radius:50%",
    "block": "width:9vw;height:4vh",
    "line": "width:23vw;height:0.55vh",
}


_ANIMATION = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{document_title}</title>
<style>
* {{ box-sizing: border-box; }}
html, body {{ width: 100%; height: 100%; margin: 0; overflow: hidden; }}
body {{ {palette} background: var(--background); color: var(--foreground);
  font-family: {family}; }}
main {{ display: grid; align-content: center; min-height: 100%; padding: 12vh 9vw; }}
.split {{ grid-template-rows: minmax(0, 1fr) auto; gap: 5vh; height: 100%; }}
figure {{ height: 100%; min-height: 0; margin: 0; background: var(--panel); overflow: hidden; }}
img {{ width: 100%; height: 100%; object-fit: cover; object-position: {focus}; display: block; }}
.copy {{ min-width: 0; overflow-wrap: anywhere; }}
.eyebrow {{ margin: 0 0 2.2vh; color: var(--muted); font-size: min(3.3vw, 4.2vh); font-weight: 650; }}
.eyebrow:empty, .detail:empty {{ display: none; }}
h1 {{ margin: 0; max-width: 18em; line-height: 1.14; font-size: min(8vw, 9vh); font-weight: 750; }}
.detail {{ margin: 2.5vh 0 0; max-width: 36em; color: var(--muted); line-height: 1.45; font-size: min(4vw, 4vh); }}
.number {{ width: fit-content; max-width: 100%; padding: .10em .27em; margin: 0 0 2.3vh;
  background: var(--accent); color: var(--accent-text); font-size: min(13vw, 15vh); font-weight: 750;
  line-height: 1.08; font-variant-numeric: tabular-nums; }}
{design_rules}
@media (min-aspect-ratio: 5/4) {{
  .split {{ grid-template: 1fr / 1.15fr .85fr; gap: 5vw; align-items: center; }}
}}
</style>
<main class="{layout}">{picture}
  <div class="copy">
    <p class="eyebrow">{eyebrow}</p>
    {metric}
    <h1>{title}</h1>
    <p class="detail">{subtitle}</p>
  </div>
</main>
<script>
const plan = {plan};
const copy = document.querySelector('.copy');
const image = document.querySelector('figure');
const number = document.querySelector('.number');
const clamp = (x) => Math.max(0, Math.min(1, x));
function frame(t) {{
  const p = clamp(t / plan.entrance);
  const eased = 1 - (1 - p) ** 3;
  if (plan.motion === 'slide_in') {{
    copy.style.opacity = String(p);
    copy.style.transform = `translateY(${{(1 - eased) * 5}}vh)`;
  }} else if (plan.motion === 'fade_in') {{
    copy.style.opacity = String(eased);
  }} else if (plan.motion === 'slide_right') {{
    copy.style.opacity = String(p);
    copy.style.transform = `translateX(${{-(1 - eased) * 9}}vw)`;
  }} else if (plan.motion === 'wipe_in') {{
    copy.style.clipPath = `inset(0 ${{(1 - eased) * 100}}% 0 0)`;
  }} else if (plan.motion === 'photo_reveal') {{
    image.style.clipPath = `inset(0 ${{(1 - eased) * 100}}% 0 0)`;
  }} else if (plan.motion === 'count_up') {{
    number.textContent = Math.round(plan.value * eased).toLocaleString(plan.locale) + plan.unit;
  }}
}}
window.film = {{ duration: plan.duration, frame }};
frame(0);
if (!window.filmHost) {{
  const start = performance.now();
  function play(now) {{
    const t = (now - start) / 1000;
    frame(Math.min(t, plan.duration));
    if (t < plan.duration) requestAnimationFrame(play);
  }}
  requestAnimationFrame(play);
}}
</script>
"""


def _text(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} 最多 {limit} 字符")
    return escape(value.strip())


def _luminance(color: str) -> float:
    rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [component / 12.92 if component <= 0.04045 else ((component + 0.055) / 1.055) ** 2.4 for component in rgb]
    return sum(component * weight for component, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _palette_findings(palette: dict[str, str]) -> list[dict[str, Any]]:
    findings = []
    for foreground, background in (("foreground", "background"), ("muted", "background"), ("accent_text", "accent")):
        darker, lighter = sorted((_luminance(palette[foreground]), _luminance(palette[background])))
        contrast = (lighter + 0.05) / (darker + 0.05)
        if contrast < 4.5:
            findings.append({"roles": [foreground, background], "contrast": round(contrast, 2), "check": "review_text_contrast"})
    return findings


def create_video_animation(
    settings: Settings,
    *,
    title: str = "",
    subtitle: str = "",
    eyebrow: str = "",
    image_path: str | None = None,
    image_focus: list[float] | None = None,
    preset: str = "editorial",
    colors: dict[str, str] | None = None,
    motion: AnimationMotion | None = None,
    value: int | None = None,
    unit: str = "",
    width: int = 1080,
    height: int = 1920,
    duration: float = 3,
    entrance: float = 0.65,
    scene_html: str | None = None,
    assets: dict[str, str] | None = None,
    style_preset: StylePreset | None = None,
    bundle_fonts: bool = False,
) -> dict[str, Any]:
    use_template = scene_html is None
    validate_canvas(width, height)
    if width % 2 or height % 2:
        raise ValueError("用于视频渲染的宽高必须为偶数")
    if not (math.isfinite(duration) and 0.5 <= duration <= 120 and math.isfinite(entrance) and 0.1 <= entrance < duration):
        raise ValueError("duration 需为 0.5–120 秒，entrance 需至少为 0.1 秒且短于 duration")
    profile = get_production_style(style_preset)
    if profile and not use_template:
        raise ValueError("style_preset 适用于预设动画，自定义 scene_html 请在页面中定义样式")
    if motion is None:
        motion = profile.animation_motion if profile else "slide_in"
    if preset not in PALETTES or motion not in get_args(AnimationMotion):
        raise ValueError("请选择已支持的 preset 或 motion")
    if scene_html is None and not title.strip():
        raise ValueError("预设动画需要 title")
    if scene_html is None and motion == "photo_reveal" and not image_path:
        raise ValueError("photo_reveal 需要 image_path")
    if value is not None and (type(value) is not int or abs(value) > 999999999):
        raise ValueError("value 必须是 -999999999 到 999999999 的整数")
    if scene_html is None and motion == "count_up" and value is None:
        raise ValueError("count_up 需要 value")
    if scene_html is not None and (len(scene_html) > 200000 or not scene_html.strip()):
        raise ValueError("scene_html 需要非空 HTML，最多 200000 字符")
    if scene_html is None and assets:
        raise ValueError("assets 用于自定义 scene_html，预设动画请使用 image_path")
    palette = dict(profile.palette if profile else PALETTES[preset])
    for key, color in (colors or {}).items():
        if key not in palette or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise ValueError(f"配色项 {key} 必须是已支持的 #RRGGBB 颜色")
        palette[key] = color
    focus = [0.5, 0.5] if image_focus is None else image_focus
    if len(focus) != 2 or not all(math.isfinite(part) and 0 <= part <= 1 for part in focus):
        raise ValueError("image_focus 需要 [x, y]，值在 0–1 之间")

    title_html = _text(title, "title", 80)
    subtitle_html = _text(subtitle, "subtitle", 160)
    eyebrow_html = _text(eyebrow, "eyebrow", 32)
    unit_html = _text(unit, "unit", 12)
    inputs: dict[str, Path] = {}
    for name, path in (assets or {}).items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}\.[a-zA-Z0-9]{1,8}", name) or name.casefold() in {"film.html", "scene.html"}:
            raise ValueError(f"资源名必须是安全的单个文件名: {name}")
        if name in inputs:
            raise ValueError(f"资源名重复: {name}")
        inputs[name] = absolute_file(path, "动画素材")
    image_name = ""
    if image_path:
        source = absolute_file(image_path, "图片")
        if source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}:
            raise ValueError("浏览器预设图片需要 JPG、PNG、WebP、AVIF 或 GIF")
        image_name = "picture" + source.suffix.lower()
        if image_name in inputs:
            raise ValueError(f"动画资源名已占用: {image_name}")
        inputs[image_name] = source

    folder = create_output_folder(settings, "web_animation")
    for name, path in inputs.items():
        shutil.copyfile(path, folder / name)

    if scene_html is None:
        plan = json.dumps({
            "duration": duration, "entrance": entrance, "motion": motion,
            "value": value, "unit": unit.strip(), "locale": "zh-CN",
        }, ensure_ascii=False).replace("<", "\\u003c")
        picture = f'<figure><img src="{image_name}" alt=""></figure>' if image_name else ""
        metric = f'<p class="number">{value:,}{unit_html}</p>' if value is not None else ""
        family = "'Noto Serif SC', 'Noto Serif CJK SC', SimSun, Georgia, serif" if profile and profile.serif else "'Noto Sans SC', 'Microsoft YaHei', 'Noto Sans CJK SC', system-ui, sans-serif"
        design_rules = (
            f'.copy::before {{content:"";display:block;margin-bottom:2.5vh;background:var(--accent);{_MARKS[profile.graphic_shape]}}}'
            if profile else ""
        )
        scene_html = _ANIMATION.format(
            document_title=title_html, title=title_html, subtitle=subtitle_html, eyebrow=eyebrow_html,
            palette=" ".join(f"--{key.replace('_', '-')}: {color};" for key, color in palette.items()),
            layout="split" if image_name else "simple", focus=f"{focus[0] * 100:.2f}% {focus[1] * 100:.2f}%",
            picture=picture, metric=metric, plan=plan, family=family, design_rules=design_rules,
        )
    if use_template or bundle_fonts:
        faces = "".join(font_face(folder, serif) for serif, family_name in
                        ((False, "Noto Sans SC"), (True, "Noto Serif SC")) if family_name in scene_html)
        scene_html = scene_html.replace("<style>", "<style>" + faces, 1)
    scene_path = folder / "scene.html"
    scene_path.write_text(scene_html, encoding="utf-8")
    film = folder / "film.html"
    film.write_text(
        f'<!doctype html>\n<meta charset="utf-8">\n<meta name="viewport" content="width={width}, height={height}">\n'
        f'<title>{title_html or "Video animation"}</title>\n'
        f'<section><iframe id="scene" src="scene.html#t=0,{duration:.6f}" at="0"></iframe></section>\n',
        encoding="utf-8",
    )
    return {
        "project_path": str(folder), "film_path": str(film), "scene_path": str(scene_path),
        "width": width, "height": height, "duration": duration, "palette": palette,
        "review_times_s": ([0, round(entrance / 2, 3), round((duration + entrance) / 2, 3)] if use_template
                           else [0, round(duration * 0.25, 3), round(duration * 0.6, 3)]),
        **({"design_findings": _palette_findings(palette)} if use_template else {}),
        **({"motion": motion} if use_template else {}),
        **({"style_preset": style_preset} if profile else {}),
    }

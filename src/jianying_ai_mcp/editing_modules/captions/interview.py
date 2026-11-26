from .theme import CaptionThemeModule


MODULE = CaptionThemeModule(
    id="interview",
    summary="访谈字幕采用白字、黑描边和阴影，可搭配关键词黄字。",
    parameters={
        "caption_theme": "interview",
        "caption_style": "可覆盖字号、字重和颜色等文字样式。",
        "caption_clip": "可覆盖字幕位置，transform_y 默认为 -0.43。",
        "caption_decoration": "可覆盖或添加底板、描边、阴影及剪映字体。",
    },
    examples=({"caption_theme": "interview"},),
    style={"size": 11.5, "color": [1, 1, 1], "bold": True},
    decoration={
        "border": {"color": [0, 0, 0], "alpha": .96, "width": 20},
        "shadow": {"color": [0, 0, 0], "alpha": .87, "diffuse": 23, "distance": 10, "angle": -90},
    },
    position=-.43,
    inherit_background=False,
)

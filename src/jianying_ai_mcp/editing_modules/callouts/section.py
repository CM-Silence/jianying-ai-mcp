from .role import CalloutModule, callout_parameters


MODULE = CalloutModule(
    id="section",
    summary="带深色底板的小节标题，放在左上方，使用渐显渐隐或擦入擦出。",
    parameters=callout_parameters("section"),
    examples=({"shots": [{"overlays": [{"role": "section", "text": "如何验收", "frames": 45}]}]},),
    style={"size": 6.4},
    clip={"transform_x": -.37, "transform_y": .67},
    decoration={"background": {"color": "#171A1F", "alpha": .90, "style": 1, "round_radius": .14}},
    native_entrance="渐显",
    native_exit="渐隐",
    portable_entrance="wipe_in",
    portable_exit="wipe_out",
)

from .role import CalloutModule, callout_parameters


MODULE = CalloutModule(
    id="person",
    summary="人物介绍角标，放在左下方，使用横向滑入和滑出。",
    parameters=callout_parameters("person"),
    examples=({"shots": [{"overlays": [{"role": "person", "text": "受访者｜项目负责人", "frames": 60}]}]},),
    style={"size": 6.0, "max_line_width": .48},
    clip={"transform_x": -.47, "transform_y": -.28},
    decoration={"background": {"color": "#171A1F", "alpha": .90, "style": 1, "round_radius": .14}},
    native_entrance="向右滑动",
    native_exit="向左滑动",
    portable_entrance="slide_right",
    portable_exit="slide_left",
)

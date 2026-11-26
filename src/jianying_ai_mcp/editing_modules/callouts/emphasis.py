from .role import CalloutModule, callout_parameters


MODULE = CalloutModule(
    id="emphasis",
    summary="用暖黄描边大字突出一句重点，文字上移入场、下移退场。",
    parameters=callout_parameters("emphasis"),
    examples=({"shots": [{"overlays": [{"role": "emphasis", "text": "先确定验收标准", "frames": 45}]}]},),
    style={"size": 8.8, "color": [1.0, .86, .30]},
    clip={"transform_x": 0, "transform_y": .24},
    decoration={"border": {"color": [0, 0, 0], "alpha": .92, "width": 21}},
    native_entrance="向上滑动",
    native_exit="向下滑动",
    portable_entrance="rise",
    portable_exit="sink",
)

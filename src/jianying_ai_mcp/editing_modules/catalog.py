from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import EditingModule, EditingModuleCategory, callouts, captions, layouts


_MODULES: dict[EditingModuleCategory, tuple[EditingModule, ...]] = {
    "layout": layouts.MODULES,
    "caption": captions.MODULES,
    "callout": callouts.MODULES,
}


def _summary(category: EditingModuleCategory, module: EditingModule) -> dict[str, str]:
    return {"category": category, "id": module.id, "summary": module.summary}


def list_editing_modules(
    category: EditingModuleCategory | None = None,
    module_id: str | None = None,
) -> dict[str, Any]:
    if category is not None and category not in _MODULES:
        raise ValueError(f"未知剪辑模块分类 {category!r}。可选分类为 {', '.join(_MODULES)}。")
    matches = [
        (group, module)
        for group, modules in _MODULES.items()
        if category is None or category == group
        for module in modules
        if module_id is None or module_id == module.id
    ]
    if module_id is None:
        return {"modules": [_summary(group, module) for group, module in matches]}
    if not matches:
        scope = f"{category} 分类中" if category is not None else ""
        raise ValueError(f"{scope}没有找到剪辑模块 {module_id!r}。省略 module_id 可查看模块列表。")
    if len(matches) > 1:
        groups = ", ".join(group for group, _ in matches)
        raise ValueError(f"剪辑模块 {module_id!r} 存在于多个分类。请用 category 指定 {groups} 中的一项。")
    group, module = matches[0]
    return {"modules": [{
        **_summary(group, module),
        "parameters": deepcopy(module.parameters),
        "examples": deepcopy(list(module.examples)),
    }]}

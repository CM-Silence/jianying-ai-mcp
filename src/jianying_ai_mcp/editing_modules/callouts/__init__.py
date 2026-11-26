from typing import Any

from ...production_graphics import ProductionError
from . import emphasis, person, section


MODULES = (person.MODULE, emphasis.MODULE, section.MODULE)
_ROLES = {role.id: role for role in MODULES}


def get_callout_defaults(role: Any, duration: int, label: str) -> dict[str, Any]:
    if not isinstance(role, str) or role not in _ROLES:
        raise ProductionError(f"{label}.role 支持 {'、'.join(_ROLES)}")
    return _ROLES[role].overlay_defaults(duration)

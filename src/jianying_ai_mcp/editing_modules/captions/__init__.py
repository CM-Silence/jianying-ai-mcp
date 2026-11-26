from ...production_graphics import ProductionError
from . import highlights, interview
from .theme import CaptionThemeModule


CaptionTheme = str
_THEME_MODULES = (interview.MODULE,)
_THEMES = {theme.id: theme for theme in _THEME_MODULES}
MODULES = (*_THEME_MODULES, highlights.MODULE)


def get_caption_theme(theme: CaptionTheme | None) -> CaptionThemeModule | None:
    if theme is None:
        return None
    if not isinstance(theme, str) or theme not in _THEMES:
        raise ProductionError(f"caption_theme 支持 {'、'.join(_THEMES)}")
    return _THEMES[theme]

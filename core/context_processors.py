"""Context processors globales del Admin."""
import os

from django.conf import settings

_RUTA_CSS = settings.BASE_DIR / "core" / "static" / "core" / "css" / "plusdigital.css"
_RUTA_JS = settings.BASE_DIR / "core" / "static" / "core" / "js" / "plusdigital.js"


def _version_de(ruta) -> int:
    try:
        return int(os.path.getmtime(ruta))
    except OSError:
        return 0


def plusdigital_css_version(request):
    """Versión de plusdigital.css/.js basada en su fecha de modificación, para
    que el navegador siempre traiga la copia nueva cuando se edita (ver
    core/templates/admin/base.html) en vez de servir una versión en caché."""
    return {
        "plusdigital_css_version": _version_de(_RUTA_CSS),
        "plusdigital_js_version": _version_de(_RUTA_JS),
    }

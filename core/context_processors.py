"""Context processors globales del Admin."""
import os

from django.conf import settings

_RUTA_CSS = settings.BASE_DIR / "core" / "static" / "core" / "css" / "plusdigital.css"


def plusdigital_css_version(request):
    """Versión de plusdigital.css basada en su fecha de modificación, para
    que el navegador siempre traiga la copia nueva cuando se edita (ver
    core/templates/admin/base.html) en vez de servir una versión en caché."""
    try:
        version = int(os.path.getmtime(_RUTA_CSS))
    except OSError:
        version = 0
    return {"plusdigital_css_version": version}

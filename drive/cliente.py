import json

from django.conf import settings
from google.oauth2 import service_account
from googleapiclient.discovery import build

from .exceptions import DriveUploadError

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_service = None  # cache del cliente construido (reutilizable entre llamadas del proceso)


def _cargar_credenciales():
    json_credenciales = getattr(settings, "GOOGLE_SERVICE_ACCOUNT_JSON", "")
    archivo_credenciales = getattr(settings, "GOOGLE_SERVICE_ACCOUNT_FILE", "")

    if json_credenciales:
        try:
            info = json.loads(json_credenciales)
        except ValueError as exc:
            raise DriveUploadError(f"GOOGLE_SERVICE_ACCOUNT_JSON no es JSON válido: {exc}") from exc
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    if archivo_credenciales:
        try:
            return service_account.Credentials.from_service_account_file(archivo_credenciales, scopes=SCOPES)
        except (OSError, ValueError) as exc:
            raise DriveUploadError(f"No se pudo cargar {archivo_credenciales}: {exc}") from exc

    raise DriveUploadError(
        "Falta configurar GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_SERVICE_ACCOUNT_FILE."
    )


def obtener_servicio(forzar_nuevo: bool = False):
    """Devuelve (y cachea) el cliente de la API de Drive."""
    global _service
    if _service is None or forzar_nuevo:
        credenciales = _cargar_credenciales()
        _service = build("drive", "v3", credentials=credenciales, cache_discovery=False)
    return _service

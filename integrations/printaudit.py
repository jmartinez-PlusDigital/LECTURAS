import logging
from datetime import date

import requests
from django.conf import settings

from .base import BaseLecturaClient, LecturaExterna
from .exceptions import IntegrationConnectionError

logger = logging.getLogger(__name__)


class ClientePrintAudit(BaseLecturaClient):
    """Cliente de la API de PrintAudit.

    NOTA: el endpoint y los nombres de campo (`serial`, `mono_count`,
    `color_count`) son un supuesto razonable a falta de la documentación
    real de la API. Ajusta `_parse_item` y la URL una vez que tengas acceso
    a la especificación oficial.
    """

    origen = "api_printaudit"

    def __init__(self, base_url=None, api_key=None, timeout=None):
        self.base_url = (base_url or settings.API_PRINTAUDIT_BASE_URL or "").rstrip("/")
        self.api_key = api_key or settings.API_PRINTAUDIT_API_KEY
        self.timeout = timeout or settings.API_INTEGRATIONS_TIMEOUT

    def obtener_lecturas(self) -> dict[str, LecturaExterna]:
        if not self.base_url or not self.api_key:
            raise IntegrationConnectionError(
                "PrintAudit: falta configurar API_PRINTAUDIT_BASE_URL / API_PRINTAUDIT_API_KEY."
            )

        try:
            response = requests.get(
                f"{self.base_url}/api/meters",
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise IntegrationConnectionError(f"PrintAudit: fallo de conexión — {exc}") from exc
        except ValueError as exc:
            raise IntegrationConnectionError(
                f"PrintAudit: respuesta no es JSON válido — {exc}"
            ) from exc

        lecturas: dict[str, LecturaExterna] = {}
        for item in payload.get("meters", []):
            parsed = self._parse_item(item)
            if parsed is not None:
                lecturas[parsed.id_externo] = parsed
        return lecturas

    def _parse_item(self, item: dict) -> LecturaExterna | None:
        id_externo = item.get("serial")
        if not id_externo:
            return None
        try:
            return LecturaExterna(
                id_externo=str(id_externo),
                lectura_bn=int(item["mono_count"]),
                lectura_color=int(item["color_count"]),
                fecha=date.today(),
                raw=item,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "PrintAudit: registro descartado por formato inválido (serial=%s): %s",
                id_externo,
                exc,
            )
            return None

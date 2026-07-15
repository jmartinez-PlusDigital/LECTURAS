import logging
from datetime import date

import requests
from django.conf import settings

from .base import BaseLecturaClient, LecturaExterna
from .exceptions import IntegrationConnectionError

logger = logging.getLogger(__name__)

TOKEN_URL = "https://identity.3manager.com/token"


class Cliente3Manager(BaseLecturaClient):
    """Cliente de la API pública de 3-Manager (documentación oficial v1.2).

    Autenticación: OAuth2 "password grant" contra `TOKEN_URL`, con el cuerpo
    en application/x-www-form-urlencoded (NO json). El token dura 5 minutos,
    así que se pide uno nuevo en cada corrida de `obtener_lecturas()` — el
    job de sincronización corre una vez al día, no hace falta cachearlo.

    El `accountId` de 3-Manager es específico por cliente nuestro (no global
    del sistema), así que se recorre cada `Cliente.id_cuenta_3manager`
    configurado y se combinan los dispositivos de todas las cuentas.

    Cada dispositivo se indexa tanto por `serialNumber` como por `deviceId`,
    para poder emparejar por lo que ya tengamos en `Equipo` (`numero_serie`
    o `id_externo_3manager`, lo que esté disponible).

    ADVERTENCIA: la documentación de 3-Manager no muestra los nombres JSON
    exactos de los contadores (solo etiquetas legibles: "Total BW", "Total
    Color"). Se asume aquí `totalBW`/`totalColor` siguiendo el patrón
    camelCase del resto de la respuesta — esto debe confirmarse con una
    llamada real antes de confiar en el dato en producción. Ver también
    `API_3MANAGER_DEVICES_PATH`: la documentación es inconsistente entre
    "/devices" (tabla "Overview of API functionality") y "/equipment"
    (captura de pantalla de ejemplo) — configurable por si el real difiere.
    """

    origen = "api_3manager"

    def __init__(
        self,
        base_url=None,
        devices_path=None,
        tenant=None,
        username=None,
        password=None,
        timeout=None,
    ):
        self.base_url = (base_url or settings.API_3MANAGER_BASE_URL or "").rstrip("/")
        self.devices_path = devices_path or settings.API_3MANAGER_DEVICES_PATH
        self.tenant = tenant or settings.API_3MANAGER_TENANT
        self.username = username or settings.API_3MANAGER_USERNAME
        self.password = password or settings.API_3MANAGER_PASSWORD
        self.timeout = timeout or settings.API_INTEGRATIONS_TIMEOUT

    def obtener_lecturas(self) -> dict[str, LecturaExterna]:
        if not (self.base_url and self.tenant and self.username and self.password):
            raise IntegrationConnectionError(
                "3-Manager: falta configurar API_3MANAGER_TENANT / "
                "API_3MANAGER_USERNAME / API_3MANAGER_PASSWORD."
            )

        from core.models import Cliente  # import diferido: evita ciclo integrations <-> core

        cuentas = list(
            Cliente.objects.exclude(id_cuenta_3manager="").values_list("id_cuenta_3manager", flat=True)
        )
        if not cuentas:
            logger.info("3-Manager: ningún Cliente tiene id_cuenta_3manager configurado.")
            return {}

        token = self._obtener_token()

        lecturas: dict[str, LecturaExterna] = {}
        for account_id in cuentas:
            lecturas.update(self._obtener_lecturas_cuenta(account_id, token))
        return lecturas

    def _obtener_token(self) -> str:
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "password",
                    "username": self.username,
                    "password": self.password,
                    "scope": "public",
                    "client_id": f"{self.tenant}-3m-api",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise IntegrationConnectionError(f"3-Manager: fallo al obtener token — {exc}") from exc
        except ValueError as exc:
            raise IntegrationConnectionError(
                f"3-Manager: respuesta de /token no es JSON válido — {exc}"
            ) from exc

        token = payload.get("access_token")
        if not token:
            raise IntegrationConnectionError("3-Manager: la respuesta de /token no incluyó access_token.")
        return token

    def _obtener_lecturas_cuenta(self, account_id: str, token: str) -> dict[str, LecturaExterna]:
        try:
            response = requests.get(
                f"{self.base_url}{self.devices_path.format(account_id=account_id)}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise IntegrationConnectionError(
                f"3-Manager: fallo de conexión (cuenta {account_id}) — {exc}"
            ) from exc
        except ValueError as exc:
            raise IntegrationConnectionError(
                f"3-Manager: respuesta no es JSON válido (cuenta {account_id}) — {exc}"
            ) from exc

        lecturas: dict[str, LecturaExterna] = {}
        for item in payload.get("items", []):
            lectura = self._parse_item(item)
            if lectura is None:
                continue
            serial = item.get("serialNumber")
            device_id = item.get("deviceId")
            if serial:
                lecturas[str(serial)] = lectura
            if device_id:
                lecturas[str(device_id)] = lectura
        return lecturas

    def _parse_item(self, item: dict) -> LecturaExterna | None:
        identificador = item.get("serialNumber") or item.get("deviceId")
        if not identificador:
            return None
        try:
            # Equipos solo-BN reportan totalColor=null (presente pero None), no
            # ausente, así que "or 0" es necesario: .get(clave, 0) no cubre ese caso.
            return LecturaExterna(
                id_externo=str(identificador),
                lectura_bn=int(item.get("totalBw") or 0),
                lectura_color=int(item.get("totalColor") or 0),
                fecha=date.today(),
                raw=item,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "3-Manager: registro descartado por formato inválido (%s): %s", identificador, exc
            )
            return None

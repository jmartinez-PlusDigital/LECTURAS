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
    así que se pide uno nuevo en cada llamada — no hace falta cachearlo, el
    job de sincronización corre una vez al día.

    El `accountId` de 3-Manager es específico por cliente nuestro (no global
    del sistema): se guarda en `Cliente.id_cuenta_3manager`.

    Campos de contador verificados contra la API real (cuenta Demant,
    2026-07-15): `totalBw` (con "w" minúscula) y `totalColor`. Los equipos
    solo-BN reportan `totalColor: null` (presente, no ausente) — se trata
    como 0, no se descarta el equipo.
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

    # --- lecturas (usado por sincronizar_lecturas) --------------------------

    def obtener_lecturas(self) -> dict[str, LecturaExterna]:
        self._validar_config()

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
            payload = self._get_json(
                self.devices_path.format(account_id=account_id), token, f"cuenta {account_id}"
            )
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

    # --- aprovisionamiento (usado por acciones del Admin) --------------------

    def buscar_cuentas(self, nombre_contiene: str = "") -> list[dict]:
        """Cuentas (orgunits tipo "Account") de 3-Manager, filtradas por
        substring de nombre (case-insensitive) si se da `nombre_contiene`."""
        self._validar_config()
        token = self._obtener_token()
        payload = self._get_json("/public/orgunits", token, "orgunits")
        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        cuentas = [item for item in items if item.get("type") == "Account"]
        if nombre_contiene:
            nombre_lower = nombre_contiene.lower()
            cuentas = [c for c in cuentas if nombre_lower in str(c.get("name", "")).lower()]
        return cuentas

    def listar_dispositivos(self, account_id: str) -> list[dict]:
        """Lista cruda de dispositivos de una cuenta (para crear Equipo, no
        solo lecturas)."""
        self._validar_config()
        token = self._obtener_token()
        payload = self._get_json(
            self.devices_path.format(account_id=account_id), token, f"cuenta {account_id}"
        )
        return payload.get("items", [])

    # --- helpers internos ----------------------------------------------------

    def _validar_config(self) -> None:
        if not (self.base_url and self.tenant and self.username and self.password):
            raise IntegrationConnectionError(
                "3-Manager: falta configurar API_3MANAGER_TENANT / "
                "API_3MANAGER_USERNAME / API_3MANAGER_PASSWORD."
            )

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

    def _get_json(self, path: str, token: str, contexto: str) -> dict:
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise IntegrationConnectionError(f"3-Manager: fallo de conexión ({contexto}) — {exc}") from exc
        except ValueError as exc:
            raise IntegrationConnectionError(
                f"3-Manager: respuesta no es JSON válido ({contexto}) — {exc}"
            ) from exc

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

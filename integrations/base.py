from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class LecturaExterna:
    """Lectura normalizada devuelta por cualquier cliente de integración.

    `en_linea`/`ultima_actualizacion` son opcionales: solo 3-Manager los
    reporta hoy (ver Cliente3Manager._parse_item); un cliente que no los
    conoce simplemente los deja en None, y sincronizar_lecturas no toca esos
    campos del Equipo en ese caso.
    """

    id_externo: str
    lectura_bn: int
    lectura_color: int
    fecha: date
    raw: dict = field(default_factory=dict)
    en_linea: bool | None = None
    ultima_actualizacion: datetime | None = None


class BaseLecturaClient(ABC):
    """Interfaz común que debe implementar cada cliente de API externa."""

    origen: str

    @abstractmethod
    def obtener_lecturas(self) -> dict[str, LecturaExterna]:
        """Devuelve las lecturas actuales indexadas por id_externo del equipo.

        Debe lanzar `IntegrationConnectionError` si la fuente completa no
        responde (timeout, error HTTP, autenticación inválida). Los equipos
        que la API omite silenciosamente (no vienen en la respuesta) no
        generan excepción: el llamador los detecta comparando contra los
        equipos esperados y los reporta como alerta individual.
        """
        raise NotImplementedError

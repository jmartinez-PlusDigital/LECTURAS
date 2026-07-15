from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


@dataclass
class LecturaExterna:
    """Lectura normalizada devuelta por cualquier cliente de integración."""

    id_externo: str
    lectura_bn: int
    lectura_color: int
    fecha: date
    raw: dict = field(default_factory=dict)


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

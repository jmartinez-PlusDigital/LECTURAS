from .base import BaseLecturaClient, LecturaExterna
from .exceptions import IntegrationConnectionError, IntegrationError
from .printaudit import ClientePrintAudit
from .tresmanager import Cliente3Manager

__all__ = [
    "BaseLecturaClient",
    "LecturaExterna",
    "IntegrationError",
    "IntegrationConnectionError",
    "Cliente3Manager",
    "ClientePrintAudit",
    "get_client",
]

_CLIENTES = {
    "api_3manager": Cliente3Manager,
    "api_printaudit": ClientePrintAudit,
}


def get_client(metodo_lectura: str) -> BaseLecturaClient:
    """Factory: devuelve el cliente de integración para un `metodo_lectura`.

    Lanza ValueError si `metodo_lectura` es 'manual' o no reconocido —
    las lecturas manuales no pasan por este módulo.
    """
    try:
        cliente_cls = _CLIENTES[metodo_lectura]
    except KeyError:
        raise ValueError(
            f"No hay cliente de integración para el método de lectura '{metodo_lectura}'."
        )
    return cliente_cls()

class IntegrationError(Exception):
    """Error base para fallos de integración con APIs externas de lectura."""


class IntegrationConnectionError(IntegrationError):
    """La API externa no respondió, respondió con error, o la autenticación falló.

    Este error debe aislarse por fuente: un fallo de 3-Manager no debe
    impedir procesar PrintAudit ni las lecturas manuales.
    """

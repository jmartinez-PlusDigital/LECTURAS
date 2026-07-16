"""Aprovisionamiento de Equipo a partir del listado de dispositivos de 3-Manager.

Solo crea/actualiza Equipo. NO crea Asignacion: a qué contrato, desde cuándo,
y con qué lectura de referencia arranca, es una decisión de negocio que no
puede inferirse de la API — eso se sigue capturando a mano (o vía
`importar_equipos` con CSV) una vez que el Equipo ya existe.
"""
from core.models import Cliente, Equipo, MetodoLectura
from integrations.tresmanager import Cliente3Manager


def importar_equipos_de_cliente_3manager(cliente: Cliente) -> list[dict]:
    if not cliente.id_cuenta_3manager:
        raise ValueError(f"El cliente '{cliente.nombre}' no tiene id_cuenta_3manager configurado.")

    dispositivos = Cliente3Manager().listar_dispositivos(cliente.id_cuenta_3manager)
    return [_procesar_dispositivo(item) for item in dispositivos]


def _procesar_dispositivo(item: dict) -> dict:
    resultado = {
        "numero_serie": "",
        "nombre_3manager": item.get("deviceName", ""),
        "equipo": "",
        "error": "",
    }

    numero_serie = str(item.get("serialNumber") or "").strip()
    if not numero_serie:
        resultado["error"] = "el dispositivo no reporta serialNumber en 3-Manager"
        return resultado
    resultado["numero_serie"] = numero_serie

    try:
        equipo, creado = Equipo.objects.update_or_create(
            numero_serie=numero_serie,
            defaults={
                "marca": str(item.get("manufacturer") or ""),
                "modelo": str(item.get("model") or ""),
                "metodo_lectura": MetodoLectura.API_3MANAGER,
                "id_externo_3manager": str(item.get("deviceId") or "") or None,
            },
        )
        resultado["equipo"] = "creado" if creado else "actualizado"
    except Exception as exc:  # noqa: BLE001 - se aísla el error por dispositivo a propósito
        resultado["error"] = str(exc)
    return resultado

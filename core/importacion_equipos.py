"""Lógica compartida de importación masiva de equipos (CSV o Excel).

Usada tanto por el management command `importar_equipos` (terminal) como por
la vista del Admin en `core/admin.py` (subida de archivo desde el navegador),
para que ambos caminos se comporten exactamente igual y solo se prueben una vez.

Columnas esperadas (encabezados exactos, en cualquier orden):
    numero_serie            (requerido, único)
    marca
    modelo
    metodo_lectura           manual | api_3manager | api_printaudit (default: manual)
    id_externo_3manager      (opcional)
    id_externo_printaudit    (opcional)
    permite_reset_contador   true/false, si/no, 1/0 (default: false)
    tope_contador            (opcional, entero)
    numero_contrato          (opcional: si se da, crea la asignación inicial)
    fecha_inicio_asignacion  YYYY-MM-DD (requerido si numero_contrato está presente)
    lectura_inicial_bn       (requerido si numero_contrato está presente)
    lectura_inicial_color    (requerido si numero_contrato está presente)
    ip_red_cliente           (opcional)
    ubicacion                (opcional: departamento/área donde está instalado, p. ej. "Almacén")

Un equipo sin numero_contrato se crea con estado_actual='sin_asignar' (en
bodega). Si se da numero_contrato y el equipo no tiene ya una asignación
activa, se crea la Asignacion y el equipo pasa a estado_actual='activo'.

Cada fila se procesa de forma aislada (un error en una fila no detiene el
resto). `dry_run=True` valida todo pero no persiste nada.
"""
from django.db import transaction

from core.importacion_comun import escribir_reporte_csv as _escribir_reporte_csv
from core.importacion_comun import booleano, entero, fecha, procesar_archivo, texto
from core.models import Asignacion, Contrato, Equipo, MetodoLectura

COLUMNAS_REPORTE = ["fila", "numero_serie", "equipo", "asignacion", "error"]


def procesar_archivo_equipos(archivo, nombre_archivo: str, *, dry_run: bool = False) -> list[dict]:
    """Procesa un archivo de equipos y devuelve el resultado por fila.

    `archivo` puede ser una ruta (str) o un objeto tipo archivo (p. ej. un
    `UploadedFile` de Django) abierto en modo binario. `nombre_archivo` se usa
    solo para decidir el formato por su extensión.
    """
    return procesar_archivo(archivo, nombre_archivo, _procesar_fila, dry_run=dry_run)


def resumen_de(resultados: list[dict]) -> dict:
    con_error = [r for r in resultados if r["error"]]
    return {
        "total": len(resultados),
        "equipos_creados": sum(1 for r in resultados if r["equipo"] == "creado"),
        "equipos_actualizados": sum(1 for r in resultados if r["equipo"] == "actualizado"),
        "asignaciones_creadas": sum(1 for r in resultados if r["asignacion"].startswith("creada")),
        "con_error": con_error,
    }


def escribir_reporte_csv(destino, resultados: list[dict]) -> None:
    """`destino` puede ser una ruta (str) o un objeto tipo archivo en modo texto."""
    _escribir_reporte_csv(destino, resultados, COLUMNAS_REPORTE)


# --- procesamiento por fila ----------------------------------------------

def _procesar_fila(numero_fila: int, fila: dict) -> dict:
    resultado = {"fila": numero_fila, "numero_serie": "", "equipo": "", "asignacion": "", "error": ""}
    try:
        with transaction.atomic():
            numero_serie = texto(fila.get("numero_serie"))
            if not numero_serie:
                raise ValueError("numero_serie es requerido")
            resultado["numero_serie"] = numero_serie

            metodo_lectura = texto(fila.get("metodo_lectura")) or MetodoLectura.MANUAL
            if metodo_lectura not in MetodoLectura.values:
                raise ValueError(f"metodo_lectura inválido: '{metodo_lectura}'")

            equipo, creado = Equipo.objects.update_or_create(
                numero_serie=numero_serie,
                defaults={
                    "marca": texto(fila.get("marca")),
                    "modelo": texto(fila.get("modelo")),
                    "metodo_lectura": metodo_lectura,
                    "id_externo_3manager": texto(fila.get("id_externo_3manager")) or None,
                    "id_externo_printaudit": texto(fila.get("id_externo_printaudit")) or None,
                    "permite_reset_contador": booleano(fila.get("permite_reset_contador")),
                    "tope_contador": entero(fila.get("tope_contador")),
                },
            )
            resultado["equipo"] = "creado" if creado else "actualizado"

            numero_contrato = texto(fila.get("numero_contrato"))
            if not numero_contrato:
                resultado["asignacion"] = "sin contrato (sin_asignar)"
                return resultado

            resultado["asignacion"] = asignar_equipo_a_contrato(equipo, numero_contrato, fila)
    except Exception as exc:  # noqa: BLE001 - se aísla el error por fila a propósito
        # La excepción hizo rollback de todo el bloque atomic() de esta fila
        # (incluyendo el update_or_create del equipo), así que el resultado
        # parcial que se haya escrito arriba ya no refleja el estado real.
        resultado["equipo"] = ""
        resultado["asignacion"] = ""
        resultado["error"] = str(exc)
    return resultado


def asignar_equipo_a_contrato(equipo: Equipo, numero_contrato: str, fila: dict) -> str:
    """Crea la Asignacion de `equipo` a `numero_contrato` a partir de una fila
    con `fecha_inicio_asignacion`, `lectura_inicial_bn`, `lectura_inicial_color`
    y opcionalmente `ip_red_cliente`. Pública porque también la usa la acción
    "Asignar a un contrato" del Admin (ver EquipoAdmin.asignar_masivo_view en
    core/admin.py), no solo el importador CSV."""
    try:
        contrato = Contrato.objects.get(numero_contrato=numero_contrato)
    except Contrato.DoesNotExist:
        raise ValueError(f"contrato '{numero_contrato}' no existe")

    asignacion_activa = Asignacion.objects.filter(equipo=equipo, fecha_fin__isnull=True).first()
    if asignacion_activa is not None:
        if asignacion_activa.contrato_id == contrato.id:
            return "ya estaba asignado a este contrato"
        raise ValueError(
            f"el equipo ya tiene una asignación activa al contrato "
            f"'{asignacion_activa.contrato.numero_contrato}'; ciérrala manualmente antes de reasignar"
        )

    fecha_inicio = fecha(fila.get("fecha_inicio_asignacion"))
    if fecha_inicio is None:
        raise ValueError("fecha_inicio_asignacion es requerida y debe tener formato YYYY-MM-DD")

    lectura_bn = entero(fila.get("lectura_inicial_bn"))
    lectura_color = entero(fila.get("lectura_inicial_color"))
    if lectura_bn is None or lectura_color is None:
        raise ValueError("lectura_inicial_bn y lectura_inicial_color son requeridas")

    Asignacion.objects.create(
        equipo=equipo,
        contrato=contrato,
        fecha_inicio=fecha_inicio,
        lectura_inicial_referencia_bn=lectura_bn,
        lectura_inicial_referencia_color=lectura_color,
        ip_red_cliente=texto(fila.get("ip_red_cliente")) or None,
        ubicacion=texto(fila.get("ubicacion")),
    )
    equipo.estado_actual = Equipo.EstadoActual.ACTIVO
    equipo.save(update_fields=["estado_actual"])
    return f"creada (contrato {numero_contrato})"

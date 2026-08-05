"""Importación masiva de lecturas (CSV o Excel) y generación de plantillas
pre-llenadas por contrato.

Usado por la vista del Admin en `core/admin.py`: (a) descargar, para uno o
varios contratos, una plantilla ya con los números de serie de sus equipos
activos, y (b) subir de vuelta un archivo con las lecturas capturadas para
cargarlas en batch, sin escribirlas una por una.

Columnas esperadas del archivo de carga (encabezados exactos, en cualquier
orden):
    numero_serie      (requerido) identifica el Equipo
    fecha             (opcional, YYYY-MM-DD; si se omite se usa hoy)
    lectura_bn        (requerido)
    lectura_color     (requerido)
    numero_contrato   (opcional: si se da, se valida que coincida con el
                       contrato de la asignación activa del equipo)

Cada equipo se resuelve a su asignación ACTIVA (fecha_fin is null) — ahí se
cuelga la Lectura, con origen='manual' sin importar el metodo_lectura
configurado del equipo (este importador es el atajo para captura manual
masiva). Si ya existe una lectura para esa asignación+fecha, se actualiza en
vez de fallar, para poder recargar un archivo corregido sin duplicar.

Cada fila se procesa de forma aislada (un error en una fila no detiene el
resto). `dry_run=True` valida todo pero no persiste nada.
"""
import csv
import io

from django.db import transaction
from django.utils import timezone

from core.importacion_comun import escribir_reporte_csv as _escribir_reporte_csv
from core.importacion_comun import entero, fecha, procesar_archivo, texto
from core.models import Asignacion, Contrato, Equipo, Lectura, MetodoLectura

COLUMNAS_REPORTE = ["fila", "numero_serie", "fecha", "lectura", "advertencia", "error"]


def procesar_archivo_lecturas(archivo, nombre_archivo: str, *, dry_run: bool = False) -> list[dict]:
    """Procesa un archivo de lecturas y devuelve el resultado por fila.

    `archivo` puede ser una ruta (str) o un objeto tipo archivo (p. ej. un
    `UploadedFile` de Django) abierto en modo binario. `nombre_archivo` se usa
    solo para decidir el formato por su extensión.
    """
    return procesar_archivo(archivo, nombre_archivo, _procesar_fila, dry_run=dry_run)


def resumen_de(resultados: list[dict]) -> dict:
    con_error = [r for r in resultados if r["error"]]
    con_advertencia = [r for r in resultados if r["advertencia"]]
    return {
        "total": len(resultados),
        "creadas": sum(1 for r in resultados if r["lectura"] == "creada"),
        "actualizadas": sum(1 for r in resultados if r["lectura"] == "actualizada"),
        "con_error": con_error,
        "con_advertencia": con_advertencia,
    }


def escribir_reporte_csv(destino, resultados: list[dict]) -> None:
    """`destino` puede ser una ruta (str) o un objeto tipo archivo en modo texto."""
    _escribir_reporte_csv(destino, resultados, COLUMNAS_REPORTE)


# --- plantilla pre-llenada por contrato -----------------------------------

def generar_plantilla_lecturas(contratos) -> bytes:
    """CSV pre-llenado con los equipos activos de uno o varios contratos.

    `contratos` puede ser un `Contrato` o un iterable de `Contrato`. Las
    columnas `marca_modelo` y `ultima_lectura_*` son solo de referencia (se
    ignoran al volver a subir el archivo); `numero_contrato` viaja pre-llenado
    para que `procesar_archivo_lecturas` pueda usarlo como validación cruzada.
    """
    if isinstance(contratos, Contrato):
        contratos = [contratos]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "numero_serie",
            "marca_modelo",
            "numero_contrato",
            "ultima_lectura_bn",
            "ultima_lectura_color",
            "fecha",
            "lectura_bn",
            "lectura_color",
        ]
    )

    hoy = timezone.localdate().isoformat()
    asignaciones = (
        Asignacion.objects.filter(contrato__in=contratos, fecha_fin__isnull=True)
        .select_related("equipo", "contrato")
        .order_by("contrato__numero_contrato", "equipo__numero_serie")
    )
    for asignacion in asignaciones:
        ultima_lectura = Lectura.objects.filter(asignacion=asignacion).order_by("-fecha").first()
        writer.writerow(
            [
                asignacion.equipo.numero_serie,
                f"{asignacion.equipo.marca} {asignacion.equipo.modelo}",
                asignacion.contrato.numero_contrato,
                ultima_lectura.lectura_bn if ultima_lectura else "(sin lecturas previas)",
                ultima_lectura.lectura_color if ultima_lectura else "",
                hoy,
                "",
                "",
            ]
        )

    return buffer.getvalue().encode("utf-8-sig")


# --- procesamiento por fila ------------------------------------------------

def _procesar_fila(numero_fila: int, fila: dict) -> dict:
    resultado = {
        "fila": numero_fila,
        "numero_serie": "",
        "fecha": "",
        "lectura": "",
        "advertencia": "",
        "error": "",
    }
    try:
        with transaction.atomic():
            numero_serie = texto(fila.get("numero_serie"))
            if not numero_serie:
                raise ValueError("numero_serie es requerido")
            resultado["numero_serie"] = numero_serie

            try:
                equipo = Equipo.objects.get(numero_serie=numero_serie)
            except Equipo.DoesNotExist:
                raise ValueError(f"no existe un equipo con numero_serie '{numero_serie}'")

            asignacion = (
                Asignacion.objects.filter(equipo=equipo, fecha_fin__isnull=True)
                .select_related("contrato")
                .first()
            )
            if asignacion is None:
                raise ValueError("el equipo no tiene una asignación activa")

            numero_contrato = texto(fila.get("numero_contrato"))
            if numero_contrato and numero_contrato != asignacion.contrato.numero_contrato:
                raise ValueError(
                    f"el equipo está asignado al contrato '{asignacion.contrato.numero_contrato}', "
                    f"no a '{numero_contrato}'"
                )

            fecha_lectura = fecha(fila.get("fecha")) or timezone.localdate()
            if fecha_lectura < asignacion.fecha_inicio:
                raise ValueError(
                    f"la fecha {fecha_lectura} es anterior al inicio de la asignación ({asignacion.fecha_inicio})"
                )
            resultado["fecha"] = str(fecha_lectura)

            lectura_bn = entero(fila.get("lectura_bn"))
            lectura_color = entero(fila.get("lectura_color"))
            if lectura_bn is None or lectura_color is None:
                raise ValueError("lectura_bn y lectura_color son requeridas")

            lectura, creada = Lectura.objects.update_or_create(
                asignacion=asignacion,
                fecha=fecha_lectura,
                defaults={
                    "lectura_bn": lectura_bn,
                    "lectura_color": lectura_color,
                    "origen": MetodoLectura.MANUAL,
                },
            )
            resultado["lectura"] = "creada" if creada else "actualizada"
            if lectura.estado_auditoria == Lectura.EstadoAuditoria.ALERTA:
                mensajes = [a["mensaje"] for a in lectura.detalle_auditoria.get("advertencias", [])]
                resultado["advertencia"] = " ".join(mensajes)
    except Exception as exc:  # noqa: BLE001 - se aísla el error por fila a propósito
        resultado["lectura"] = ""
        resultado["error"] = str(exc)
    return resultado

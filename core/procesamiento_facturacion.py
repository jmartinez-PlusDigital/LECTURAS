"""Lógica del ciclo de facturación de un contrato, compartida por el comando
`procesar_facturacion_diaria` y por la acción "Facturar ahora" del Admin
(ver `ContratoAdmin`).

`procesar_contrato` aísla cualquier error a nivel de un solo contrato: nunca
lanza, siempre devuelve un dict con el resultado para que el llamador pueda
procesar varios contratos sin que uno detenga a los demás.
"""
from datetime import date, timedelta

from django.conf import settings
from django.core import management
from django.db import transaction
from django.utils import timezone

from auditoria import auditar_contrato
from core.models import Contrato, Factura, Lectura, LogEjecucion
from documentos import generar_excel_factura, generar_pdf_factura
from facturacion import calcular_factura, persistir_factura


def generar_snapshot(hoy: date) -> str:
    """Respaldo completo de la base de datos antes de facturar (dumpdata)."""
    backups_dir = settings.BASE_DIR / "backups"
    backups_dir.mkdir(exist_ok=True)
    ruta = backups_dir / f"snapshot_{hoy.isoformat()}_{timezone.localtime():%H%M%S}.json"
    try:
        with open(ruta, "w", encoding="utf-8") as archivo:
            management.call_command(
                "dumpdata",
                exclude=["contenttypes", "auth.permission", "admin.logentry", "sessions.session"],
                natural_foreign=True,
                stdout=archivo,
            )
    except Exception as exc:
        LogEjecucion.objects.create(
            estado=LogEjecucion.Estado.ERROR, detalle={"fase": "snapshot", "error": str(exc)}
        )
        raise
    return str(ruta)


def procesar_contrato(contrato: Contrato, hoy: date) -> dict:
    fecha_inicio, fecha_fin = _calcular_periodo(contrato, hoy)
    periodo_mes, periodo_anio = hoy.month, hoy.year

    try:
        resultado_auditoria = auditar_contrato(contrato, fecha_inicio, fecha_fin)
    except Exception as exc:
        return _registrar_error(contrato, "auditoria", exc)

    if not resultado_auditoria.aprobado:
        LogEjecucion.objects.create(
            contrato=contrato,
            estado=LogEjecucion.Estado.PENDIENTE,
            detalle={"fase": "auditoria", **resultado_auditoria.to_dict()},
        )
        return {"contrato": contrato.numero_contrato, "estado": "pendiente", "detalle": "alertas de auditoría sin resolver"}

    try:
        resultado_calculo = calcular_factura(
            contrato, fecha_inicio, fecha_fin, periodo_mes, periodo_anio, simulacion=True
        )
        pdf_bytes = generar_pdf_factura(resultado_calculo, contrato)
        excel_bytes = generar_excel_factura(resultado_calculo, contrato)
    except Exception as exc:
        return _registrar_error(contrato, "calculo_o_documentos", exc)

    with transaction.atomic():
        asignaciones_ids = [c.asignacion_id for c in resultado_calculo.consumo_por_equipo]
        Lectura.objects.filter(
            asignacion_id__in=asignaciones_ids, fecha__gte=fecha_inicio, fecha__lte=fecha_fin
        ).update(estado_auditoria=Lectura.EstadoAuditoria.OK)

        factura = persistir_factura(
            resultado_calculo, pdf_bytes=pdf_bytes, excel_bytes=excel_bytes, estado=Factura.Estado.OK
        )

        detalle = {"fase": "completo", "factura_id": factura.id, "monto_total": str(factura.monto_total)}
        if resultado_auditoria.alertas:
            # Solo llegan aquí alertas no bloqueantes (candado f): el contrato ya
            # pasó `resultado_auditoria.aprobado` arriba. Antes se descartaban en
            # silencio; ahora quedan en el log para revisión humana (ver
            # core.dashboard._advertencias_consumo_recientes).
            detalle["advertencias"] = _advertencias_de(resultado_auditoria)

        LogEjecucion.objects.create(
            contrato=contrato,
            estado=LogEjecucion.Estado.OK,
            detalle=detalle,
            enlaces_generados={"pdf": factura.pdf_archivo.url, "excel": factura.excel_archivo.url},
        )

    return {"contrato": contrato.numero_contrato, "estado": "ok", "detalle": f"factura {factura.id}"}


def _advertencias_de(resultado_auditoria) -> list[dict]:
    return [
        {"candado": a.candado, "equipo": a.equipo_numero_serie, "mensaje": a.mensaje, "datos": a.datos}
        for a in resultado_auditoria.alertas
    ]


def _registrar_error(contrato: Contrato, fase: str, exc: Exception) -> dict:
    LogEjecucion.objects.create(
        contrato=contrato,
        estado=LogEjecucion.Estado.ERROR,
        detalle={"fase": fase, "error": str(exc)},
    )
    return {"contrato": contrato.numero_contrato, "estado": "error", "detalle": str(exc)}


def _calcular_periodo(contrato: Contrato, hoy: date) -> tuple[date, date]:
    """El periodo siempre arranca justo donde terminó la última factura real.

    Antes se recalculaba el inicio a partir de `dia_corte_facturacion` aplicado
    al mes previo a `hoy`, lo cual solo daba periodos contiguos si cada factura
    se corría exactamente el día de corte. Si se forzaba en otra fecha (p. ej.
    --contrato/--fecha, o la acción "Facturar ahora" del Admin), quedaba un
    hueco de lecturas sin facturar entre el fin real de la última factura y el
    inicio recalculado. Anclarse a `Factura.fecha_fin` elimina ese hueco sin
    importar qué día se ejecute.
    """
    ultima_factura = contrato.facturas.order_by("-periodo_anio", "-periodo_mes").first()
    if ultima_factura is not None and ultima_factura.fecha_fin is not None:
        fecha_inicio = ultima_factura.fecha_fin + timedelta(days=1)
    else:
        fecha_inicio = contrato.fecha_inicio
    return max(fecha_inicio, contrato.fecha_inicio), hoy

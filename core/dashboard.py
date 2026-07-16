"""Datos del panel de inicio del Admin (dashboard).

Reemplaza el listado plano de modelos que muestra Jazzmin por defecto con lo
que de verdad se necesita revisar cada día: contratos por facturar hoy,
alertas de auditoría pendientes y facturas recientes. Ver
`core/templates/admin/index.html` y `core/admin.py` (vista `dashboard_view`).
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from core.models import Asignacion, Contrato, Factura, LogEjecucion


def contexto_dashboard() -> dict:
    hoy = timezone.localdate()

    contratos_hoy = list(
        Contrato.objects.filter(estado=Contrato.Estado.ACTIVO, dia_corte_facturacion=hoy.day)
        .select_related("cliente")
        .order_by("numero_contrato")
    )
    for contrato in contratos_hoy:
        contrato.ultima_factura = (
            Factura.objects.filter(contrato=contrato).order_by("-periodo_anio", "-periodo_mes").first()
        )
        contrato.equipos_activos = Asignacion.objects.filter(
            contrato=contrato, fecha_fin__isnull=True
        ).count()

    alertas_pendientes = _alertas_pendientes_activas()

    facturas_recientes = list(
        Factura.objects.select_related("contrato", "contrato__cliente").order_by("-fecha_generacion")[:8]
    )

    facturado_mes = Factura.objects.filter(
        periodo_mes=hoy.month, periodo_anio=hoy.year, estado=Factura.Estado.OK
    ).aggregate(total=Sum("monto_total"))["total"] or Decimal("0")
    facturas_mes_count = Factura.objects.filter(
        periodo_mes=hoy.month, periodo_anio=hoy.year, estado=Factura.Estado.OK
    ).count()

    return {
        "hoy": hoy,
        "contratos_activos_count": Contrato.objects.filter(estado=Contrato.Estado.ACTIVO).count(),
        "contratos_hoy": contratos_hoy,
        "alertas_pendientes": alertas_pendientes,
        "facturas_recientes": facturas_recientes,
        "facturado_mes": facturado_mes,
        "facturas_mes_count": facturas_mes_count,
    }


def _alertas_pendientes_activas(dias: int = 90) -> list[LogEjecucion]:
    """El LogEjecucion más reciente por contrato (dentro de los últimos `dias`
    días) cuando ese estado más reciente sigue siendo PENDIENTE — si el
    contrato ya se facturó después, esa alerta vieja ya no aplica."""
    desde = timezone.now() - timedelta(days=dias)
    logs = (
        LogEjecucion.objects.filter(contrato__isnull=False, timestamp__gte=desde)
        .select_related("contrato", "contrato__cliente")
        .order_by("-timestamp")
    )

    ultimo_por_contrato: dict[int, LogEjecucion] = {}
    for log in logs:
        ultimo_por_contrato.setdefault(log.contrato_id, log)

    pendientes = [log for log in ultimo_por_contrato.values() if log.estado == LogEjecucion.Estado.PENDIENTE]

    for log in pendientes:
        alertas = (log.detalle or {}).get("alertas", [])
        bloqueantes = [a for a in alertas if a.get("bloqueante")]
        log.alertas_bloqueantes = bloqueantes

    pendientes.sort(key=lambda log: log.timestamp, reverse=True)
    return pendientes

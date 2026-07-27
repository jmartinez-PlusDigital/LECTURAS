"""Datos del panel de inicio del Admin (dashboard).

Reemplaza el listado plano de modelos que muestra Jazzmin por defecto con lo
que de verdad se necesita revisar cada día: contratos por facturar hoy,
alertas de auditoría pendientes y facturas recientes. Ver
`core/templates/admin/index.html` y `core/admin.py` (vista `dashboard_view`).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from core.models import Asignacion, Contrato, Factura, Lectura, LogEjecucion

MESES_CORTOS = [
    "", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
]


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
    contratos_sin_lectura = _contratos_sin_lectura_proximos(hoy)
    contratos_por_vencer = _contratos_por_vencer(hoy)
    advertencias_consumo = _advertencias_consumo_recientes()

    facturas_recientes = list(
        Factura.objects.select_related("contrato", "contrato__cliente").order_by("-fecha_generacion")[:8]
    )

    facturado_mes_por_moneda = _facturado_mes_por_moneda(hoy)

    return {
        "hoy": hoy,
        "contratos_activos_count": Contrato.objects.filter(estado=Contrato.Estado.ACTIVO).count(),
        "contratos_hoy": contratos_hoy,
        "alertas_pendientes": alertas_pendientes,
        "contratos_sin_lectura": contratos_sin_lectura,
        "contratos_por_vencer": contratos_por_vencer,
        "alertas_operativas_count": len(contratos_sin_lectura) + len(contratos_por_vencer),
        "advertencias_consumo": advertencias_consumo,
        "facturas_recientes": facturas_recientes,
        "facturado_mes_por_moneda": facturado_mes_por_moneda,
        "tendencia_facturacion": facturacion_mensual(),
        "tendencia_excedente": excedente_mensual(),
    }


def _ultimos_n_meses(hoy: date, n: int) -> list[tuple[int, int]]:
    """Los últimos `n` periodos (año, mes), en orden ascendente, incluyendo el
    mes en curso."""
    meses = []
    anio, mes = hoy.year, hoy.month
    for _ in range(n):
        meses.append((anio, mes))
        mes -= 1
        if mes == 0:
            mes = 12
            anio -= 1
    meses.reverse()
    return meses


def _q_ultimos_meses(periodos: list[tuple[int, int]]) -> Q:
    primero_anio, primero_mes = periodos[0]
    return Q(periodo_anio__gt=primero_anio) | Q(periodo_anio=primero_anio, periodo_mes__gte=primero_mes)


def facturacion_mensual(meses: int = 12, hoy: date | None = None) -> list[dict]:
    """Una serie por moneda: sumar MXN y USD en una sola barra no tendría
    sentido sin un tipo de cambio, así que cada moneda se grafica aparte."""
    hoy = hoy or timezone.localdate()
    periodos = _ultimos_n_meses(hoy, meses)

    filas = (
        Factura.objects.filter(estado=Factura.Estado.OK)
        .filter(_q_ultimos_meses(periodos))
        .values("periodo_anio", "periodo_mes", "moneda")
        .annotate(total=Sum("monto_total"))
    )
    por_periodo_moneda = {(f["periodo_anio"], f["periodo_mes"], f["moneda"]): f["total"] for f in filas}
    monedas = sorted({f["moneda"] for f in filas})

    series = []
    for moneda in monedas:
        valores = [por_periodo_moneda.get((anio, mes, moneda)) or Decimal("0") for anio, mes in periodos]
        maximo = max(valores) or Decimal("1")
        puntos = [
            {
                "etiqueta": f"{MESES_CORTOS[mes]} {anio}",
                "valor": valor,
                "pct": float(valor / maximo * 100),
            }
            for (anio, mes), valor in zip(periodos, valores)
        ]
        series.append({"moneda": moneda, "puntos": puntos})
    return series


def excedente_mensual(meses: int = 12, hoy: date | None = None) -> list[dict]:
    """Excedente de copias facturado por mes (no el consumo total: solo lo
    que rebasó lo incluido en cada contrato, que es el dato que ya se guarda
    por factura)."""
    hoy = hoy or timezone.localdate()
    periodos = _ultimos_n_meses(hoy, meses)

    filas = (
        Factura.objects.filter(estado=Factura.Estado.OK)
        .filter(_q_ultimos_meses(periodos))
        .values("periodo_anio", "periodo_mes")
        .annotate(bn=Sum("consumo_excedente_bn"), color=Sum("consumo_excedente_color"))
    )
    por_periodo = {(f["periodo_anio"], f["periodo_mes"]): f for f in filas}

    valores_bn = [por_periodo.get((anio, mes), {}).get("bn") or 0 for anio, mes in periodos]
    valores_color = [por_periodo.get((anio, mes), {}).get("color") or 0 for anio, mes in periodos]
    maximo = max(valores_bn + valores_color) or 1

    return [
        {
            "etiqueta": f"{MESES_CORTOS[mes]} {anio}",
            "bn": bn,
            "color": color,
            "pct_bn": bn / maximo * 100,
            "pct_color": color / maximo * 100,
        }
        for (anio, mes), bn, color in zip(periodos, valores_bn, valores_color)
    ]


def _facturado_mes_por_moneda(hoy) -> list[dict]:
    """No se suman montos de distintas monedas entre sí: un total en pesos y
    otro en dólares no son comparables sin una tasa de cambio, así que se
    reportan por separado, una tarjeta de KPI por moneda con facturas este mes."""
    filas = (
        Factura.objects.filter(periodo_mes=hoy.month, periodo_anio=hoy.year, estado=Factura.Estado.OK)
        .values("moneda")
        .annotate(total=Sum("monto_total"), cantidad=Count("id"))
        .order_by("moneda")
    )
    return [
        {"moneda": fila["moneda"], "total": fila["total"] or Decimal("0"), "cantidad": fila["cantidad"]}
        for fila in filas
    ]


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


def _proximo_corte(contrato: Contrato, hoy: date) -> date:
    """Próxima fecha (>= hoy) en la que cae el día de corte del contrato.
    Si el mes en curso no tiene ese día (p. ej. corte 31 en febrero), usa el
    último día del mes en vez de tronar."""

    def _fecha_de_corte(anio: int, mes: int) -> date:
        try:
            return date(anio, mes, contrato.dia_corte_facturacion)
        except ValueError:
            siguiente = date(anio + (mes == 12), (mes % 12) + 1, 1)
            return siguiente - timedelta(days=1)

    candidato = _fecha_de_corte(hoy.year, hoy.month)
    if candidato >= hoy:
        return candidato
    mes_sig, anio_sig = (1, hoy.year + 1) if hoy.month == 12 else (hoy.month + 1, hoy.year)
    return _fecha_de_corte(anio_sig, mes_sig)


def _contratos_sin_lectura_proximos(hoy: date, dias_anticipacion: int = 5) -> list[Contrato]:
    """Contratos activos cuyo próximo corte cae dentro de los próximos
    `dias_anticipacion` días y que todavía no tienen ninguna lectura
    capturada este mes calendario — para avisar con tiempo, antes de que el
    corte llegue y no haya con qué facturar."""
    limite = hoy + timedelta(days=dias_anticipacion)
    resultado = []
    for contrato in Contrato.objects.filter(estado=Contrato.Estado.ACTIVO).select_related("cliente"):
        proximo = _proximo_corte(contrato, hoy)
        if not (hoy <= proximo <= limite):
            continue
        tiene_lectura = Lectura.objects.filter(
            asignacion__contrato=contrato,
            asignacion__fecha_fin__isnull=True,
            fecha__year=hoy.year,
            fecha__month=hoy.month,
        ).exists()
        if not tiene_lectura:
            contrato.proximo_corte = proximo
            resultado.append(contrato)
    resultado.sort(key=lambda c: c.proximo_corte)
    return resultado


def _contratos_por_vencer(hoy: date, dias: int = 30) -> list[Contrato]:
    """Contratos activos con fecha_fin dentro de los próximos `dias` días."""
    limite = hoy + timedelta(days=dias)
    return list(
        Contrato.objects.filter(
            estado=Contrato.Estado.ACTIVO, fecha_fin__isnull=False, fecha_fin__gte=hoy, fecha_fin__lte=limite
        )
        .select_related("cliente")
        .order_by("fecha_fin")
    )


def _advertencias_consumo_recientes(dias: int = 30) -> list[dict]:
    """Facturas aprobadas y generadas con alertas de consumo no bloqueantes
    (candado f de auditoria/motor.py) — antes se descartaban en silencio al
    facturar (ver core.procesamiento_facturacion.procesar_contrato), ahora
    quedan aquí para revisión humana, sin haber bloqueado la factura."""
    desde = timezone.now() - timedelta(days=dias)
    logs = (
        LogEjecucion.objects.filter(
            estado=LogEjecucion.Estado.OK, timestamp__gte=desde, detalle__has_key="advertencias"
        )
        .select_related("contrato", "contrato__cliente")
        .order_by("-timestamp")
    )
    filas = []
    for log in logs:
        for advertencia in log.detalle.get("advertencias", []):
            filas.append(
                {
                    "contrato": log.contrato,
                    "timestamp": log.timestamp,
                    "equipo": advertencia.get("equipo"),
                    "mensaje": advertencia.get("mensaje"),
                }
            )
    return filas

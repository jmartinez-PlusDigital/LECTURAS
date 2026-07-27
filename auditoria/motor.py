"""Motor de auditoría: valida los 7 candados de consumo para un contrato
en un rango de fechas (normalmente un periodo de facturación).

Candados:
    a) Lectura invertida sin rollover configurado -> bloquea.
    b) Rollover de contador (aplicado automáticamente en core.calculo_consumo,
       no genera alerta si el equipo lo tiene configurado correctamente).
    c) Equipo omitido (asignación activa sin ninguna lectura en el periodo) -> bloquea.
    d) Equipo en cero (lectura_bn == 0 y lectura_color == 0) -> bloquea, requiere confirmación.
    e) Equipo huérfano (lectura fuera de la vigencia de su propia asignación) -> bloquea.
    f) Anomalía estadística (consumo > factor_anomalia x promedio histórico) -> NO bloquea.
    g) Falla de sincronización (equipo API sin lectura reciente) -> bloquea.

Si el ResultadoAuditoria tiene alguna alerta bloqueante, el contrato debe
tratarse como PENDIENTE en el ciclo (no se genera factura automáticamente).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q

from core.calculo_consumo import (
    FACTOR_ANOMALIA_DEFAULT,
    MESES_HISTORIAL_DEFAULT,
    calcular_consumo,
    promedio_historico_mensual,
)
from core.models import Asignacion, Lectura, MetodoLectura

from .tipos import AlertaAuditoria, ResultadoAuditoria

DIAS_FALLA_SINCRONIZACION_DEFAULT = 5


def auditar_contrato(
    contrato,
    fecha_inicio: date,
    fecha_fin: date,
    *,
    dias_falla_sincronizacion: int = DIAS_FALLA_SINCRONIZACION_DEFAULT,
    meses_historial: int = MESES_HISTORIAL_DEFAULT,
    factor_anomalia: Decimal = FACTOR_ANOMALIA_DEFAULT,
) -> ResultadoAuditoria:
    resultado = ResultadoAuditoria(contrato_id=contrato.id, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin)

    asignaciones_vigentes = list(
        Asignacion.objects.filter(contrato=contrato, fecha_inicio__lte=fecha_fin)
        .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=fecha_inicio))
        .select_related("equipo")
    )

    for asignacion in asignaciones_vigentes:
        _auditar_asignacion(
            resultado,
            asignacion,
            fecha_inicio,
            fecha_fin,
            dias_falla_sincronizacion=dias_falla_sincronizacion,
            meses_historial=meses_historial,
            factor_anomalia=factor_anomalia,
        )

    return resultado


def _auditar_asignacion(
    resultado: ResultadoAuditoria,
    asignacion: Asignacion,
    fecha_inicio: date,
    fecha_fin: date,
    *,
    dias_falla_sincronizacion: int,
    meses_historial: int,
    factor_anomalia: Decimal,
) -> None:
    equipo = asignacion.equipo
    lecturas_periodo = list(
        Lectura.objects.filter(asignacion=asignacion, fecha__gte=fecha_inicio, fecha__lte=fecha_fin).order_by(
            "fecha"
        )
    )

    # (c) Equipo omitido: solo para equipos con asignación activa (sin fecha_fin) al contrato.
    if not lecturas_periodo and asignacion.fecha_fin is None:
        resultado.alertas.append(
            AlertaAuditoria(
                candado="c",
                equipo_numero_serie=equipo.numero_serie,
                bloqueante=True,
                mensaje="Equipo con asignación activa sin ninguna lectura en el periodo auditado.",
                datos={"asignacion_id": asignacion.id},
            )
        )

    # (g) Falla de sincronización: solo equipos con método de lectura vía API.
    if equipo.metodo_lectura in (MetodoLectura.API_3MANAGER, MetodoLectura.API_PRINTAUDIT):
        ultima_lectura = Lectura.objects.filter(asignacion=asignacion).order_by("-fecha").first()
        limite = fecha_fin - timedelta(days=dias_falla_sincronizacion)
        if ultima_lectura is None or ultima_lectura.fecha < limite:
            resultado.alertas.append(
                AlertaAuditoria(
                    candado="g",
                    equipo_numero_serie=equipo.numero_serie,
                    bloqueante=True,
                    mensaje=(
                        f"Equipo con método {equipo.metodo_lectura} sin lectura en los "
                        f"últimos {dias_falla_sincronizacion} días."
                    ),
                    datos={"ultima_lectura": str(ultima_lectura.fecha) if ultima_lectura else None},
                )
            )

    consumo_bn_periodo = 0
    consumo_color_periodo = 0

    for lectura in lecturas_periodo:
        # (e) Equipo huérfano: la lectura cae fuera de la vigencia de su propia asignación.
        fuera_de_vigencia = lectura.fecha < asignacion.fecha_inicio or (
            asignacion.fecha_fin is not None and lectura.fecha > asignacion.fecha_fin
        )
        if fuera_de_vigencia:
            resultado.alertas.append(
                AlertaAuditoria(
                    candado="e",
                    equipo_numero_serie=equipo.numero_serie,
                    bloqueante=True,
                    mensaje="Lectura registrada fuera de la vigencia de la asignación (equipo huérfano).",
                    datos={"lectura_id": lectura.id, "fecha": str(lectura.fecha)},
                )
            )
            continue  # no se contabiliza en el consumo del periodo

        # (d) Equipo en cero.
        if lectura.lectura_bn == 0 and lectura.lectura_color == 0:
            resultado.alertas.append(
                AlertaAuditoria(
                    candado="d",
                    equipo_numero_serie=equipo.numero_serie,
                    bloqueante=True,
                    mensaje="Lectura en cero: requiere confirmación explícita antes de facturar.",
                    datos={"lectura_id": lectura.id, "fecha": str(lectura.fecha)},
                )
            )

        consumo = calcular_consumo(asignacion, lectura)

        # (a) Lectura invertida sin rollover configurado. El rollover válido (b)
        # ya fue aplicado dentro de calcular_consumo() y no genera alerta.
        if consumo.lectura_invertida_bn or consumo.lectura_invertida_color:
            resultado.alertas.append(
                AlertaAuditoria(
                    candado="a",
                    equipo_numero_serie=equipo.numero_serie,
                    bloqueante=True,
                    mensaje="Lectura invertida (menor a la anterior) sin rollover configurado para el equipo.",
                    datos={"lectura_id": lectura.id, "fecha": str(lectura.fecha)},
                )
            )

        consumo_bn_periodo += consumo.consumo_bn
        consumo_color_periodo += consumo.consumo_color

    # (f) Anomalía estadística sobre el consumo total del periodo.
    if lecturas_periodo:
        for categoria, consumo_periodo in (("bn", consumo_bn_periodo), ("color", consumo_color_periodo)):
            promedio = promedio_historico_mensual(equipo, categoria, fecha_inicio, meses_historial)
            if promedio and consumo_periodo > float(factor_anomalia) * promedio:
                resultado.alertas.append(
                    AlertaAuditoria(
                        candado="f",
                        equipo_numero_serie=equipo.numero_serie,
                        bloqueante=False,
                        mensaje=(
                            f"Consumo {categoria.upper()} ({consumo_periodo}) supera "
                            f"{factor_anomalia}x el promedio histórico ({promedio:.1f})."
                        ),
                        datos={"consumo": consumo_periodo, "promedio_historico": promedio},
                    )
                )

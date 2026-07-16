"""Cálculo de consumo por lectura, compartido entre `auditoria/` y `facturacion/`.

Mantener esta lógica en un solo lugar evita que ambos motores diverjan en cómo
interpretan rollover de contador o el primer mes de una asignación nueva.
"""
from dataclasses import dataclass
from datetime import date

from core.models import Asignacion, Lectura


@dataclass
class ResultadoConsumo:
    consumo_bn: int
    consumo_color: int
    lectura_invertida_bn: bool
    lectura_invertida_color: bool
    es_primera_lectura_asignacion: bool
    anterior_bn: int
    anterior_color: int
    fecha_anterior: date


def lectura_anterior(asignacion: Asignacion, lectura: Lectura) -> tuple[int, int, bool, date]:
    """Devuelve (lectura_bn_anterior, lectura_color_anterior, es_primer_punto, fecha_anterior).

    Si existe una Lectura previa (por fecha) en la misma asignación se usa esa.
    Si no existe ninguna —primer mes de la asignación— se usa la lectura de
    referencia capturada al crear la asignación, con fecha_inicio como fecha.
    """
    anterior = (
        Lectura.objects.filter(asignacion=asignacion, fecha__lt=lectura.fecha)
        .order_by("-fecha")
        .first()
    )
    if anterior is not None:
        return anterior.lectura_bn, anterior.lectura_color, False, anterior.fecha
    return (
        asignacion.lectura_inicial_referencia_bn,
        asignacion.lectura_inicial_referencia_color,
        True,
        asignacion.fecha_inicio,
    )


def _calcular_categoria(anterior: int, actual: int, tope_contador, permite_reset: bool) -> tuple[int, bool]:
    """Devuelve (consumo, invertida_sin_rollover_valido)."""
    if actual >= anterior:
        return actual - anterior, False
    if permite_reset and tope_contador:
        return (tope_contador - anterior) + actual, False
    return 0, True


def calcular_consumo(asignacion: Asignacion, lectura: Lectura) -> ResultadoConsumo:
    """Consumo de una Lectura puntual contra su punto de referencia anterior.

    Aplica rollover automáticamente cuando el equipo lo permite y tiene
    `tope_contador` configurado; si la lectura es menor a la anterior sin
    rollover válido, el consumo se reporta en 0 y se marca `lectura_invertida_*`
    para que el llamador decida cómo bloquear/alertar.
    """
    anterior_bn, anterior_color, es_primera, fecha_anterior = lectura_anterior(asignacion, lectura)
    equipo = asignacion.equipo

    consumo_bn, invertida_bn = _calcular_categoria(
        anterior_bn, lectura.lectura_bn, equipo.tope_contador, equipo.permite_reset_contador
    )
    consumo_color, invertida_color = _calcular_categoria(
        anterior_color, lectura.lectura_color, equipo.tope_contador, equipo.permite_reset_contador
    )

    return ResultadoConsumo(
        consumo_bn=consumo_bn,
        consumo_color=consumo_color,
        lectura_invertida_bn=invertida_bn,
        lectura_invertida_color=invertida_color,
        es_primera_lectura_asignacion=es_primera,
        anterior_bn=anterior_bn,
        anterior_color=anterior_color,
        fecha_anterior=fecha_anterior,
    )

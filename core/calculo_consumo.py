"""Cálculo de consumo por lectura, compartido entre `auditoria/`, `facturacion/`
y `core.models.Lectura.save()`.

Mantener esta lógica en un solo lugar evita que los distintos puntos que la
usan diverjan en cómo interpretan rollover de contador, el primer mes de una
asignación nueva, o qué cuenta como "consumo anómalo" para un equipo.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from core.models import Asignacion, Equipo, Lectura

DIAS_MES_APROX = 30
MESES_HISTORIAL_DEFAULT = 6
FACTOR_ANOMALIA_DEFAULT = Decimal("3.5")


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


def promedio_historico_mensual(equipo: Equipo, categoria: str, antes_de: date, meses: int) -> float | None:
    """Promedio de consumo mensual del equipo en los `meses` anteriores a `antes_de`.

    Agrupa por mes calendario (la suma de deltas diarios dentro de un mes es
    igual al consumo total del mes, sin importar la granularidad real de las
    lecturas) y promedia esos totales mensuales. Devuelve None si no hay
    suficiente historial para establecer una base confiable.
    """
    desde = antes_de - timedelta(days=meses * DIAS_MES_APROX)
    lecturas = list(
        Lectura.objects.filter(asignacion__equipo=equipo, fecha__gte=desde, fecha__lt=antes_de)
        .select_related("asignacion")
        .order_by("fecha")
    )
    if len(lecturas) < 2:
        return None

    consumo_por_mes: dict[tuple[int, int], int] = {}
    for lectura in lecturas:
        resultado_consumo = calcular_consumo(lectura.asignacion, lectura)
        if resultado_consumo.lectura_invertida_bn or resultado_consumo.lectura_invertida_color:
            continue
        if resultado_consumo.es_primera_lectura_asignacion:
            # El salto contra la lectura de referencia inicial no representa
            # consumo real del periodo anterior; se excluye del promedio.
            continue
        consumo = resultado_consumo.consumo_bn if categoria == "bn" else resultado_consumo.consumo_color
        clave_mes = (lectura.fecha.year, lectura.fecha.month)
        consumo_por_mes[clave_mes] = consumo_por_mes.get(clave_mes, 0) + consumo

    if not consumo_por_mes:
        return None
    valores = list(consumo_por_mes.values())
    return sum(valores) / len(valores)


@dataclass
class AnomaliaConsumo:
    categoria: str  # "bn" o "color"
    consumo: int
    promedio_historico: float
    factor: Decimal

    @property
    def mensaje(self) -> str:
        return (
            f"Consumo {self.categoria.upper()} ({self.consumo}) supera "
            f"{self.factor}x el promedio histórico ({self.promedio_historico:.1f})."
        )

    def to_dict(self) -> dict:
        return {
            "categoria": self.categoria,
            "consumo": self.consumo,
            "promedio_historico": self.promedio_historico,
            "factor": str(self.factor),
            "mensaje": self.mensaje,
        }


def detectar_anomalias(
    asignacion: Asignacion,
    lectura: Lectura,
    *,
    factor_anomalia: Decimal = FACTOR_ANOMALIA_DEFAULT,
    meses_historial: int = MESES_HISTORIAL_DEFAULT,
) -> list[AnomaliaConsumo]:
    """Compara el consumo implícito de una lectura puntual contra el promedio
    histórico mensual del equipo — para avisar en el momento de captura, no
    solo hasta la auditoría de fin de mes (ver `auditoria.motor`, candado f,
    que aplica el mismo criterio sobre el consumo de todo un periodo).

    Asume una cadencia de lectura aproximadamente mensual: con lecturas más
    frecuentes el aviso puede no dispararse; con huecos largos puede
    dispararse de más. Como es no bloqueante (solo marca `estado_auditoria`),
    ese margen de error es aceptable.
    """
    consumo = calcular_consumo(asignacion, lectura)
    if consumo.es_primera_lectura_asignacion or consumo.lectura_invertida_bn or consumo.lectura_invertida_color:
        return []

    equipo = asignacion.equipo
    anomalias = []
    for categoria, valor in (("bn", consumo.consumo_bn), ("color", consumo.consumo_color)):
        promedio = promedio_historico_mensual(equipo, categoria, lectura.fecha, meses_historial)
        if promedio and valor > float(factor_anomalia) * promedio:
            anomalias.append(
                AnomaliaConsumo(
                    categoria=categoria, consumo=valor, promedio_historico=promedio, factor=factor_anomalia
                )
            )
    return anomalias

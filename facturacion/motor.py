"""Motor de cálculo financiero.

`calcular_factura` es la función principal: para un Contrato ya aprobado en
auditoría (ver `auditoria.auditar_contrato`), calcula el consumo real por
equipo —usando `lectura_inicial_referencia` cuando es el primer mes de la
asignación—, lo consolida por categoría BN/Color a nivel contrato, calcula
excedente sobre lo incluido, y aplica IVA.

`simulacion=True` (default) no toca la base de datos. `simulacion=False`
además persiste la Factura. El orquestador de facturación (módulo 8) llama
esta función en modo simulación para generar los documentos, y solo si la
subida a Drive tiene éxito invoca `persistir_factura` directamente (con las
URLs ya conocidas) dentro de su propia transacción atómica.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.calculo_consumo import calcular_consumo
from core.models import Asignacion, Contrato, Factura, Lectura

from .tipos import ConsumoEquipo, ResultadoFacturacion


def calcular_factura(
    contrato: Contrato,
    fecha_inicio: date,
    fecha_fin: date,
    periodo_mes: int,
    periodo_anio: int,
    *,
    simulacion: bool = True,
) -> ResultadoFacturacion:
    resultado = _calcular(contrato, fecha_inicio, fecha_fin, periodo_mes, periodo_anio)
    if not simulacion:
        persistir_factura(resultado)
    return resultado


def _calcular(
    contrato: Contrato, fecha_inicio: date, fecha_fin: date, periodo_mes: int, periodo_anio: int
) -> ResultadoFacturacion:
    asignaciones = (
        Asignacion.objects.filter(contrato=contrato, fecha_inicio__lte=fecha_fin)
        .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=fecha_inicio))
        .select_related("equipo")
    )

    consumo_por_equipo: list[ConsumoEquipo] = []
    total_consumo_bn = 0
    total_consumo_color = 0

    for asignacion in asignaciones:
        lecturas = Lectura.objects.filter(
            asignacion=asignacion, fecha__gte=fecha_inicio, fecha__lte=fecha_fin
        ).order_by("fecha")

        consumo_bn = 0
        consumo_color = 0
        hubo_lecturas_validas = False
        for lectura in lecturas:
            fuera_de_vigencia = lectura.fecha < asignacion.fecha_inicio or (
                asignacion.fecha_fin is not None and lectura.fecha > asignacion.fecha_fin
            )
            if fuera_de_vigencia:
                # equipo huérfano: se excluye del cálculo, igual que en auditoría.
                continue
            resultado_consumo = calcular_consumo(asignacion, lectura)
            consumo_bn += resultado_consumo.consumo_bn
            consumo_color += resultado_consumo.consumo_color
            hubo_lecturas_validas = True

        if hubo_lecturas_validas:
            consumo_por_equipo.append(
                ConsumoEquipo(
                    asignacion_id=asignacion.id,
                    equipo_numero_serie=asignacion.equipo.numero_serie,
                    consumo_bn=consumo_bn,
                    consumo_color=consumo_color,
                )
            )
            total_consumo_bn += consumo_bn
            total_consumo_color += consumo_color

    excedente_bn = max(0, total_consumo_bn - contrato.copias_incluidas_bn)
    excedente_color = max(0, total_consumo_color - contrato.copias_incluidas_color)

    monto_renta = _redondear(contrato.renta_base)
    monto_excedente = _redondear(
        Decimal(excedente_bn) * contrato.costo_excedente_bn
        + Decimal(excedente_color) * contrato.costo_excedente_color
    )
    subtotal = monto_renta + monto_excedente
    monto_iva = _redondear(subtotal * contrato.iva_porcentaje / Decimal("100"))
    monto_total = monto_renta + monto_excedente + monto_iva

    return ResultadoFacturacion(
        contrato_id=contrato.id,
        periodo_mes=periodo_mes,
        periodo_anio=periodo_anio,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        consumo_por_equipo=consumo_por_equipo,
        consumo_excedente_bn=excedente_bn,
        consumo_excedente_color=excedente_color,
        monto_renta=monto_renta,
        monto_excedente=monto_excedente,
        monto_iva=monto_iva,
        monto_total=monto_total,
    )


def _redondear(valor: Decimal) -> Decimal:
    return valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
def persistir_factura(
    resultado: ResultadoFacturacion,
    *,
    pdf_url: str = "",
    excel_url: str = "",
    estado: str = Factura.Estado.OK,
) -> Factura:
    """Crea (o actualiza) la Factura de un ResultadoFacturacion ya calculado.

    Se usa una vez que los archivos ya fueron subidos exitosamente a Drive;
    ver el orquestador `procesar_facturacion_diaria`.
    """
    factura, _ = Factura.objects.update_or_create(
        contrato_id=resultado.contrato_id,
        periodo_mes=resultado.periodo_mes,
        periodo_anio=resultado.periodo_anio,
        defaults={
            "consumo_excedente_bn": resultado.consumo_excedente_bn,
            "consumo_excedente_color": resultado.consumo_excedente_color,
            "monto_renta": resultado.monto_renta,
            "monto_excedente": resultado.monto_excedente,
            "monto_iva": resultado.monto_iva,
            "monto_total": resultado.monto_total,
            "pdf_url": pdf_url,
            "excel_url": excel_url,
            "estado": estado,
            "fecha_generacion": timezone.now(),
        },
    )
    resultado.factura = factura
    return factura

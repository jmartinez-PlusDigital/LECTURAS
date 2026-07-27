from decimal import Decimal

import weasyprint
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from core.models import Contrato, Equipo
from facturacion import ResultadoFacturacion

MESES_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


SIMBOLO_MONEDA = {"MXN": "$", "USD": "US$"}


def _moneda(valor: Decimal, moneda: str = "MXN") -> str:
    simbolo = SIMBOLO_MONEDA.get(moneda, f"{moneda} ")
    return f"{simbolo}{valor:,.2f}"


def _tarifa(valor: Decimal, moneda: str = "MXN") -> str:
    """Tarifa por copia: a diferencia de los montos (2 decimales), se muestra
    con 4 —el costo_excedente del contrato se captura con esa precisión— para
    que la multiplicación (copias × tarifa) le cuadre exacto al cliente."""
    simbolo = SIMBOLO_MONEDA.get(moneda, f"{moneda} ")
    return f"{simbolo}{valor:,.4f}"


def generar_pdf_factura(resultado: ResultadoFacturacion, contrato: Contrato) -> bytes:
    """Genera el PDF de factura (horizontal) a partir de un ResultadoFacturacion.

    No persiste nada: recibe el resultado ya calculado (ver `facturacion.calcular_factura`).
    """
    series = [c.equipo_numero_serie for c in resultado.consumo_por_equipo]
    marcas_modelos = {
        e.numero_serie: f"{e.marca} {e.modelo}"
        for e in Equipo.objects.filter(numero_serie__in=series).only("numero_serie", "marca", "modelo")
    }
    equipos = [
        {
            "numero_serie": consumo.equipo_numero_serie,
            "marca_modelo": marcas_modelos.get(consumo.equipo_numero_serie, ""),
            "fecha_lectura_anterior": consumo.fecha_lectura_anterior.strftime("%d/%m/%Y"),
            "lectura_anterior_bn": consumo.lectura_anterior_bn,
            "lectura_anterior_color": consumo.lectura_anterior_color,
            "fecha_lectura_actual": consumo.fecha_lectura_actual.strftime("%d/%m/%Y"),
            "lectura_actual_bn": consumo.lectura_actual_bn,
            "lectura_actual_color": consumo.lectura_actual_color,
            "consumo_bn": consumo.consumo_bn,
            "consumo_color": consumo.consumo_color,
        }
        for consumo in resultado.consumo_por_equipo
    ]

    monto_excedente_bn = Decimal(resultado.consumo_excedente_bn) * contrato.costo_excedente_bn
    monto_excedente_color = Decimal(resultado.consumo_excedente_color) * contrato.costo_excedente_color
    total_consumo_bn = sum((c.consumo_bn for c in resultado.consumo_por_equipo), 0)
    total_consumo_color = sum((c.consumo_color for c in resultado.consumo_por_equipo), 0)

    contexto = {
        "empresa_nombre": settings.EMPRESA_NOMBRE,
        "contrato": contrato,
        "cliente": contrato.cliente,
        "periodo_texto": f"{MESES_ES[resultado.periodo_mes]} {resultado.periodo_anio}",
        "estado_texto": "Ok",
        "fecha_inicio": resultado.fecha_inicio.strftime("%d/%m/%Y"),
        "fecha_fin": resultado.fecha_fin.strftime("%d/%m/%Y"),
        "fecha_generacion": timezone.localtime().strftime("%d/%m/%Y %H:%M"),
        "equipos": equipos,
        "moneda": resultado.moneda,
        "total_consumo_bn": total_consumo_bn,
        "total_consumo_color": total_consumo_color,
        "copias_incluidas_bn": contrato.copias_incluidas_bn,
        "copias_incluidas_color": contrato.copias_incluidas_color,
        "tarifa_excedente_bn": _tarifa(contrato.costo_excedente_bn, resultado.moneda),
        "tarifa_excedente_color": _tarifa(contrato.costo_excedente_color, resultado.moneda),
        "consumo_excedente_bn": resultado.consumo_excedente_bn,
        "consumo_excedente_color": resultado.consumo_excedente_color,
        "monto_excedente_bn": _moneda(monto_excedente_bn, resultado.moneda),
        "monto_excedente_color": _moneda(monto_excedente_color, resultado.moneda),
        "monto_renta": _moneda(resultado.monto_renta, resultado.moneda),
        "monto_excedente": _moneda(resultado.monto_excedente, resultado.moneda),
        "monto_iva": _moneda(resultado.monto_iva, resultado.moneda),
        "monto_total": _moneda(resultado.monto_total, resultado.moneda),
        "iva_porcentaje": contrato.iva_porcentaje,
    }

    html = render_to_string("documentos/factura_pdf.html", contexto)
    return weasyprint.HTML(string=html).write_pdf()

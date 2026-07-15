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


def _moneda(valor: Decimal) -> str:
    return f"${valor:,.2f}"


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
            "consumo_bn": consumo.consumo_bn,
            "consumo_color": consumo.consumo_color,
        }
        for consumo in resultado.consumo_por_equipo
    ]

    monto_excedente_bn = Decimal(resultado.consumo_excedente_bn) * contrato.costo_excedente_bn
    monto_excedente_color = Decimal(resultado.consumo_excedente_color) * contrato.costo_excedente_color

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
        "consumo_excedente_bn": resultado.consumo_excedente_bn,
        "consumo_excedente_color": resultado.consumo_excedente_color,
        "monto_excedente_bn": _moneda(monto_excedente_bn),
        "monto_excedente_color": _moneda(monto_excedente_color),
        "monto_renta": _moneda(resultado.monto_renta),
        "monto_excedente": _moneda(resultado.monto_excedente),
        "monto_iva": _moneda(resultado.monto_iva),
        "monto_total": _moneda(resultado.monto_total),
        "iva_porcentaje": contrato.iva_porcentaje,
    }

    html = render_to_string("documentos/factura_pdf.html", contexto)
    return weasyprint.HTML(string=html).write_pdf()

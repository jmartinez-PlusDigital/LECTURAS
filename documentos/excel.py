from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.models import Contrato, Equipo
from facturacion import ResultadoFacturacion

from .pdf import MESES_ES

AZUL_OSCURO = "1F3D5C"
GRIS_CLARO = "F5F7F9"


def generar_excel_factura(resultado: ResultadoFacturacion, contrato: Contrato) -> bytes:
    """Genera el Excel de desglose de consumo y facturación de un contrato/periodo."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Factura"

    fuente_titulo = Font(size=14, bold=True, color=AZUL_OSCURO)
    fuente_encabezado_tabla = Font(bold=True, color="FFFFFF")
    relleno_encabezado_tabla = PatternFill("solid", fgColor=AZUL_OSCURO)
    fuente_etiqueta = Font(bold=True)
    relleno_alterno = PatternFill("solid", fgColor=GRIS_CLARO)

    ws["A1"] = f"Contrato {contrato.numero_contrato} — {MESES_ES[resultado.periodo_mes]} {resultado.periodo_anio}"
    ws["A1"].font = fuente_titulo
    ws.merge_cells("A1:D1")

    ws["A3"] = "Cliente"
    ws["A3"].font = fuente_etiqueta
    ws["B3"] = contrato.cliente.nombre
    ws["A4"] = "Periodo"
    ws["A4"].font = fuente_etiqueta
    ws["B4"] = f"{resultado.fecha_inicio.strftime('%d/%m/%Y')} - {resultado.fecha_fin.strftime('%d/%m/%Y')}"

    fila_encabezado_tabla = 6
    encabezados = [
        "Número de serie",
        "Marca / Modelo",
        "Fecha lect. anterior",
        "Lect. anterior BN",
        "Lect. anterior Color",
        "Fecha lect. actual",
        "Lect. actual BN",
        "Lect. actual Color",
        "Consumo BN",
        "Consumo Color",
    ]
    for col, texto in enumerate(encabezados, start=1):
        celda = ws.cell(row=fila_encabezado_tabla, column=col, value=texto)
        celda.font = fuente_encabezado_tabla
        celda.fill = relleno_encabezado_tabla
        celda.alignment = Alignment(horizontal="center")

    series = [c.equipo_numero_serie for c in resultado.consumo_por_equipo]
    marcas_modelos = {
        e.numero_serie: f"{e.marca} {e.modelo}"
        for e in Equipo.objects.filter(numero_serie__in=series).only("numero_serie", "marca", "modelo")
    }

    fila = fila_encabezado_tabla + 1
    for consumo in resultado.consumo_por_equipo:
        ws.cell(row=fila, column=1, value=consumo.equipo_numero_serie)
        ws.cell(row=fila, column=2, value=marcas_modelos.get(consumo.equipo_numero_serie, ""))
        ws.cell(row=fila, column=3, value=consumo.fecha_lectura_anterior.strftime("%d/%m/%Y"))
        ws.cell(row=fila, column=4, value=consumo.lectura_anterior_bn)
        ws.cell(row=fila, column=5, value=consumo.lectura_anterior_color)
        ws.cell(row=fila, column=6, value=consumo.fecha_lectura_actual.strftime("%d/%m/%Y"))
        ws.cell(row=fila, column=7, value=consumo.lectura_actual_bn)
        ws.cell(row=fila, column=8, value=consumo.lectura_actual_color)
        ws.cell(row=fila, column=9, value=consumo.consumo_bn)
        ws.cell(row=fila, column=10, value=consumo.consumo_color)
        if fila % 2 == 0:
            for col in range(1, 11):
                ws.cell(row=fila, column=col).fill = relleno_alterno
        fila += 1

    if not resultado.consumo_por_equipo:
        ws.cell(row=fila, column=1, value="Sin equipos con consumo registrado en este periodo.")
        fila += 1

    fila += 1  # renglón en blanco antes del resumen
    resumen = [
        ("Excedente BN (copias)", resultado.consumo_excedente_bn),
        ("Excedente Color (copias)", resultado.consumo_excedente_color),
        ("Monto renta", float(resultado.monto_renta)),
        ("Monto excedente", float(resultado.monto_excedente)),
        (f"IVA ({contrato.iva_porcentaje}%)", float(resultado.monto_iva)),
        ("Total", float(resultado.monto_total)),
    ]
    for etiqueta, valor in resumen:
        ws.cell(row=fila, column=1, value=etiqueta).font = fuente_etiqueta
        celda_valor = ws.cell(row=fila, column=2, value=valor)
        if isinstance(valor, float):
            celda_valor.number_format = '"$"#,##0.00'
        if etiqueta == "Total":
            ws.cell(row=fila, column=1).font = Font(bold=True, size=12, color=AZUL_OSCURO)
            celda_valor.font = Font(bold=True, size=12, color=AZUL_OSCURO)
        fila += 1

    anchos = [18, 28, 16, 15, 17, 16, 14, 16, 12, 14]
    for i, ancho in enumerate(anchos, start=1):
        ws.column_dimensions[get_column_letter(i)].width = ancho

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()

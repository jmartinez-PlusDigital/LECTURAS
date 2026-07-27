from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.models import Contrato, Equipo
from facturacion import ResultadoFacturacion

from .pdf import MESES_ES, SIMBOLO_MONEDA

AZUL_OSCURO = "1F3D5C"
GRIS_CLARO = "F5F7F9"
FORMATO_MONEDA = {"MXN": '"$"#,##0.00', "USD": '"US$"#,##0.00'}


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
    ws["A5"] = "Moneda"
    ws["A5"].font = fuente_etiqueta
    ws["B5"] = resultado.moneda

    formato_moneda = FORMATO_MONEDA.get(resultado.moneda, '#,##0.00 "' + resultado.moneda + '"')
    fila_encabezado_tabla = 7
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

    total_consumo_bn = sum((c.consumo_bn for c in resultado.consumo_por_equipo), 0)
    total_consumo_color = sum((c.consumo_color for c in resultado.consumo_por_equipo), 0)

    if not resultado.consumo_por_equipo:
        ws.cell(row=fila, column=1, value="Sin equipos con consumo registrado en este periodo.")
        fila += 1
    else:
        ws.cell(row=fila, column=1, value="Total consumo del periodo").font = Font(bold=True, color=AZUL_OSCURO)
        ws.cell(row=fila, column=9, value=total_consumo_bn).font = Font(bold=True, color=AZUL_OSCURO)
        ws.cell(row=fila, column=10, value=total_consumo_color).font = Font(bold=True, color=AZUL_OSCURO)
        for col in range(1, 11):
            ws.cell(row=fila, column=col).border = Border(top=Side(style="medium", color=AZUL_OSCURO))
        fila += 1

    fila += 1  # renglón en blanco antes del resumen
    simbolo_tarifa = SIMBOLO_MONEDA.get(resultado.moneda, resultado.moneda + " ")
    formato_tarifa = f'"{simbolo_tarifa}"#,##0.0000'
    resumen = [
        ("Consumo total BN (copias)", total_consumo_bn, None),
        ("Copias incluidas BN", contrato.copias_incluidas_bn, None),
        ("Tarifa excedente BN (por copia)", float(contrato.costo_excedente_bn), formato_tarifa),
        ("Excedente BN (copias)", resultado.consumo_excedente_bn, None),
        ("Consumo total Color (copias)", total_consumo_color, None),
        ("Copias incluidas Color", contrato.copias_incluidas_color, None),
        ("Tarifa excedente Color (por copia)", float(contrato.costo_excedente_color), formato_tarifa),
        ("Excedente Color (copias)", resultado.consumo_excedente_color, None),
        ("Monto renta", float(resultado.monto_renta), formato_moneda),
        ("Monto excedente", float(resultado.monto_excedente), formato_moneda),
        (f"IVA ({contrato.iva_porcentaje}%)", float(resultado.monto_iva), formato_moneda),
        ("Total", float(resultado.monto_total), formato_moneda),
    ]
    for etiqueta, valor, formato in resumen:
        ws.cell(row=fila, column=1, value=etiqueta).font = fuente_etiqueta
        celda_valor = ws.cell(row=fila, column=2, value=valor)
        if formato:
            celda_valor.number_format = formato
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

"""Piezas compartidas por los 4 importadores masivos (`importacion_clientes`,
`importacion_contratos`, `importacion_equipos`, `importacion_lecturas`):
parseo de valores de celda y el patrón transacción-con-dry_run.

Cada importador sigue conservando su propia función pública
`procesar_archivo_*` (firma estable para quien la importe) y su propio
`_procesar_fila`, que sí es específico de cada modelo — lo que se comparte
aquí es solo lo que era literalmente idéntico entre los cuatro.
"""
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Callable

from django.db import transaction
from django.utils.dateparse import parse_date

from core.archivos import leer_filas

VALORES_VERDADEROS = {"1", "true", "verdadero", "si", "sí", "x", "yes"}


def procesar_archivo(
    archivo, nombre_archivo: str, procesar_fila: Callable[[int, dict], dict], *, dry_run: bool = False
) -> list[dict]:
    """Lee `archivo` fila por fila y aplica `procesar_fila(numero_fila, fila)`
    a cada una dentro de una sola transacción; con `dry_run=True` se revierte
    todo al final (cada fila ya corrió su propia validación y, si aplicó,
    escritura, pero nada queda persistido)."""
    filas = list(leer_filas(archivo, nombre_archivo))
    if not filas:
        return []

    resultados = []
    with transaction.atomic():
        for numero_fila, fila in enumerate(filas, start=2):  # fila 1 = encabezados
            resultados.append(procesar_fila(numero_fila, fila))
        if dry_run:
            transaction.set_rollback(True)
    return resultados


def escribir_reporte_csv(destino, resultados: list[dict], columnas: list[str]) -> None:
    """`destino` puede ser una ruta (str) o un objeto tipo archivo en modo texto."""
    if isinstance(destino, str):
        with open(destino, "w", newline="", encoding="utf-8") as f:
            _escribir_csv(f, resultados, columnas)
    else:
        _escribir_csv(destino, resultados, columnas)


def _escribir_csv(f, resultados: list[dict], columnas: list[str]) -> None:
    writer = csv.DictWriter(f, fieldnames=columnas)
    writer.writeheader()
    writer.writerows(resultados)


# --- parseo de valores de celda --------------------------------------------

def texto(valor) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def entero(valor):
    """None si la celda viene vacía; lanza ValueError si trae algo no numérico."""
    valor_texto = texto(valor)
    if not valor_texto:
        return None
    try:
        return int(Decimal(valor_texto))
    except (InvalidOperation, ValueError):
        raise ValueError(f"valor numérico inválido: '{valor}'")


def decimal(valor, *, requerido: bool):
    valor_texto = texto(valor)
    if not valor_texto:
        if requerido:
            raise ValueError("valor requerido faltante")
        return None
    try:
        return Decimal(valor_texto)
    except InvalidOperation:
        raise ValueError(f"valor decimal inválido: '{valor}'")


def fecha(valor):
    """None si la celda viene vacía; lanza ValueError si trae un formato que
    `parse_date` no reconoce (se espera YYYY-MM-DD)."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, date):
        return valor
    fecha_parseada = parse_date(texto(valor))
    if fecha_parseada is None:
        raise ValueError(f"fecha inválida: '{valor}'. Usa formato YYYY-MM-DD.")
    return fecha_parseada


def booleano(valor, *, default: bool = False) -> bool:
    valor_texto = texto(valor)
    if not valor_texto:
        return default
    return valor_texto.lower() in VALORES_VERDADEROS

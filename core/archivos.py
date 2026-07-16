"""Lectura genérica de archivos subidos (CSV o Excel) como filas dict.

Compartido por los importadores masivos (`importacion_equipos`,
`importacion_lecturas`) para no duplicar el parseo de formato.
"""
import csv
import io

import openpyxl


def leer_filas(archivo, nombre_archivo: str):
    """`archivo` puede ser una ruta (str) o un objeto tipo archivo (p. ej. un
    `UploadedFile` de Django) abierto en modo binario. `nombre_archivo` se usa
    solo para decidir el formato por su extensión."""
    if nombre_archivo.lower().endswith((".xlsx", ".xls")):
        yield from _leer_xlsx(archivo)
    else:
        yield from _leer_csv(archivo)


def _leer_csv(archivo):
    if isinstance(archivo, str):
        with open(archivo, newline="", encoding="utf-8-sig") as f:
            yield from csv.DictReader(f)
    else:
        texto = io.TextIOWrapper(archivo, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(texto)


def _leer_xlsx(archivo):
    wb = openpyxl.load_workbook(archivo, read_only=True, data_only=True)
    hoja = wb.active
    filas = hoja.iter_rows(values_only=True)
    encabezados = [str(c).strip() if c is not None else "" for c in next(filas)]
    for fila in filas:
        if all(v is None for v in fila):
            continue
        yield dict(zip(encabezados, fila))

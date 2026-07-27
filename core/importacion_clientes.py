"""Importación masiva de clientes (CSV o Excel).

Usada por la vista del Admin en `core/admin.py`. Primer eslabón de la cadena
de carga masiva: Cliente -> Contrato -> Equipo (+ lectura de referencia) ->
Lectura. `id_cuenta_3manager` es opcional pero útil: si un cliente ya existe
en 3-Manager y se llena aquí, después se puede seleccionar en el listado de
Clientes y usar la acción "3-Manager: importar equipos de la cuenta" para
traer sus equipos automáticamente.

Columnas esperadas (encabezados exactos, en cualquier orden):
    nombre                (requerido, clave de emparejamiento)
    razon_social          (opcional)
    rfc                   (opcional)
    contacto_nombre       (opcional)
    contacto_email        (opcional)
    contacto_telefono     (opcional)
    direccion             (opcional)
    activo                true/false, si/no, 1/0 (default: true)
    id_cuenta_3manager    (opcional)

Si ya existe un cliente con ese `nombre`, se actualiza en vez de duplicarse.
Cada fila se procesa de forma aislada (un error en una fila no detiene el
resto). `dry_run=True` valida todo pero no persiste nada.
"""
import csv

from django.db import transaction

from core.archivos import leer_filas
from core.models import Cliente

VALORES_VERDADEROS = {"1", "true", "verdadero", "si", "sí", "x", "yes"}
COLUMNAS_REPORTE = ["fila", "nombre", "cliente", "error"]


def procesar_archivo_clientes(archivo, nombre_archivo: str, *, dry_run: bool = False) -> list[dict]:
    """Procesa un archivo de clientes y devuelve el resultado por fila.

    `archivo` puede ser una ruta (str) o un objeto tipo archivo (p. ej. un
    `UploadedFile` de Django) abierto en modo binario. `nombre_archivo` se usa
    solo para decidir el formato por su extensión.
    """
    filas = list(leer_filas(archivo, nombre_archivo))
    if not filas:
        return []

    resultados = []
    with transaction.atomic():
        for numero_fila, fila in enumerate(filas, start=2):  # fila 1 = encabezados
            resultados.append(_procesar_fila(numero_fila, fila))
        if dry_run:
            transaction.set_rollback(True)
    return resultados


def resumen_de(resultados: list[dict]) -> dict:
    con_error = [r for r in resultados if r["error"]]
    return {
        "total": len(resultados),
        "creados": sum(1 for r in resultados if r["cliente"] == "creado"),
        "actualizados": sum(1 for r in resultados if r["cliente"] == "actualizado"),
        "con_error": con_error,
    }


def escribir_reporte_csv(destino, resultados: list[dict]) -> None:
    """`destino` puede ser una ruta (str) o un objeto tipo archivo en modo texto."""
    if isinstance(destino, str):
        with open(destino, "w", newline="", encoding="utf-8") as f:
            _escribir_csv(f, resultados)
    else:
        _escribir_csv(destino, resultados)


def _escribir_csv(f, resultados: list[dict]) -> None:
    writer = csv.DictWriter(f, fieldnames=COLUMNAS_REPORTE)
    writer.writeheader()
    writer.writerows(resultados)


# --- procesamiento por fila ------------------------------------------------

def _procesar_fila(numero_fila: int, fila: dict) -> dict:
    resultado = {"fila": numero_fila, "nombre": "", "cliente": "", "error": ""}
    try:
        with transaction.atomic():
            nombre = _texto(fila.get("nombre"))
            if not nombre:
                raise ValueError("nombre es requerido")
            resultado["nombre"] = nombre

            cliente, creado = Cliente.objects.update_or_create(
                nombre=nombre,
                defaults={
                    "razon_social": _texto(fila.get("razon_social")),
                    "rfc": _texto(fila.get("rfc")),
                    "contacto_nombre": _texto(fila.get("contacto_nombre")),
                    "contacto_email": _texto(fila.get("contacto_email")),
                    "contacto_telefono": _texto(fila.get("contacto_telefono")),
                    "direccion": _texto(fila.get("direccion")),
                    "activo": _booleano(fila.get("activo"), default=True),
                    "id_cuenta_3manager": _texto(fila.get("id_cuenta_3manager")),
                },
            )
            resultado["cliente"] = "creado" if creado else "actualizado"
    except Exception as exc:  # noqa: BLE001 - se aísla el error por fila a propósito
        resultado["cliente"] = ""
        resultado["error"] = str(exc)
    return resultado


def _texto(valor) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def _booleano(valor, *, default: bool) -> bool:
    texto = _texto(valor)
    if not texto:
        return default
    return texto.lower() in VALORES_VERDADEROS

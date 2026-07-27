"""Importación masiva de contratos (CSV o Excel).

Usada por la vista del Admin en `core/admin.py`. Segundo eslabón de la cadena
de carga masiva: Cliente -> Contrato -> Equipo (+ lectura de referencia) ->
Lectura. El cliente referenciado en cada fila debe existir ya (ver
`core.importacion_clientes`).

Columnas esperadas (encabezados exactos, en cualquier orden):
    numero_contrato          (requerido, único, clave de emparejamiento)
    cliente                  (requerido: nombre exacto de un Cliente ya existente)
    moneda                   MXN | USD (default: MXN)
    renta_base                (requerido)
    copias_incluidas_bn      (opcional, default 0)
    copias_incluidas_color   (opcional, default 0)
    costo_excedente_bn        (requerido)
    costo_excedente_color     (requerido)
    iva_porcentaje            (opcional, default 16.00)
    dia_corte_facturacion    (requerido, 1-31)
    estado                    activo | suspendido | terminado (default: activo)
    fecha_inicio              YYYY-MM-DD (requerido)
    fecha_fin                 YYYY-MM-DD (opcional)

Si ya existe un contrato con ese `numero_contrato`, se actualiza en vez de
duplicarse. Cada fila se procesa de forma aislada (un error en una fila no
detiene el resto). `dry_run=True` valida todo pero no persiste nada.
"""
import csv
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.dateparse import parse_date

from core.archivos import leer_filas
from core.models import Cliente, Contrato, Moneda

COLUMNAS_REPORTE = ["fila", "numero_contrato", "contrato", "error"]


def procesar_archivo_contratos(archivo, nombre_archivo: str, *, dry_run: bool = False) -> list[dict]:
    """Procesa un archivo de contratos y devuelve el resultado por fila.

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
        "creados": sum(1 for r in resultados if r["contrato"] == "creado"),
        "actualizados": sum(1 for r in resultados if r["contrato"] == "actualizado"),
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
    resultado = {"fila": numero_fila, "numero_contrato": "", "contrato": "", "error": ""}
    try:
        with transaction.atomic():
            numero_contrato = _texto(fila.get("numero_contrato"))
            if not numero_contrato:
                raise ValueError("numero_contrato es requerido")
            resultado["numero_contrato"] = numero_contrato

            nombre_cliente = _texto(fila.get("cliente"))
            if not nombre_cliente:
                raise ValueError("cliente es requerido")
            try:
                cliente = Cliente.objects.get(nombre=nombre_cliente)
            except Cliente.DoesNotExist:
                raise ValueError(f"no existe un cliente con nombre '{nombre_cliente}'")
            except Cliente.MultipleObjectsReturned:
                raise ValueError(f"hay más de un cliente con nombre '{nombre_cliente}'; desambigua a mano")

            moneda = _texto(fila.get("moneda")) or Moneda.MXN
            if moneda not in Moneda.values:
                raise ValueError(f"moneda inválida: '{moneda}'")

            estado = _texto(fila.get("estado")) or Contrato.Estado.ACTIVO
            if estado not in Contrato.Estado.values:
                raise ValueError(f"estado inválido: '{estado}'")

            fecha_inicio = _fecha(fila.get("fecha_inicio"))
            if fecha_inicio is None:
                raise ValueError("fecha_inicio es requerida y debe tener formato YYYY-MM-DD")
            fecha_fin = _fecha(fila.get("fecha_fin")) if _texto(fila.get("fecha_fin")) else None
            if fecha_fin is not None and fecha_fin <= fecha_inicio:
                raise ValueError("fecha_fin debe ser posterior a fecha_inicio")

            dia_corte = _entero(fila.get("dia_corte_facturacion"))
            if dia_corte is None or not (1 <= dia_corte <= 31):
                raise ValueError("dia_corte_facturacion es requerido y debe estar entre 1 y 31")

            contrato, creado = Contrato.objects.update_or_create(
                numero_contrato=numero_contrato,
                defaults={
                    "cliente": cliente,
                    "moneda": moneda,
                    "renta_base": _decimal(fila.get("renta_base"), requerido=True),
                    "copias_incluidas_bn": _entero(fila.get("copias_incluidas_bn")) or 0,
                    "copias_incluidas_color": _entero(fila.get("copias_incluidas_color")) or 0,
                    "costo_excedente_bn": _decimal(fila.get("costo_excedente_bn"), requerido=True),
                    "costo_excedente_color": _decimal(fila.get("costo_excedente_color"), requerido=True),
                    "iva_porcentaje": _decimal(fila.get("iva_porcentaje"), requerido=False) or Decimal("16.00"),
                    "dia_corte_facturacion": dia_corte,
                    "estado": estado,
                    "fecha_inicio": fecha_inicio,
                    "fecha_fin": fecha_fin,
                },
            )
            resultado["contrato"] = "creado" if creado else "actualizado"
    except Exception as exc:  # noqa: BLE001 - se aísla el error por fila a propósito
        resultado["contrato"] = ""
        resultado["error"] = str(exc)
    return resultado


def _texto(valor) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def _entero(valor):
    texto = _texto(valor)
    if not texto:
        return None
    try:
        return int(Decimal(texto))
    except (InvalidOperation, ValueError):
        raise ValueError(f"valor numérico inválido: '{valor}'")


def _decimal(valor, *, requerido: bool):
    texto = _texto(valor)
    if not texto:
        if requerido:
            raise ValueError("valor requerido faltante")
        return None
    try:
        return Decimal(texto)
    except InvalidOperation:
        raise ValueError(f"valor decimal inválido: '{valor}'")


def _fecha(valor):
    if valor is None or valor == "":
        return None
    if isinstance(valor, date):
        return valor
    fecha = parse_date(_texto(valor))
    if fecha is None:
        raise ValueError(f"fecha inválida: '{valor}'. Usa formato YYYY-MM-DD.")
    return fecha

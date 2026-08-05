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
from decimal import Decimal

from django.db import transaction

from core.importacion_comun import escribir_reporte_csv as _escribir_reporte_csv
from core.importacion_comun import decimal, entero, fecha, procesar_archivo, texto
from core.models import Cliente, Contrato, Moneda

COLUMNAS_REPORTE = ["fila", "numero_contrato", "contrato", "error"]


def procesar_archivo_contratos(archivo, nombre_archivo: str, *, dry_run: bool = False) -> list[dict]:
    """Procesa un archivo de contratos y devuelve el resultado por fila.

    `archivo` puede ser una ruta (str) o un objeto tipo archivo (p. ej. un
    `UploadedFile` de Django) abierto en modo binario. `nombre_archivo` se usa
    solo para decidir el formato por su extensión.
    """
    return procesar_archivo(archivo, nombre_archivo, _procesar_fila, dry_run=dry_run)


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
    _escribir_reporte_csv(destino, resultados, COLUMNAS_REPORTE)


# --- procesamiento por fila ------------------------------------------------

def _procesar_fila(numero_fila: int, fila: dict) -> dict:
    resultado = {"fila": numero_fila, "numero_contrato": "", "contrato": "", "error": ""}
    try:
        with transaction.atomic():
            numero_contrato = texto(fila.get("numero_contrato"))
            if not numero_contrato:
                raise ValueError("numero_contrato es requerido")
            resultado["numero_contrato"] = numero_contrato

            nombre_cliente = texto(fila.get("cliente"))
            if not nombre_cliente:
                raise ValueError("cliente es requerido")
            try:
                cliente = Cliente.objects.get(nombre=nombre_cliente)
            except Cliente.DoesNotExist:
                raise ValueError(f"no existe un cliente con nombre '{nombre_cliente}'")
            except Cliente.MultipleObjectsReturned:
                raise ValueError(f"hay más de un cliente con nombre '{nombre_cliente}'; desambigua a mano")

            moneda = texto(fila.get("moneda")) or Moneda.MXN
            if moneda not in Moneda.values:
                raise ValueError(f"moneda inválida: '{moneda}'")

            estado = texto(fila.get("estado")) or Contrato.Estado.ACTIVO
            if estado not in Contrato.Estado.values:
                raise ValueError(f"estado inválido: '{estado}'")

            fecha_inicio = fecha(fila.get("fecha_inicio"))
            if fecha_inicio is None:
                raise ValueError("fecha_inicio es requerida y debe tener formato YYYY-MM-DD")
            fecha_fin = fecha(fila.get("fecha_fin")) if texto(fila.get("fecha_fin")) else None
            if fecha_fin is not None and fecha_fin <= fecha_inicio:
                raise ValueError("fecha_fin debe ser posterior a fecha_inicio")

            dia_corte = entero(fila.get("dia_corte_facturacion"))
            if dia_corte is None or not (1 <= dia_corte <= 31):
                raise ValueError("dia_corte_facturacion es requerido y debe estar entre 1 y 31")

            contrato, creado = Contrato.objects.update_or_create(
                numero_contrato=numero_contrato,
                defaults={
                    "cliente": cliente,
                    "moneda": moneda,
                    "renta_base": decimal(fila.get("renta_base"), requerido=True),
                    "copias_incluidas_bn": entero(fila.get("copias_incluidas_bn")) or 0,
                    "copias_incluidas_color": entero(fila.get("copias_incluidas_color")) or 0,
                    "costo_excedente_bn": decimal(fila.get("costo_excedente_bn"), requerido=True),
                    "costo_excedente_color": decimal(fila.get("costo_excedente_color"), requerido=True),
                    "iva_porcentaje": decimal(fila.get("iva_porcentaje"), requerido=False) or Decimal("16.00"),
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

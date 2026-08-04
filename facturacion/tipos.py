from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class ConsumoEquipo:
    asignacion_id: int
    equipo_numero_serie: str
    consumo_bn: int
    consumo_color: int
    lectura_anterior_bn: int
    lectura_anterior_color: int
    fecha_lectura_anterior: date
    lectura_actual_bn: int
    lectura_actual_color: int
    fecha_lectura_actual: date
    ubicacion: str = ""


@dataclass
class ResultadoFacturacion:
    contrato_id: int
    periodo_mes: int
    periodo_anio: int
    fecha_inicio: date
    fecha_fin: date
    moneda: str
    emisor: object  # instancia de core.models.EmpresaEmisora (o None), copiada del contrato
    consumo_por_equipo: list[ConsumoEquipo]
    consumo_excedente_bn: int
    consumo_excedente_color: int
    monto_renta: Decimal
    monto_excedente: Decimal
    monto_iva: Decimal
    monto_total: Decimal
    factura: object = None  # se asigna una vez persistido (ver persistir_factura)

    def to_dict(self) -> dict:
        return {
            "contrato_id": self.contrato_id,
            "periodo_mes": self.periodo_mes,
            "periodo_anio": self.periodo_anio,
            "fecha_inicio": str(self.fecha_inicio),
            "fecha_fin": str(self.fecha_fin),
            "moneda": self.moneda,
            "emisor": self.emisor.nombre if self.emisor else None,
            "consumo_por_equipo": [
                {
                    "asignacion_id": c.asignacion_id,
                    "equipo": c.equipo_numero_serie,
                    "consumo_bn": c.consumo_bn,
                    "consumo_color": c.consumo_color,
                    "lectura_anterior_bn": c.lectura_anterior_bn,
                    "lectura_anterior_color": c.lectura_anterior_color,
                    "fecha_lectura_anterior": str(c.fecha_lectura_anterior),
                    "lectura_actual_bn": c.lectura_actual_bn,
                    "lectura_actual_color": c.lectura_actual_color,
                    "fecha_lectura_actual": str(c.fecha_lectura_actual),
                    "ubicacion": c.ubicacion,
                }
                for c in self.consumo_por_equipo
            ],
            "consumo_excedente_bn": self.consumo_excedente_bn,
            "consumo_excedente_color": self.consumo_excedente_color,
            "monto_renta": str(self.monto_renta),
            "monto_excedente": str(self.monto_excedente),
            "monto_iva": str(self.monto_iva),
            "monto_total": str(self.monto_total),
        }

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class ConsumoEquipo:
    asignacion_id: int
    equipo_numero_serie: str
    consumo_bn: int
    consumo_color: int


@dataclass
class ResultadoFacturacion:
    contrato_id: int
    periodo_mes: int
    periodo_anio: int
    fecha_inicio: date
    fecha_fin: date
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
            "consumo_por_equipo": [
                {
                    "asignacion_id": c.asignacion_id,
                    "equipo": c.equipo_numero_serie,
                    "consumo_bn": c.consumo_bn,
                    "consumo_color": c.consumo_color,
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

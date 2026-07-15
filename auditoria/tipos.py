from dataclasses import dataclass, field
from datetime import date


@dataclass
class AlertaAuditoria:
    candado: str  # 'a'..'g', ver auditoria/motor.py
    equipo_numero_serie: str
    bloqueante: bool
    mensaje: str
    datos: dict = field(default_factory=dict)


@dataclass
class ResultadoAuditoria:
    contrato_id: int
    fecha_inicio: date
    fecha_fin: date
    alertas: list[AlertaAuditoria] = field(default_factory=list)

    @property
    def aprobado(self) -> bool:
        return not any(a.bloqueante for a in self.alertas)

    @property
    def estado(self) -> str:
        return "aprobado" if self.aprobado else "pendiente"

    def to_dict(self) -> dict:
        return {
            "contrato_id": self.contrato_id,
            "fecha_inicio": str(self.fecha_inicio),
            "fecha_fin": str(self.fecha_fin),
            "estado": self.estado,
            "alertas": [
                {
                    "candado": a.candado,
                    "equipo": a.equipo_numero_serie,
                    "bloqueante": a.bloqueante,
                    "mensaje": a.mensaje,
                    "datos": a.datos,
                }
                for a in self.alertas
            ],
        }

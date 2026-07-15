from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from core.models import Asignacion, Equipo, Lectura, LogEjecucion, MetodoLectura
from integrations import IntegrationConnectionError, get_client

CAMPO_ID_EXTERNO = {
    MetodoLectura.API_3MANAGER: "id_externo_3manager",
    MetodoLectura.API_PRINTAUDIT: "id_externo_printaudit",
}


class Command(BaseCommand):
    help = (
        "Sincroniza lecturas diarias de equipos con metodo_lectura api_3manager o "
        "api_printaudit. Un fallo de una fuente o de un equipo individual no detiene "
        "el resto del job."
    )

    def handle(self, *args, **options):
        hoy = timezone.localdate()
        resumen = {"fecha": str(hoy), "fuentes": {}}
        hubo_alerta = False
        hubo_error_fuente = False

        for metodo in (MetodoLectura.API_3MANAGER, MetodoLectura.API_PRINTAUDIT):
            resumen_fuente = self._sincronizar_fuente(metodo, hoy)
            resumen["fuentes"][metodo] = resumen_fuente

            if resumen_fuente.get("error_fuente"):
                hubo_error_fuente = True
            elif (
                resumen_fuente["sin_respuesta_api"]
                or resumen_fuente["sin_asignacion_vigente"]
                or resumen_fuente["errores_equipo"]
            ):
                hubo_alerta = True

        if hubo_error_fuente:
            estado = LogEjecucion.Estado.ERROR
        elif hubo_alerta:
            estado = LogEjecucion.Estado.PENDIENTE
        else:
            estado = LogEjecucion.Estado.OK

        LogEjecucion.objects.create(estado=estado, detalle=resumen)

        self.stdout.write(self.style.SUCCESS(f"Sincronización de lecturas completada: {estado}"))

    def _sincronizar_fuente(self, metodo, hoy):
        equipos = Equipo.objects.filter(metodo_lectura=metodo).exclude(
            estado_actual=Equipo.EstadoActual.BAJA
        )
        resumen_fuente = {
            "equipos_evaluados": equipos.count(),
            "lecturas_creadas": 0,
            "lecturas_actualizadas": 0,
            "sin_respuesta_api": [],
            "sin_asignacion_vigente": [],
            "errores_equipo": [],
        }

        try:
            lecturas_api = get_client(metodo).obtener_lecturas()
        except IntegrationConnectionError as exc:
            self.stderr.write(self.style.ERROR(f"{metodo}: {exc}"))
            resumen_fuente["error_fuente"] = str(exc)
            return resumen_fuente

        campo_id_externo = CAMPO_ID_EXTERNO[metodo]

        for equipo in equipos:
            # Si no hay id_externo capturado a mano, se intenta emparejar por
            # numero_serie (3-Manager expone serialNumber en su respuesta).
            id_externo = getattr(equipo, campo_id_externo) or equipo.numero_serie
            if not id_externo:
                resumen_fuente["errores_equipo"].append(
                    {"equipo": equipo.numero_serie, "motivo": f"sin {campo_id_externo} ni numero_serie"}
                )
                continue

            lectura_externa = lecturas_api.get(str(id_externo))
            if lectura_externa is None:
                resumen_fuente["sin_respuesta_api"].append(equipo.numero_serie)
                continue

            asignacion = (
                Asignacion.objects.filter(equipo=equipo, fecha_inicio__lte=hoy)
                .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy))
                .order_by("-fecha_inicio")
                .first()
            )
            if asignacion is None:
                resumen_fuente["sin_asignacion_vigente"].append(equipo.numero_serie)
                continue

            try:
                _, creada = Lectura.objects.update_or_create(
                    asignacion=asignacion,
                    fecha=hoy,
                    defaults={
                        "lectura_bn": lectura_externa.lectura_bn,
                        "lectura_color": lectura_externa.lectura_color,
                        "origen": metodo,
                    },
                )
                resumen_fuente["lecturas_creadas" if creada else "lecturas_actualizadas"] += 1
            except Exception as exc:
                resumen_fuente["errores_equipo"].append(
                    {"equipo": equipo.numero_serie, "motivo": str(exc)}
                )

        return resumen_fuente

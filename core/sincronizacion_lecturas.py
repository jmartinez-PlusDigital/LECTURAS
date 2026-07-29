"""Sincronización de lecturas con las APIs externas (3-Manager, PrintAudit).

Compartido entre el management command `sincronizar_lecturas` (para el job
diario) y la acción "Sincronizar lecturas ahora" del Admin (ver
core/admin.py) — un solo lugar con la lógica real, ambos caminos solo la
invocan. Un fallo de una fuente completa o de un equipo individual no
detiene el resto (ver también integrations.Cliente3Manager.obtener_lecturas,
que aísla por cuenta dentro de una misma fuente).
"""
import logging

from django.db.models import Q
from django.utils import timezone

from core.models import Asignacion, Equipo, Lectura, LogEjecucion, MetodoLectura
from integrations import IntegrationConnectionError, get_client

logger = logging.getLogger(__name__)

CAMPO_ID_EXTERNO = {
    MetodoLectura.API_3MANAGER: "id_externo_3manager",
    MetodoLectura.API_PRINTAUDIT: "id_externo_printaudit",
}


def sincronizar_lecturas() -> LogEjecucion:
    """Sincroniza lecturas diarias de equipos con metodo_lectura api_3manager
    o api_printaudit, actualiza el estatus de conectividad de cada equipo
    (en_linea_api/ultima_actualizacion_api), y deja constancia en un
    LogEjecucion. Devuelve ese LogEjecucion (con `estado` y `detalle` ya
    resueltos) para que el llamador decida cómo reportarlo."""
    hoy = timezone.localdate()
    resumen = {"fecha": str(hoy), "fuentes": {}}
    hubo_alerta = False
    hubo_error_fuente = False

    for metodo in (MetodoLectura.API_3MANAGER, MetodoLectura.API_PRINTAUDIT):
        resumen_fuente = _sincronizar_fuente(metodo, hoy)
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

    return LogEjecucion.objects.create(estado=estado, detalle=resumen)


def _sincronizar_fuente(metodo, hoy) -> dict:
    equipos = Equipo.objects.filter(metodo_lectura=metodo).exclude(estado_actual=Equipo.EstadoActual.BAJA)
    resumen_fuente = {
        "equipos_evaluados": equipos.count(),
        "lecturas_creadas": 0,
        "lecturas_actualizadas": 0,
        "sin_respuesta_api": [],
        "sin_asignacion_vigente": [],
        "errores_equipo": [],
    }

    if resumen_fuente["equipos_evaluados"] == 0:
        # Ningún equipo depende de esta fuente: ni vale la pena conectarse,
        # ni una fuente sin configurar (p. ej. PrintAudit si no se usa) debe
        # ensuciar el estado del ciclo completo con un error irrelevante.
        return resumen_fuente

    try:
        lecturas_api = get_client(metodo).obtener_lecturas()
    except IntegrationConnectionError as exc:
        logger.error("%s: %s", metodo, exc)
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

        # Estatus de conectividad de la fuente (hoy solo 3-Manager lo
        # reporta): se guarda aunque el equipo no tenga asignación vigente,
        # para que el candado (g) y el dashboard lo vean.
        if lectura_externa.en_linea is not None or lectura_externa.ultima_actualizacion is not None:
            equipo.en_linea_api = lectura_externa.en_linea
            equipo.ultima_actualizacion_api = lectura_externa.ultima_actualizacion
            equipo.save(update_fields=["en_linea_api", "ultima_actualizacion_api"])

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
            resumen_fuente["errores_equipo"].append({"equipo": equipo.numero_serie, "motivo": str(exc)})

    return resumen_fuente

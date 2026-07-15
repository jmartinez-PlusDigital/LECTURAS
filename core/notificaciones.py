import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def enviar_resumen_ejecucion(resumenes: list[dict]) -> None:
    """Notifica por correo el resumen del ciclo diario de facturación.

    Solo envía si hay contratos con incidencias (pendiente/error_archivo/error).
    Si todo salió OK, no notifica para evitar ruido.
    """
    incidencias = [r for r in resumenes if r["estado"] != "ok"]
    if not incidencias:
        return

    if not settings.NOTIFICACIONES_EMAIL_DESTINO:
        logger.warning(
            "Hay %s contrato(s) con incidencias pero NOTIFICACIONES_EMAIL_DESTINO no está configurado.",
            len(incidencias),
        )
        return

    lineas = [f"- {r['contrato']}: {r['estado']} — {r.get('detalle', '')}" for r in incidencias]
    cuerpo = (
        f"Ciclo de facturación diaria completado con {len(incidencias)} contrato(s) con "
        f"incidencias de un total de {len(resumenes)} procesados:\n\n" + "\n".join(lineas)
    )

    send_mail(
        subject=f"[Facturación] {len(incidencias)} contrato(s) requieren atención",
        message=cuerpo,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=settings.NOTIFICACIONES_EMAIL_DESTINO,
        fail_silently=False,
    )

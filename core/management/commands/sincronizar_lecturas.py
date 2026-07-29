from django.core.management.base import BaseCommand

from core.sincronizacion_lecturas import sincronizar_lecturas


class Command(BaseCommand):
    help = (
        "Sincroniza lecturas diarias de equipos con metodo_lectura api_3manager o "
        "api_printaudit. Un fallo de una fuente o de un equipo individual no detiene "
        "el resto del job."
    )

    def handle(self, *args, **options):
        log = sincronizar_lecturas()
        self.stdout.write(self.style.SUCCESS(f"Sincronización de lecturas completada: {log.estado}"))

"""Importación masiva de equipos (y su asignación inicial) desde CSV o Excel.

Pensado para el onboarding inicial de un parque grande de equipos (p. ej. 1000
copiadoras) sin tener que capturarlos uno por uno en el Admin. También
disponible desde el navegador: botón "Importar equipos" en /admin/core/equipo/.

Ver `core/importacion_equipos.py` para el formato de columnas esperado.
"""
from django.core.management.base import BaseCommand, CommandError

from core.importacion_equipos import escribir_reporte_csv, procesar_archivo_equipos, resumen_de


class Command(BaseCommand):
    help = "Importa equipos (y su asignación inicial) en batch desde un archivo CSV o Excel."

    def add_arguments(self, parser):
        parser.add_argument("archivo", type=str, help="Ruta al archivo .csv, .xlsx o .xls")
        parser.add_argument(
            "--dry-run", action="store_true", help="Valida y muestra el resultado sin persistir nada."
        )
        parser.add_argument(
            "--reporte", type=str, default=None, help="Ruta donde escribir un CSV con el resultado por fila."
        )

    def handle(self, *args, **options):
        ruta = options["archivo"]
        dry_run = options["dry_run"]
        ruta_reporte = options["reporte"]

        try:
            resultados = procesar_archivo_equipos(ruta, ruta, dry_run=dry_run)
        except FileNotFoundError:
            raise CommandError(f"No se encontró el archivo: {ruta}")

        if not resultados:
            self.stdout.write(self.style.WARNING("El archivo no tiene filas de datos."))
            return

        self._imprimir_resumen(resultados, dry_run)
        if ruta_reporte:
            escribir_reporte_csv(ruta_reporte, resultados)
            self.stdout.write(f"Reporte escrito en: {ruta_reporte}")

    def _imprimir_resumen(self, resultados: list[dict], dry_run: bool) -> None:
        resumen = resumen_de(resultados)
        prefijo = "[DRY-RUN, no se escribió nada] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{prefijo}Filas procesadas: {resumen['total']}"))
        self.stdout.write(f"  Equipos creados: {resumen['equipos_creados']}")
        self.stdout.write(f"  Equipos actualizados: {resumen['equipos_actualizados']}")
        self.stdout.write(f"  Asignaciones creadas: {resumen['asignaciones_creadas']}")
        if resumen["con_error"]:
            self.stdout.write(self.style.ERROR(f"  Filas con error: {len(resumen['con_error'])}"))
            for r in resumen["con_error"]:
                self.stdout.write(self.style.ERROR(f"    fila {r['fila']} ({r['numero_serie']}): {r['error']}"))
        else:
            self.stdout.write(self.style.SUCCESS("  Sin errores."))

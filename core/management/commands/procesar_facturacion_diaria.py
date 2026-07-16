from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from core.models import Contrato
from core.notificaciones import enviar_resumen_ejecucion
from core.procesamiento_facturacion import generar_snapshot, procesar_contrato


class Command(BaseCommand):
    help = (
        "Orquesta el ciclo diario de facturación: identifica contratos con corte hoy, "
        "los audita, calcula, genera PDF/Excel (descargables desde el Admin) y persiste "
        "la factura. Un fallo en un contrato no detiene el procesamiento del resto."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--contrato",
            action="append",
            dest="contratos",
            metavar="NUMERO_CONTRATO",
            help=(
                "Fuerza el procesamiento de un contrato específico por su numero_contrato, "
                "sin importar si hoy coincide con su dia_corte_facturacion. Repetible para "
                "procesar varios a la vez: --contrato CT-001 --contrato CT-002."
            ),
        )
        parser.add_argument(
            "--fecha",
            type=str,
            default=None,
            metavar="YYYY-MM-DD",
            help=(
                "Fecha a usar como 'hoy' para el cálculo del periodo y el snapshot, en vez "
                "de la fecha real del sistema. Útil junto con --contrato para reprocesar un "
                "corte que no se ejecutó en su día."
            ),
        )

    def handle(self, *args, **options):
        hoy = self._resolver_fecha(options["fecha"])

        # Se resuelven los contratos ANTES del snapshot: si --contrato no
        # existe o no hay nada que procesar, no tiene sentido gastar un
        # respaldo de la base de datos completa.
        contratos = self._resolver_contratos(options["contratos"], hoy)
        if not contratos:
            self.stdout.write(self.style.WARNING("No hay contratos que procesar."))
            return

        try:
            ruta_snapshot = generar_snapshot(hoy)
        except Exception as exc:
            raise CommandError(f"No se pudo generar el snapshot de la base de datos: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"Snapshot generado: {ruta_snapshot}"))

        resumenes = [procesar_contrato(contrato, hoy) for contrato in contratos]

        enviar_resumen_ejecucion(resumenes)

        con_incidencias = [r for r in resumenes if r["estado"] != "ok"]
        for resumen in con_incidencias:
            self.stderr.write(self.style.ERROR(f"{resumen['contrato']} [{resumen['estado']}]: {resumen['detalle']}"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Ciclo de facturación completado: {len(resumenes)} contrato(s) procesados, "
                f"{len(con_incidencias)} con incidencias."
            )
        )

    # --- Resolución de fecha/contratos a procesar -----------------------

    def _resolver_fecha(self, valor: str | None) -> date:
        if not valor:
            return timezone.localdate()
        fecha = parse_date(valor)
        if fecha is None:
            raise CommandError(f"--fecha inválida: '{valor}'. Usa formato YYYY-MM-DD.")
        return fecha

    def _resolver_contratos(self, numeros_forzados: list[str] | None, hoy: date):
        if numeros_forzados:
            contratos = list(
                Contrato.objects.filter(
                    numero_contrato__in=numeros_forzados, estado=Contrato.Estado.ACTIVO
                ).select_related("cliente")
            )
            encontrados = {c.numero_contrato for c in contratos}
            faltantes = set(numeros_forzados) - encontrados
            if faltantes:
                raise CommandError(
                    "No se encontró contrato activo con numero_contrato: " + ", ".join(sorted(faltantes))
                )
            return contratos

        return list(
            Contrato.objects.filter(
                estado=Contrato.Estado.ACTIVO, dia_corte_facturacion=hoy.day
            ).select_related("cliente")
        )

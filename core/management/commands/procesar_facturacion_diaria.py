import calendar
from datetime import date, timedelta

from django.conf import settings
from django.core import management
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from auditoria import auditar_contrato
from core.models import Contrato, Factura, Lectura, LogEjecucion
from core.notificaciones import enviar_resumen_ejecucion
from documentos import generar_excel_factura, generar_pdf_factura
from drive import DriveUploadError, subir_documentos_factura
from facturacion import calcular_factura, persistir_factura


class Command(BaseCommand):
    help = (
        "Orquesta el ciclo diario de facturación: identifica contratos con corte hoy, "
        "los audita, calcula, genera PDF/Excel, sube a Drive y persiste la factura. "
        "Un fallo en un contrato no detiene el procesamiento del resto."
    )

    def handle(self, *args, **options):
        hoy = timezone.localdate()

        ruta_snapshot = self._generar_snapshot(hoy)
        self.stdout.write(self.style.SUCCESS(f"Snapshot generado: {ruta_snapshot}"))

        contratos = Contrato.objects.filter(
            estado=Contrato.Estado.ACTIVO, dia_corte_facturacion=hoy.day
        ).select_related("cliente")

        resumenes = [self._procesar_contrato(contrato, hoy) for contrato in contratos]

        enviar_resumen_ejecucion(resumenes)

        con_incidencias = [r for r in resumenes if r["estado"] != "ok"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Ciclo de facturación completado: {len(resumenes)} contrato(s) procesados, "
                f"{len(con_incidencias)} con incidencias."
            )
        )

    # --- Snapshot ------------------------------------------------------

    def _generar_snapshot(self, hoy: date) -> str:
        backups_dir = settings.BASE_DIR / "backups"
        backups_dir.mkdir(exist_ok=True)
        ruta = backups_dir / f"snapshot_{hoy.isoformat()}_{timezone.localtime():%H%M%S}.json"
        try:
            with open(ruta, "w", encoding="utf-8") as archivo:
                management.call_command(
                    "dumpdata",
                    exclude=["contenttypes", "auth.permission", "admin.logentry", "sessions.session"],
                    natural_foreign=True,
                    stdout=archivo,
                )
        except Exception as exc:
            LogEjecucion.objects.create(
                estado=LogEjecucion.Estado.ERROR, detalle={"fase": "snapshot", "error": str(exc)}
            )
            raise CommandError(f"No se pudo generar el snapshot de la base de datos: {exc}") from exc
        return str(ruta)

    # --- Por contrato ----------------------------------------------------

    def _procesar_contrato(self, contrato: Contrato, hoy: date) -> dict:
        fecha_inicio, fecha_fin = self._calcular_periodo(contrato, hoy)
        periodo_mes, periodo_anio = hoy.month, hoy.year

        try:
            resultado_auditoria = auditar_contrato(contrato, fecha_inicio, fecha_fin)
        except Exception as exc:
            return self._registrar_error(contrato, "auditoria", exc)

        if not resultado_auditoria.aprobado:
            LogEjecucion.objects.create(
                contrato=contrato,
                estado=LogEjecucion.Estado.PENDIENTE,
                detalle={"fase": "auditoria", **resultado_auditoria.to_dict()},
            )
            return {"contrato": contrato.numero_contrato, "estado": "pendiente", "detalle": "alertas de auditoría sin resolver"}

        try:
            resultado_calculo = calcular_factura(
                contrato, fecha_inicio, fecha_fin, periodo_mes, periodo_anio, simulacion=True
            )
            pdf_bytes = generar_pdf_factura(resultado_calculo, contrato)
            excel_bytes = generar_excel_factura(resultado_calculo, contrato)
        except Exception as exc:
            return self._registrar_error(contrato, "calculo_o_documentos", exc)

        nombre_base = f"{contrato.numero_contrato}_{periodo_anio}-{periodo_mes:02d}"
        try:
            pdf_url, excel_url = subir_documentos_factura(
                carpeta_id=contrato.carpeta_drive_destino_id,
                nombre_base=nombre_base,
                pdf_bytes=pdf_bytes,
                excel_bytes=excel_bytes,
            )
        except DriveUploadError as exc:
            LogEjecucion.objects.create(
                contrato=contrato,
                estado=LogEjecucion.Estado.ERROR_ARCHIVO,
                detalle={"fase": "subida_drive", "error": str(exc)},
            )
            self.stderr.write(self.style.ERROR(f"{contrato.numero_contrato}: {exc}"))
            return {"contrato": contrato.numero_contrato, "estado": "error_archivo", "detalle": str(exc)}

        # Solo si la subida fue exitosa se persisten Lecturas + Factura, atómicamente.
        with transaction.atomic():
            asignaciones_ids = [c.asignacion_id for c in resultado_calculo.consumo_por_equipo]
            Lectura.objects.filter(
                asignacion_id__in=asignaciones_ids, fecha__gte=fecha_inicio, fecha__lte=fecha_fin
            ).update(estado_auditoria=Lectura.EstadoAuditoria.OK)

            factura = persistir_factura(
                resultado_calculo, pdf_url=pdf_url, excel_url=excel_url, estado=Factura.Estado.OK
            )

            LogEjecucion.objects.create(
                contrato=contrato,
                estado=LogEjecucion.Estado.OK,
                detalle={"fase": "completo", "factura_id": factura.id, "monto_total": str(factura.monto_total)},
                enlaces_generados={"pdf": pdf_url, "excel": excel_url},
            )

        return {"contrato": contrato.numero_contrato, "estado": "ok", "detalle": f"factura {factura.id}"}

    def _registrar_error(self, contrato: Contrato, fase: str, exc: Exception) -> dict:
        LogEjecucion.objects.create(
            contrato=contrato,
            estado=LogEjecucion.Estado.ERROR,
            detalle={"fase": fase, "error": str(exc)},
        )
        self.stderr.write(self.style.ERROR(f"{contrato.numero_contrato} [{fase}]: {exc}"))
        return {"contrato": contrato.numero_contrato, "estado": "error", "detalle": str(exc)}

    # --- Periodo de facturación ------------------------------------------

    def _calcular_periodo(self, contrato: Contrato, hoy: date) -> tuple[date, date]:
        ultima_factura = contrato.facturas.order_by("-periodo_anio", "-periodo_mes").first()
        if ultima_factura is not None:
            fecha_inicio = self._dia_siguiente_a_corte_anterior(hoy, contrato.dia_corte_facturacion)
        else:
            fecha_inicio = contrato.fecha_inicio
        return max(fecha_inicio, contrato.fecha_inicio), hoy

    @staticmethod
    def _dia_siguiente_a_corte_anterior(hoy: date, dia_corte: int) -> date:
        if hoy.month == 1:
            year, month = hoy.year - 1, 12
        else:
            year, month = hoy.year, hoy.month - 1
        ultimo_dia_mes = calendar.monthrange(year, month)[1]
        fecha_corte_anterior = date(year, month, min(dia_corte, ultimo_dia_mes))
        return fecha_corte_anterior + timedelta(days=1)

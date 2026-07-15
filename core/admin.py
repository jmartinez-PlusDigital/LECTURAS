import json
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse
from django.shortcuts import render
from django.urls import path
from django.utils.html import format_html, format_html_join

from core.importacion_equipos import procesar_archivo_equipos, resumen_de

from .models import (
    Asignacion,
    Cliente,
    Contrato,
    Equipo,
    Factura,
    Lectura,
    LogEjecucion,
)


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = (
        "nombre",
        "razon_social",
        "rfc",
        "contacto_email",
        "contacto_telefono",
        "id_cuenta_3manager",
        "activo",
    )
    list_filter = ("activo",)
    search_fields = ("nombre", "razon_social", "rfc", "contacto_email")


class ContratoForm(forms.ModelForm):
    class Meta:
        model = Contrato
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        fecha_inicio = cleaned_data.get("fecha_inicio")
        fecha_fin = cleaned_data.get("fecha_fin")
        if fecha_inicio and fecha_fin and fecha_fin <= fecha_inicio:
            raise ValidationError("La fecha de fin debe ser posterior a la fecha de inicio.")
        return cleaned_data


@admin.register(Contrato)
class ContratoAdmin(admin.ModelAdmin):
    form = ContratoForm
    list_display = (
        "numero_contrato",
        "cliente",
        "estado",
        "renta_base",
        "dia_corte_facturacion",
        "fecha_inicio",
        "fecha_fin",
    )
    list_filter = ("estado",)
    search_fields = ("numero_contrato", "cliente__nombre")
    autocomplete_fields = ("cliente",)
    date_hierarchy = "fecha_inicio"


class EquipoForm(forms.ModelForm):
    class Meta:
        model = Equipo
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        metodo = cleaned_data.get("metodo_lectura")
        # id_externo_3manager es opcional: si se deja en blanco, la sincronización
        # empareja por numero_serie (3-Manager expone serialNumber en su API).
        if metodo == "api_printaudit" and not cleaned_data.get("id_externo_printaudit"):
            raise ValidationError(
                "Un equipo con método de lectura API PrintAudit requiere id_externo_printaudit."
            )
        return cleaned_data


class ImportarEquiposForm(forms.Form):
    archivo = forms.FileField(label="Archivo (.csv, .xlsx o .xls)")
    dry_run = forms.BooleanField(
        label="Solo validar, no guardar todavía", required=False, initial=True
    )

    def clean_archivo(self):
        archivo = self.cleaned_data["archivo"]
        if not archivo.name.lower().endswith((".csv", ".xlsx", ".xls")):
            raise ValidationError("El archivo debe ser .csv, .xlsx o .xls.")
        return archivo


@admin.register(Equipo)
class EquipoAdmin(admin.ModelAdmin):
    form = EquipoForm
    change_list_template = "admin/core/equipo/change_list.html"
    list_display = (
        "numero_serie",
        "marca",
        "modelo",
        "metodo_lectura",
        "estado_actual",
        "permite_reset_contador",
    )
    list_filter = ("metodo_lectura", "estado_actual", "marca")
    search_fields = ("numero_serie", "marca", "modelo", "id_externo_3manager", "id_externo_printaudit")

    def get_urls(self):
        urls = [
            path(
                "importar/",
                self.admin_site.admin_view(self.importar_equipos_view),
                name="core_equipo_importar",
            ),
            path(
                "importar/plantilla/",
                self.admin_site.admin_view(self.descargar_plantilla_view),
                name="core_equipo_importar_plantilla",
            ),
        ]
        return urls + super().get_urls()

    def importar_equipos_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        resultados = None
        resumen = None
        dry_run = True

        if request.method == "POST":
            form = ImportarEquiposForm(request.POST, request.FILES)
            if form.is_valid():
                archivo = form.cleaned_data["archivo"]
                dry_run = form.cleaned_data["dry_run"]
                resultados = procesar_archivo_equipos(archivo, archivo.name, dry_run=dry_run)
                if not resultados:
                    messages.warning(request, "El archivo no tiene filas de datos.")
                else:
                    resumen = resumen_de(resultados)
                    if dry_run:
                        messages.info(request, "Validación en seco: no se guardó ningún cambio todavía.")
                    elif resumen["con_error"]:
                        messages.warning(
                            request,
                            f"Importación completada con {len(resumen['con_error'])} fila(s) con error.",
                        )
                    else:
                        messages.success(request, f"Importación completada: {resumen['total']} fila(s) procesadas.")
        else:
            form = ImportarEquiposForm()

        contexto = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Importar equipos",
            "form": form,
            "resultados": resultados,
            "resumen": resumen,
        }
        return render(request, "admin/core/equipo/importar_equipos.html", contexto)

    def descargar_plantilla_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        ruta = Path(settings.BASE_DIR) / "plantillas" / "plantilla_importar_equipos.csv"
        return FileResponse(open(ruta, "rb"), as_attachment=True, filename=ruta.name)


class LecturaInline(admin.TabularInline):
    model = Lectura
    extra = 0
    fields = ("fecha", "lectura_bn", "lectura_color", "origen", "estado_auditoria")


class AsignacionForm(forms.ModelForm):
    class Meta:
        model = Asignacion
        fields = "__all__"

    def clean(self):
        # La regla "un equipo no puede tener dos asignaciones activas
        # simultáneas" la valida automáticamente Django al llamar a
        # full_clean(), que incluye el UniqueConstraint condicional
        # `unique_asignacion_activa_por_equipo` definido en el modelo.
        cleaned_data = super().clean()
        fecha_inicio = cleaned_data.get("fecha_inicio")
        fecha_fin = cleaned_data.get("fecha_fin")
        lectura_cierre_bn = cleaned_data.get("lectura_cierre_bn")
        lectura_cierre_color = cleaned_data.get("lectura_cierre_color")

        if fecha_inicio and fecha_fin and fecha_fin < fecha_inicio:
            raise ValidationError("La fecha de fin no puede ser anterior a la fecha de inicio.")

        if (lectura_cierre_bn is not None or lectura_cierre_color is not None) and not fecha_fin:
            raise ValidationError(
                "No puedes capturar una lectura de cierre sin establecer la fecha de fin."
            )

        return cleaned_data


@admin.register(Asignacion)
class AsignacionAdmin(admin.ModelAdmin):
    form = AsignacionForm
    list_display = ("equipo", "contrato", "fecha_inicio", "fecha_fin", "esta_activa")
    list_filter = ("contrato__estado", "fecha_fin")
    search_fields = ("equipo__numero_serie", "contrato__numero_contrato")
    autocomplete_fields = ("equipo", "contrato")
    date_hierarchy = "fecha_inicio"
    inlines = [LecturaInline]

    @admin.display(boolean=True, description="Activa")
    def esta_activa(self, obj):
        return obj.fecha_fin is None


class LecturaForm(forms.ModelForm):
    class Meta:
        model = Lectura
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        asignacion = cleaned_data.get("asignacion")
        fecha = cleaned_data.get("fecha")
        if asignacion and fecha:
            if fecha < asignacion.fecha_inicio:
                raise ValidationError(
                    "La fecha de la lectura no puede ser anterior al inicio de la asignación."
                )
            if asignacion.fecha_fin and fecha > asignacion.fecha_fin:
                raise ValidationError(
                    "La fecha de la lectura no puede ser posterior al cierre de la asignación."
                )
        return cleaned_data


@admin.register(Lectura)
class LecturaAdmin(admin.ModelAdmin):
    form = LecturaForm
    list_display = ("asignacion", "fecha", "lectura_bn", "lectura_color", "origen", "estado_auditoria")
    list_filter = ("origen", "estado_auditoria")
    search_fields = ("asignacion__equipo__numero_serie", "asignacion__contrato__numero_contrato")
    autocomplete_fields = ("asignacion",)
    date_hierarchy = "fecha"


@admin.register(Factura)
class FacturaAdmin(admin.ModelAdmin):
    list_display = (
        "contrato",
        "periodo_mes",
        "periodo_anio",
        "monto_total",
        "estado",
        "fecha_generacion",
        "enlace_pdf",
        "enlace_excel",
    )
    list_filter = ("estado", "periodo_anio", "periodo_mes")
    search_fields = ("contrato__numero_contrato",)
    autocomplete_fields = ("contrato",)

    def has_add_permission(self, request):
        # Las facturas las genera el orquestador de facturación (módulo 8),
        # no se capturan manualmente desde el Admin.
        return False

    @admin.display(description="PDF")
    def enlace_pdf(self, obj):
        if obj.pdf_url:
            return format_html('<a href="{}" target="_blank">PDF</a>', obj.pdf_url)
        return "—"

    @admin.display(description="Excel")
    def enlace_excel(self, obj):
        if obj.excel_url:
            return format_html('<a href="{}" target="_blank">Excel</a>', obj.excel_url)
        return "—"


@admin.register(LogEjecucion)
class LogEjecucionAdmin(admin.ModelAdmin):
    """Panel de resumen del histórico de ejecuciones (módulo 9).

    Filtrable por fecha (date_hierarchy + filtro de timestamp) y por estado
    (list_filter), con enlaces directos a los PDF/Excel subidos a Drive.
    """

    list_display = ("timestamp", "contrato", "estado", "enlaces_documentos")
    list_filter = ("estado", "timestamp")
    search_fields = ("contrato__numero_contrato",)
    date_hierarchy = "timestamp"
    readonly_fields = ("contrato", "timestamp", "estado", "detalle_formateado", "enlaces_generados_formateado")
    fields = ("contrato", "timestamp", "estado", "detalle_formateado", "enlaces_generados_formateado")

    def has_add_permission(self, request):
        return False

    @admin.display(description="Documentos")
    def enlaces_documentos(self, obj):
        enlaces = obj.enlaces_generados or {}
        items = [(nombre, enlaces[clave]) for clave, nombre in (("pdf", "PDF"), ("excel", "Excel")) if enlaces.get(clave)]
        if not items:
            return "—"
        return format_html_join(" | ", '<a href="{}" target="_blank">{}</a>', ((url, nombre) for nombre, url in items))

    @admin.display(description="Detalle")
    def detalle_formateado(self, obj):
        return format_html("<pre>{}</pre>", json.dumps(obj.detalle, indent=2, ensure_ascii=False))

    @admin.display(description="Enlaces generados")
    def enlaces_generados_formateado(self, obj):
        return format_html(
            "<pre>{}</pre>", json.dumps(obj.enlaces_generados, indent=2, ensure_ascii=False)
        )

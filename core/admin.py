import json
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.html import format_html, format_html_join

from core.dashboard import contexto_dashboard
from core.historial_lecturas import generar_excel_historial, historial_de_contrato
from core.importacion_3manager import importar_equipos_de_cliente_3manager
from core.importacion_clientes import procesar_archivo_clientes
from core.importacion_clientes import resumen_de as resumen_de_clientes
from core.importacion_contratos import procesar_archivo_contratos
from core.importacion_contratos import resumen_de as resumen_de_contratos
from core.importacion_equipos import asignar_equipo_a_contrato, procesar_archivo_equipos
from core.importacion_equipos import resumen_de as resumen_de_equipos
from core.importacion_lecturas import generar_plantilla_lecturas, procesar_archivo_lecturas
from core.importacion_lecturas import resumen_de as resumen_de_lecturas
from core.notificaciones import enviar_resumen_ejecucion
from core.procesamiento_facturacion import generar_snapshot, procesar_contrato
from core.sincronizacion_lecturas import sincronizar_lecturas
from integrations.exceptions import IntegrationConnectionError
from integrations.tresmanager import Cliente3Manager

from .models import (
    Asignacion,
    Cliente,
    Contrato,
    EmpresaEmisora,
    Equipo,
    Factura,
    Lectura,
    LogEjecucion,
)


@admin.action(description="3-Manager: buscar cuenta por nombre")
def buscar_cuenta_3manager(modeladmin, request, queryset):
    pendientes = list(queryset.filter(id_cuenta_3manager=""))
    for cliente in queryset.exclude(id_cuenta_3manager=""):
        messages.info(request, f"{cliente.nombre}: ya tiene id_cuenta_3manager, se omite.")

    if not pendientes:
        return

    try:
        cuentas = Cliente3Manager().buscar_cuentas()
    except IntegrationConnectionError as exc:
        messages.error(request, f"No se pudo conectar a 3-Manager: {exc}")
        return

    for cliente in pendientes:
        coincidencias = [c for c in cuentas if cliente.nombre.lower() in str(c.get("name", "")).lower()]
        if len(coincidencias) == 1:
            cliente.id_cuenta_3manager = coincidencias[0]["id"]
            cliente.save(update_fields=["id_cuenta_3manager"])
            messages.success(
                request, f"{cliente.nombre}: cuenta encontrada y guardada ({coincidencias[0]['name']})."
            )
        elif not coincidencias:
            messages.warning(request, f"{cliente.nombre}: no se encontró ninguna cuenta con ese nombre en 3-Manager.")
        else:
            nombres = ", ".join(c.get("name", "") for c in coincidencias)
            messages.warning(
                request,
                f"{cliente.nombre}: hay {len(coincidencias)} cuentas parecidas ({nombres}); "
                "asígnala manualmente.",
            )


@admin.action(description="3-Manager: importar equipos de la cuenta")
def importar_equipos_3manager(modeladmin, request, queryset):
    for cliente in queryset:
        if not cliente.id_cuenta_3manager:
            messages.warning(request, f"{cliente.nombre}: no tiene id_cuenta_3manager configurado, se omite.")
            continue
        try:
            resultados = importar_equipos_de_cliente_3manager(cliente)
        except IntegrationConnectionError as exc:
            messages.error(request, f"{cliente.nombre}: fallo de conexión con 3-Manager — {exc}")
            continue

        creados = sum(1 for r in resultados if r["equipo"] == "creado")
        actualizados = sum(1 for r in resultados if r["equipo"] == "actualizado")
        con_error = [r for r in resultados if r["error"]]

        if creados or actualizados:
            messages.success(
                request, f"{cliente.nombre}: {creados} equipo(s) creado(s), {actualizados} actualizado(s)."
            )
        if con_error:
            detalle = "; ".join(f"{r['nombre_3manager'] or '(sin nombre)'}: {r['error']}" for r in con_error)
            messages.warning(request, f"{cliente.nombre}: {len(con_error)} con error — {detalle}")


class ImportarClientesForm(forms.Form):
    archivo = forms.FileField(label="Archivo (.csv, .xlsx o .xls)")
    dry_run = forms.BooleanField(
        label="Solo validar, no guardar todavía", required=False, initial=True
    )

    def clean_archivo(self):
        archivo = self.cleaned_data["archivo"]
        if not archivo.name.lower().endswith((".csv", ".xlsx", ".xls")):
            raise ValidationError("El archivo debe ser .csv, .xlsx o .xls.")
        return archivo


@admin.register(EmpresaEmisora)
class EmpresaEmisoraAdmin(admin.ModelAdmin):
    list_display = ("nombre", "logo_preview")
    search_fields = ("nombre",)

    @admin.display(description="Logo")
    def logo_preview(self, obj):
        if obj.logo:
            return format_html('<img src="{}" style="height: 32px;">', obj.logo.url)
        return "— (sube el logo en esta ficha)"


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    change_list_template = "admin/core/cliente/change_list.html"
    list_display = (
        "nombre",
        "razon_social",
        "rfc",
        "contacto_email",
        "contacto_telefono",
        "id_cuenta_3manager",
        "activo",
        "enlace_equipos",
    )
    list_filter = ("activo",)
    search_fields = ("nombre", "razon_social", "rfc", "contacto_email")
    actions = [buscar_cuenta_3manager, importar_equipos_3manager]

    @admin.display(description="Equipos")
    def enlace_equipos(self, obj):
        url = reverse("admin:core_equipo_changelist")
        return format_html(
            '<a href="{}?asignaciones__contrato__cliente__id__exact={}">Ver equipos</a>', url, obj.pk
        )

    def get_urls(self):
        urls = [
            path(
                "importar/",
                self.admin_site.admin_view(self.importar_clientes_view),
                name="core_cliente_importar",
            ),
            path(
                "importar/plantilla/",
                self.admin_site.admin_view(self.descargar_plantilla_view),
                name="core_cliente_importar_plantilla",
            ),
        ]
        return urls + super().get_urls()

    def importar_clientes_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        resultados = None
        resumen = None

        if request.method == "POST":
            form = ImportarClientesForm(request.POST, request.FILES)
            if form.is_valid():
                archivo = form.cleaned_data["archivo"]
                dry_run = form.cleaned_data["dry_run"]
                resultados = procesar_archivo_clientes(archivo, archivo.name, dry_run=dry_run)
                if not resultados:
                    messages.warning(request, "El archivo no tiene filas de datos.")
                else:
                    resumen = resumen_de_clientes(resultados)
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
            form = ImportarClientesForm()

        contexto = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Importar clientes",
            "form": form,
            "resultados": resultados,
            "resumen": resumen,
        }
        return render(request, "admin/core/cliente/importar_clientes.html", contexto)

    def descargar_plantilla_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        ruta = Path(settings.BASE_DIR) / "plantillas" / "plantilla_importar_clientes.csv"
        return FileResponse(open(ruta, "rb"), as_attachment=True, filename=ruta.name)


def _leer_rango_fechas(request):
    """Lee ?desde=YYYY-MM-DD&hasta=YYYY-MM-DD del querystring (ambos opcionales).
    Una fecha con formato inválido se ignora en vez de tronar la página."""
    fecha_desde = parse_date(request.GET.get("desde", "") or "")
    fecha_hasta = parse_date(request.GET.get("hasta", "") or "")
    return fecha_desde, fecha_hasta


def _facturar_contratos(request, contratos: list[Contrato]) -> None:
    """Corre el ciclo de facturación para `contratos` y reporta el resultado
    vía el framework de mensajes. Usado tanto por la acción masiva del Admin
    como por el botón "Facturar ahora" de un clic del dashboard."""
    if not contratos:
        return

    hoy = timezone.localdate()
    try:
        generar_snapshot(hoy)
    except Exception as exc:
        messages.error(request, f"No se pudo generar el respaldo previo de la base de datos, se cancela: {exc}")
        return

    resumenes = [procesar_contrato(contrato, hoy) for contrato in contratos]
    enviar_resumen_ejecucion(resumenes)

    for resumen in resumenes:
        if resumen["estado"] == "ok":
            messages.success(request, f"{resumen['contrato']}: facturado correctamente ({resumen['detalle']}).")
        elif resumen["estado"] == "pendiente":
            messages.warning(
                request,
                f"{resumen['contrato']}: pendiente — {resumen['detalle']}. Revisa las alertas de auditoría "
                "(Lecturas) antes de forzar de nuevo.",
            )
        else:
            messages.error(request, f"{resumen['contrato']}: {resumen['detalle']}")


@admin.action(description="Facturación: facturar ahora (fuerza el corte hoy)")
def facturar_ahora(modeladmin, request, queryset):
    contratos = list(queryset.filter(estado=Contrato.Estado.ACTIVO))
    for contrato in queryset.exclude(estado=Contrato.Estado.ACTIVO):
        messages.warning(request, f"{contrato.numero_contrato}: no está activo, se omite.")
    _facturar_contratos(request, contratos)


def dashboard_view(request):
    """Panel de inicio del Admin: reemplaza el listado plano de modelos de
    Jazzmin por lo que hay que revisar cada día (ver core/dashboard.py)."""
    contexto = {
        **admin.site.each_context(request),
        "title": "Inicio",
        **contexto_dashboard(),
    }
    return render(request, "admin/index.html", contexto)


def busqueda_global_view(request):
    """Búsqueda de un cliente/contrato/equipo desde una sola caja en el
    navbar, en vez de tener que adivinar en qué listado buscar. Reemplaza
    las tres cajas de búsqueda por-modelo que ponía Jazzmin (ver
    `admin/base.html`, ya no se usan)."""
    query = (request.GET.get("q") or "").strip()

    clientes = contratos = equipos = []
    if query:
        clientes = list(
            Cliente.objects.filter(
                Q(nombre__icontains=query) | Q(razon_social__icontains=query) | Q(rfc__icontains=query)
            ).order_by("nombre")[:20]
        )
        contratos = list(
            Contrato.objects.select_related("cliente")
            .filter(Q(numero_contrato__icontains=query) | Q(cliente__nombre__icontains=query))
            .order_by("numero_contrato")[:20]
        )
        equipos = list(
            Equipo.objects.prefetch_related("asignaciones__contrato__cliente")
            .filter(Q(numero_serie__icontains=query) | Q(marca__icontains=query) | Q(modelo__icontains=query))
            .order_by("numero_serie")[:20]
        )
        for equipo in equipos:
            equipo.asignacion_activa = next(
                (a for a in equipo.asignaciones.all() if a.fecha_fin is None), None
            )

    contexto = {
        **admin.site.each_context(request),
        "title": f"Buscar: {query}" if query else "Buscar",
        "query": query,
        "clientes": clientes,
        "contratos": contratos,
        "equipos": equipos,
        "total": len(clientes) + len(contratos) + len(equipos),
    }
    return render(request, "admin/busqueda_global.html", contexto)


def facturar_uno_ahora_view(request, contrato_id):
    if request.method != "POST":
        raise PermissionDenied
    if not request.user.has_perm("core.change_contrato"):
        raise PermissionDenied
    contrato = get_object_or_404(Contrato, pk=contrato_id, estado=Contrato.Estado.ACTIVO)
    _facturar_contratos(request, [contrato])
    return redirect("admin:index")


def sincronizar_lecturas_ahora_view(request):
    """Botón "Sincronizar lecturas ahora" del dashboard: corre bajo demanda
    lo mismo que el job diario (ver core.sincronizacion_lecturas), para no
    tener que esperar al cron/tarea programada (que hoy, de hecho, todavía
    no existe) ni abrir una terminal."""
    if request.method != "POST":
        raise PermissionDenied
    if not request.user.has_perm("core.change_equipo"):
        raise PermissionDenied

    log = sincronizar_lecturas()
    totales = {"creadas": 0, "actualizadas": 0, "sin_respuesta": 0, "errores": 0}
    for resumen_fuente in log.detalle.get("fuentes", {}).values():
        totales["creadas"] += resumen_fuente.get("lecturas_creadas", 0)
        totales["actualizadas"] += resumen_fuente.get("lecturas_actualizadas", 0)
        totales["sin_respuesta"] += len(resumen_fuente.get("sin_respuesta_api", []))
        totales["errores"] += len(resumen_fuente.get("errores_equipo", []))

    resumen_texto = (
        f"{totales['creadas']} lectura(s) creada(s), {totales['actualizadas']} actualizada(s)"
    )
    if log.estado == LogEjecucion.Estado.OK:
        messages.success(request, f"Sincronización completada: {resumen_texto}.")
    elif log.estado == LogEjecucion.Estado.PENDIENTE:
        messages.warning(
            request,
            f"Sincronización completada con alertas ({resumen_texto}; "
            f"{totales['sin_respuesta']} sin respuesta, {totales['errores']} con error) — "
            "revisa el detalle en Sistema → Historial de ejecución.",
        )
    else:
        messages.error(
            request,
            "Una o más fuentes fallaron por completo — revisa el detalle en "
            "Sistema → Historial de ejecución.",
        )
    return redirect("admin:index")


@admin.action(description="Facturación: descargar plantilla de lecturas")
def descargar_plantilla_lecturas(modeladmin, request, queryset):
    contratos = list(queryset)
    contenido = generar_plantilla_lecturas(contratos)
    if len(contratos) == 1:
        nombre = f"plantilla_lecturas_{contratos[0].numero_contrato}.csv"
    else:
        nombre = "plantilla_lecturas.csv"
    response = HttpResponse(contenido, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{nombre}"'
    return response


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


class ImportarContratosForm(forms.Form):
    archivo = forms.FileField(label="Archivo (.csv, .xlsx o .xls)")
    dry_run = forms.BooleanField(
        label="Solo validar, no guardar todavía", required=False, initial=True
    )

    def clean_archivo(self):
        archivo = self.cleaned_data["archivo"]
        if not archivo.name.lower().endswith((".csv", ".xlsx", ".xls")):
            raise ValidationError("El archivo debe ser .csv, .xlsx o .xls.")
        return archivo


@admin.register(Contrato)
class ContratoAdmin(admin.ModelAdmin):
    form = ContratoForm
    change_list_template = "admin/core/contrato/change_list.html"
    list_display = (
        "numero_contrato",
        "cliente",
        "emisor",
        "estado",
        "moneda",
        "renta_base",
        "dia_corte_facturacion",
        "fecha_inicio",
        "fecha_fin",
        "enlace_historial",
        "enlace_equipos",
    )
    list_filter = ("estado", "moneda", "emisor")
    search_fields = ("numero_contrato", "cliente__nombre")
    autocomplete_fields = ("cliente",)
    date_hierarchy = "fecha_inicio"
    actions = [facturar_ahora, descargar_plantilla_lecturas]

    @admin.display(description="Historial")
    def enlace_historial(self, obj):
        url = reverse("admin:core_contrato_historial_lecturas", args=[obj.pk])
        return format_html('<a href="{}">Ver historial de lecturas</a>', url)

    @admin.display(description="Equipos")
    def enlace_equipos(self, obj):
        url = reverse("admin:core_equipo_changelist")
        return format_html('<a href="{}?asignaciones__contrato__id__exact={}">Ver equipos</a>', url, obj.pk)

    def get_urls(self):
        urls = [
            path(
                "<int:contrato_id>/historial-lecturas/",
                self.admin_site.admin_view(self.historial_lecturas_view),
                name="core_contrato_historial_lecturas",
            ),
            path(
                "<int:contrato_id>/historial-lecturas/excel/",
                self.admin_site.admin_view(self.historial_lecturas_excel_view),
                name="core_contrato_historial_lecturas_excel",
            ),
            path(
                "importar/",
                self.admin_site.admin_view(self.importar_contratos_view),
                name="core_contrato_importar",
            ),
            path(
                "importar/plantilla/",
                self.admin_site.admin_view(self.descargar_plantilla_contratos_view),
                name="core_contrato_importar_plantilla",
            ),
        ]
        return urls + super().get_urls()

    def importar_contratos_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        resultados = None
        resumen = None

        if request.method == "POST":
            form = ImportarContratosForm(request.POST, request.FILES)
            if form.is_valid():
                archivo = form.cleaned_data["archivo"]
                dry_run = form.cleaned_data["dry_run"]
                resultados = procesar_archivo_contratos(archivo, archivo.name, dry_run=dry_run)
                if not resultados:
                    messages.warning(request, "El archivo no tiene filas de datos.")
                else:
                    resumen = resumen_de_contratos(resultados)
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
            form = ImportarContratosForm()

        contexto = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Importar contratos",
            "form": form,
            "resultados": resultados,
            "resumen": resumen,
        }
        return render(request, "admin/core/contrato/importar_contratos.html", contexto)

    def descargar_plantilla_contratos_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        ruta = Path(settings.BASE_DIR) / "plantillas" / "plantilla_importar_contratos.csv"
        return FileResponse(open(ruta, "rb"), as_attachment=True, filename=ruta.name)

    def historial_lecturas_view(self, request, contrato_id):
        contrato = get_object_or_404(Contrato, pk=contrato_id)
        fecha_desde, fecha_hasta = _leer_rango_fechas(request)
        filas = historial_de_contrato(contrato, fecha_desde=fecha_desde, fecha_hasta=fecha_hasta)
        contexto = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Historial de lecturas — {contrato.numero_contrato}",
            "contrato": contrato,
            "filas": filas,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
        }
        return render(request, "admin/core/contrato/historial_lecturas.html", contexto)

    def historial_lecturas_excel_view(self, request, contrato_id):
        contrato = get_object_or_404(Contrato, pk=contrato_id)
        fecha_desde, fecha_hasta = _leer_rango_fechas(request)
        filas = historial_de_contrato(contrato, fecha_desde=fecha_desde, fecha_hasta=fecha_hasta)
        contenido = generar_excel_historial(contrato, filas, fecha_desde=fecha_desde, fecha_hasta=fecha_hasta)
        response = HttpResponse(
            contenido, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = f'attachment; filename="historial_lecturas_{contrato.numero_contrato}.xlsx"'
        return response


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


class ImportarLecturasForm(forms.Form):
    archivo = forms.FileField(label="Archivo (.csv, .xlsx o .xls)")
    dry_run = forms.BooleanField(
        label="Solo validar, no guardar todavía", required=False, initial=True
    )

    def clean_archivo(self):
        archivo = self.cleaned_data["archivo"]
        if not archivo.name.lower().endswith((".csv", ".xlsx", ".xls")):
            raise ValidationError("El archivo debe ser .csv, .xlsx o .xls.")
        return archivo


class AsignarEquiposForm(forms.Form):
    contrato = forms.ModelChoiceField(
        queryset=Contrato.objects.filter(estado=Contrato.Estado.ACTIVO).order_by("numero_contrato"),
        label="Contrato destino",
    )
    fecha_inicio = forms.DateField(label="Fecha de inicio de la asignación")

    def __init__(self, *args, equipos=(), **kwargs):
        super().__init__(*args, **kwargs)
        # Un campo de lectura inicial por equipo: cada uno arranca su propia
        # asignación con su propio punto de referencia, no puede compartirse
        # un solo valor entre varios equipos.
        for equipo in equipos:
            self.fields[f"lectura_bn_{equipo.pk}"] = forms.IntegerField(
                label="Lectura inicial BN", min_value=0, initial=0
            )
            self.fields[f"lectura_color_{equipo.pk}"] = forms.IntegerField(
                label="Lectura inicial Color", min_value=0, initial=0
            )


@admin.action(description="Asignar equipos seleccionados a un contrato")
def asignar_a_contrato_action(modeladmin, request, queryset):
    ids = ",".join(str(pk) for pk in queryset.values_list("pk", flat=True))
    return redirect(f"{reverse('admin:core_equipo_asignar_masivo')}?ids={ids}")


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
        "en_linea_api",
        "ultima_actualizacion_api",
        "contrato_activo",
        "cliente_activo",
    )
    list_filter = (
        "metodo_lectura",
        "estado_actual",
        "marca",
        "en_linea_api",
        ("asignaciones__contrato", admin.RelatedOnlyFieldListFilter),
        ("asignaciones__contrato__cliente", admin.RelatedOnlyFieldListFilter),
    )
    search_fields = (
        "numero_serie",
        "marca",
        "modelo",
        "id_externo_3manager",
        "id_externo_printaudit",
        "asignaciones__contrato__numero_contrato",
        "asignaciones__contrato__cliente__nombre",
    )
    actions = [asignar_a_contrato_action]

    def get_queryset(self, request):
        # Los filtros/búsqueda por asignaciones__* hacen un JOIN; sin distinct()
        # un equipo con más de una asignación histórica aparecería repetido.
        # prefetch_related evita una consulta por fila en contrato_activo/cliente_activo.
        return (
            super()
            .get_queryset(request)
            .distinct()
            .prefetch_related("asignaciones__contrato__cliente")
        )

    @admin.display(description="Contrato actual")
    def contrato_activo(self, obj):
        asignacion = next((a for a in obj.asignaciones.all() if a.fecha_fin is None), None)
        return asignacion.contrato.numero_contrato if asignacion else "—"

    @admin.display(description="Cliente actual")
    def cliente_activo(self, obj):
        asignacion = next((a for a in obj.asignaciones.all() if a.fecha_fin is None), None)
        return asignacion.contrato.cliente.nombre if asignacion else "—"

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
            path(
                "asignar-masivo/",
                self.admin_site.admin_view(self.asignar_masivo_view),
                name="core_equipo_asignar_masivo",
            ),
        ]
        return urls + super().get_urls()

    def asignar_masivo_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied

        ids_texto = request.POST.get("ids") if request.method == "POST" else request.GET.get("ids", "")
        ids = [int(pk) for pk in (ids_texto or "").split(",") if pk]
        equipos = list(Equipo.objects.filter(pk__in=ids).order_by("numero_serie"))
        if not equipos:
            messages.warning(request, "No se seleccionó ningún equipo.")
            return redirect("admin:core_equipo_changelist")

        ya_asignados = set(
            Asignacion.objects.filter(equipo_id__in=ids, fecha_fin__isnull=True).values_list(
                "equipo_id", flat=True
            )
        )
        equipos_disponibles = [e for e in equipos if e.pk not in ya_asignados]
        if len(equipos_disponibles) < len(equipos):
            messages.warning(
                request,
                f"{len(equipos) - len(equipos_disponibles)} equipo(s) ya tenían una asignación activa "
                "y se omitieron de esta pantalla.",
            )
        if not equipos_disponibles:
            messages.error(request, "Ninguno de los equipos seleccionados está disponible para asignar.")
            return redirect("admin:core_equipo_changelist")

        resultados = None
        if request.method == "POST":
            form = AsignarEquiposForm(request.POST, equipos=equipos_disponibles)
            if form.is_valid():
                contrato = form.cleaned_data["contrato"]
                fecha_inicio = form.cleaned_data["fecha_inicio"]
                resultados = []
                for equipo in equipos_disponibles:
                    fila = {
                        "fecha_inicio_asignacion": fecha_inicio.isoformat(),
                        "lectura_inicial_bn": form.cleaned_data[f"lectura_bn_{equipo.pk}"],
                        "lectura_inicial_color": form.cleaned_data[f"lectura_color_{equipo.pk}"],
                    }
                    try:
                        with transaction.atomic():
                            detalle = asignar_equipo_a_contrato(equipo, contrato.numero_contrato, fila)
                        resultados.append({"equipo": equipo.numero_serie, "detalle": detalle, "error": ""})
                    except Exception as exc:  # noqa: BLE001 - se aísla el error por equipo a propósito
                        resultados.append({"equipo": equipo.numero_serie, "detalle": "", "error": str(exc)})

                con_error = [r for r in resultados if r["error"]]
                creados = len(resultados) - len(con_error)
                if con_error:
                    messages.warning(
                        request, f"{creados} asignación(es) creada(s), {len(con_error)} con error."
                    )
                elif creados:
                    messages.success(
                        request, f"{creados} equipo(s) asignado(s) al contrato {contrato.numero_contrato}."
                    )
        else:
            form = AsignarEquiposForm(equipos=equipos_disponibles, initial={"fecha_inicio": timezone.localdate()})

        filas_formulario = [
            (equipo, form[f"lectura_bn_{equipo.pk}"], form[f"lectura_color_{equipo.pk}"])
            for equipo in equipos_disponibles
        ]
        contexto = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Asignar equipos a un contrato",
            "form": form,
            "filas_formulario": filas_formulario,
            "ids": ",".join(str(e.pk) for e in equipos_disponibles),
            "resultados": resultados,
        }
        return render(request, "admin/core/equipo/asignar_masivo.html", contexto)

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
                    resumen = resumen_de_equipos(resultados)
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


def _avisar_lecturas_con_anomalia(request, lecturas):
    """Muestra un `messages.warning` por cada Lectura cuyo `save()` la marcó
    con `estado_auditoria=ALERTA` (consumo fuera de lo normal para el
    equipo, ver `core.calculo_consumo.detectar_anomalias`). No bloquea nada:
    la lectura ya se guardó, esto solo avisa para revisión."""
    for lectura in lecturas:
        if lectura.estado_auditoria != Lectura.EstadoAuditoria.ALERTA:
            continue
        mensajes = [a["mensaje"] for a in lectura.detalle_auditoria.get("advertencias", [])]
        detalle = " ".join(mensajes) or "Consumo fuera de lo normal para este equipo."
        messages.warning(request, f"{lectura.asignacion.equipo.numero_serie} ({lectura.fecha}): {detalle}")


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

    def save_formset(self, request, form, formset, change):
        if formset.model is not Lectura:
            return super().save_formset(request, form, formset, change)
        _avisar_lecturas_con_anomalia(request, formset.save())


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
    change_list_template = "admin/core/lectura/change_list.html"
    list_display = ("asignacion", "fecha", "lectura_bn", "lectura_color", "origen", "estado_auditoria")
    list_filter = ("origen", "estado_auditoria")
    search_fields = ("asignacion__equipo__numero_serie", "asignacion__contrato__numero_contrato")
    autocomplete_fields = ("asignacion",)
    date_hierarchy = "fecha"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _avisar_lecturas_con_anomalia(request, [obj])

    def get_urls(self):
        urls = [
            path(
                "importar/",
                self.admin_site.admin_view(self.importar_lecturas_view),
                name="core_lectura_importar",
            ),
            path(
                "importar/plantilla/",
                self.admin_site.admin_view(self.descargar_plantilla_view),
                name="core_lectura_importar_plantilla",
            ),
        ]
        return urls + super().get_urls()

    def importar_lecturas_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        resultados = None
        resumen = None

        if request.method == "POST":
            form = ImportarLecturasForm(request.POST, request.FILES)
            if form.is_valid():
                archivo = form.cleaned_data["archivo"]
                dry_run = form.cleaned_data["dry_run"]
                resultados = procesar_archivo_lecturas(archivo, archivo.name, dry_run=dry_run)
                if not resultados:
                    messages.warning(request, "El archivo no tiene filas de datos.")
                else:
                    resumen = resumen_de_lecturas(resultados)
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
            form = ImportarLecturasForm()

        contexto = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Importar lecturas",
            "form": form,
            "resultados": resultados,
            "resumen": resumen,
        }
        return render(request, "admin/core/lectura/importar_lecturas.html", contexto)

    def descargar_plantilla_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        ruta = Path(settings.BASE_DIR) / "plantillas" / "plantilla_importar_lecturas.csv"
        return FileResponse(open(ruta, "rb"), as_attachment=True, filename=ruta.name)


@admin.register(Factura)
class FacturaAdmin(admin.ModelAdmin):
    list_display = (
        "contrato",
        "emisor",
        "periodo_mes",
        "periodo_anio",
        "fecha_inicio",
        "fecha_fin",
        "monto_total",
        "moneda",
        "estado",
        "fecha_generacion",
        "enlace_pdf",
        "enlace_excel",
    )
    list_filter = ("estado", "moneda", "emisor", "periodo_anio", "periodo_mes")
    search_fields = ("contrato__numero_contrato",)
    autocomplete_fields = ("contrato",)

    def has_add_permission(self, request):
        # Las facturas las genera el orquestador de facturación (módulo 8),
        # no se capturan manualmente desde el Admin.
        return False

    @admin.display(description="PDF")
    def enlace_pdf(self, obj):
        if obj.pdf_archivo:
            return format_html('<a href="{}" target="_blank">Descargar PDF</a>', obj.pdf_archivo.url)
        return "—"

    @admin.display(description="Excel")
    def enlace_excel(self, obj):
        if obj.excel_archivo:
            return format_html('<a href="{}" target="_blank">Descargar Excel</a>', obj.excel_archivo.url)
        return "—"


@admin.register(LogEjecucion)
class LogEjecucionAdmin(admin.ModelAdmin):
    """Panel de resumen del histórico de ejecuciones (módulo 9).

    Filtrable por fecha (date_hierarchy + filtro de timestamp) y por estado
    (list_filter), con enlaces directos de descarga al PDF/Excel generados.
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

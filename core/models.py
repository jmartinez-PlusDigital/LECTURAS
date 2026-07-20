from decimal import Decimal

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q, UniqueConstraint, CheckConstraint


class TimestampedModel(models.Model):
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class MetodoLectura(models.TextChoices):
    API_3MANAGER = "api_3manager", "API 3-Manager"
    API_PRINTAUDIT = "api_printaudit", "API PrintAudit"
    MANUAL = "manual", "Manual"


class Moneda(models.TextChoices):
    MXN = "MXN", "Peso mexicano (MXN)"
    USD = "USD", "Dólar estadounidense (USD)"


class Cliente(TimestampedModel):
    nombre = models.CharField(max_length=255)
    razon_social = models.CharField(max_length=255, blank=True)
    rfc = models.CharField(max_length=13, blank=True)
    contacto_nombre = models.CharField(max_length=255, blank=True)
    contacto_email = models.EmailField(blank=True)
    contacto_telefono = models.CharField(max_length=20, blank=True)
    direccion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)
    id_cuenta_3manager = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="ID de cuenta en 3-Manager",
        help_text=(
            "accountId (GUID) de este cliente en 3-Manager, visible en la URL al ver la cuenta "
            "en la plataforma. Requerido solo si algún equipo de este cliente usa metodo_lectura=api_3manager."
        ),
    )

    class Meta:
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class Contrato(TimestampedModel):
    class Estado(models.TextChoices):
        ACTIVO = "activo", "Activo"
        SUSPENDIDO = "suspendido", "Suspendido"
        TERMINADO = "terminado", "Terminado"

    numero_contrato = models.CharField(max_length=50, unique=True)
    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name="contratos")
    moneda = models.CharField(max_length=3, choices=Moneda.choices, default=Moneda.MXN)
    renta_base = models.DecimalField(max_digits=10, decimal_places=2)
    copias_incluidas_bn = models.PositiveIntegerField(default=0)
    copias_incluidas_color = models.PositiveIntegerField(default=0)
    costo_excedente_bn = models.DecimalField(max_digits=10, decimal_places=4)
    costo_excedente_color = models.DecimalField(max_digits=10, decimal_places=4)
    iva_porcentaje = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("16.00"))
    dia_corte_facturacion = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(31)]
    )
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.ACTIVO)
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["numero_contrato"]
        constraints = [
            CheckConstraint(
                condition=Q(dia_corte_facturacion__gte=1) & Q(dia_corte_facturacion__lte=31),
                name="contrato_dia_corte_valido",
                violation_error_message="El día de corte debe estar entre 1 y 31.",
            ),
        ]
        indexes = [models.Index(fields=["estado", "dia_corte_facturacion"])]

    def __str__(self):
        return self.numero_contrato


class Equipo(TimestampedModel):
    class EstadoActual(models.TextChoices):
        ACTIVO = "activo", "Activo"
        EN_TALLER = "en_taller", "En taller"
        SIN_ASIGNAR = "sin_asignar", "Sin asignar"
        BAJA = "baja", "Baja"

    numero_serie = models.CharField(max_length=100, unique=True)
    marca = models.CharField(max_length=100)
    modelo = models.CharField(max_length=100)
    metodo_lectura = models.CharField(max_length=20, choices=MetodoLectura.choices)
    id_externo_3manager = models.CharField(max_length=100, null=True, blank=True)
    id_externo_printaudit = models.CharField(max_length=100, null=True, blank=True)
    permite_reset_contador = models.BooleanField(default=False)
    tope_contador = models.PositiveIntegerField(null=True, blank=True)
    estado_actual = models.CharField(
        max_length=20, choices=EstadoActual.choices, default=EstadoActual.SIN_ASIGNAR
    )

    class Meta:
        ordering = ["numero_serie"]
        indexes = [models.Index(fields=["metodo_lectura", "estado_actual"])]

    def __str__(self):
        return f"{self.numero_serie} ({self.marca} {self.modelo})"

    def save(self, *args, **kwargs):
        # "Ricoh"/"RICOH"/"ricoh" no deben contar como marcas distintas en los
        # filtros del Admin: se reutiliza la capitalización ya existente en la
        # base (no se adivina con .title(), que rompería siglas como "HP").
        if self.marca:
            self.marca = self.marca.strip()
            existente = (
                Equipo.objects.filter(marca__iexact=self.marca).exclude(pk=self.pk).values_list("marca", flat=True).first()
            )
            if existente:
                self.marca = existente
        super().save(*args, **kwargs)


class Asignacion(TimestampedModel):
    equipo = models.ForeignKey(Equipo, on_delete=models.PROTECT, related_name="asignaciones")
    contrato = models.ForeignKey(Contrato, on_delete=models.PROTECT, related_name="asignaciones")
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField(null=True, blank=True)
    lectura_inicial_referencia_bn = models.PositiveIntegerField()
    lectura_inicial_referencia_color = models.PositiveIntegerField()
    lectura_cierre_bn = models.PositiveIntegerField(null=True, blank=True)
    lectura_cierre_color = models.PositiveIntegerField(null=True, blank=True)
    ip_red_cliente = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-fecha_inicio"]
        constraints = [
            UniqueConstraint(
                fields=["equipo"],
                condition=Q(fecha_fin__isnull=True),
                name="unique_asignacion_activa_por_equipo",
                violation_error_message=(
                    "Este equipo ya tiene una asignación activa (sin fecha de fin). "
                    "Cierra esa asignación antes de crear una nueva."
                ),
            ),
        ]
        indexes = [models.Index(fields=["contrato", "fecha_fin"])]

    def __str__(self):
        return f"{self.equipo} → {self.contrato}"


class Lectura(TimestampedModel):
    class EstadoAuditoria(models.TextChoices):
        OK = "ok", "Ok"
        ALERTA = "alerta", "Alerta"
        ERROR = "error", "Error"

    asignacion = models.ForeignKey(Asignacion, on_delete=models.PROTECT, related_name="lecturas")
    fecha = models.DateField()
    lectura_bn = models.PositiveIntegerField()
    lectura_color = models.PositiveIntegerField()
    origen = models.CharField(max_length=20, choices=MetodoLectura.choices)
    estado_auditoria = models.CharField(
        max_length=10, choices=EstadoAuditoria.choices, default=EstadoAuditoria.OK
    )
    detalle_auditoria = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-fecha"]
        constraints = [
            UniqueConstraint(
                fields=["asignacion", "fecha"],
                name="unique_lectura_por_dia",
                violation_error_message="Ya existe una lectura registrada para esta asignación en esa fecha.",
            ),
        ]
        indexes = [models.Index(fields=["fecha", "estado_auditoria"])]

    def __str__(self):
        return f"{self.asignacion.equipo} @ {self.fecha}"


def _ruta_documento_factura(instance, filename):
    return f"facturas/{instance.contrato.numero_contrato}/{instance.periodo_anio}-{instance.periodo_mes:02d}/{filename}"


class Factura(TimestampedModel):
    class Estado(models.TextChoices):
        OK = "ok", "Ok"
        PENDIENTE = "pendiente", "Pendiente"
        ERROR_ARCHIVO = "error_archivo", "Error de archivo"

    contrato = models.ForeignKey(Contrato, on_delete=models.PROTECT, related_name="facturas")
    periodo_mes = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    periodo_anio = models.PositiveIntegerField()
    fecha_inicio = models.DateField(
        null=True,
        blank=True,
        help_text="Inicio real del rango de lecturas facturado. Ancla el siguiente periodo (ver core.procesamiento_facturacion).",
    )
    fecha_fin = models.DateField(null=True, blank=True, help_text="Fin real del rango de lecturas facturado.")
    moneda = models.CharField(
        max_length=3,
        choices=Moneda.choices,
        default=Moneda.MXN,
        help_text="Copiada del contrato al momento de calcular la factura, para que quede fija aunque el contrato cambie de moneda después.",
    )
    consumo_excedente_bn = models.PositiveIntegerField(default=0)
    consumo_excedente_color = models.PositiveIntegerField(default=0)
    monto_renta = models.DecimalField(max_digits=10, decimal_places=2)
    monto_excedente = models.DecimalField(max_digits=10, decimal_places=2)
    monto_iva = models.DecimalField(max_digits=10, decimal_places=2)
    monto_total = models.DecimalField(max_digits=10, decimal_places=2)
    pdf_archivo = models.FileField(upload_to=_ruta_documento_factura, blank=True)
    excel_archivo = models.FileField(upload_to=_ruta_documento_factura, blank=True)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    fecha_generacion = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-periodo_anio", "-periodo_mes"]
        constraints = [
            UniqueConstraint(
                fields=["contrato", "periodo_mes", "periodo_anio"],
                name="unique_factura_por_periodo",
                violation_error_message="Ya existe una factura para este contrato en ese periodo.",
            ),
        ]

    def __str__(self):
        return f"{self.contrato} {self.periodo_mes}/{self.periodo_anio}"


class LogEjecucion(models.Model):
    class Estado(models.TextChoices):
        INICIADO = "iniciado", "Iniciado"
        OK = "ok", "Ok"
        PENDIENTE = "pendiente", "Pendiente (auditoría)"
        ERROR_ARCHIVO = "error_archivo", "Error de archivo"
        ERROR = "error", "Error"

    contrato = models.ForeignKey(
        Contrato, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs"
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    estado = models.CharField(max_length=20, choices=Estado.choices)
    detalle = models.JSONField(default=dict, blank=True)
    enlaces_generados = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["timestamp", "estado"])]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} — {self.estado}"

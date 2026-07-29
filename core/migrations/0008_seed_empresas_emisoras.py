from django.db import migrations

NOMBRE_EMISOR_DEFAULT = "Plus Digital"
EMISORAS = ["Plus Digital", "Mexicali Plus Digital"]


def crear_emisoras_y_backfill(apps, schema_editor):
    EmpresaEmisora = apps.get_model("core", "EmpresaEmisora")
    Contrato = apps.get_model("core", "Contrato")

    for nombre in EMISORAS:
        EmpresaEmisora.objects.get_or_create(nombre=nombre)

    default = EmpresaEmisora.objects.get(nombre=NOMBRE_EMISOR_DEFAULT)
    Contrato.objects.filter(emisor__isnull=True).update(emisor=default)


def revertir(apps, schema_editor):
    # No se borran las EmpresaEmisora ni se limpia el backfill: podrían ya
    # estar en uso (logo subido, facturas generadas). Migración de datos de
    # solo avance.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_empresaemisora_contrato_emisor_factura_emisor'),
    ]

    operations = [
        migrations.RunPython(crear_emisoras_y_backfill, revertir),
    ]

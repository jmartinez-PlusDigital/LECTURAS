import shutil
import tempfile
from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings

from core.models import Asignacion, Cliente, Contrato, Equipo, Factura, Lectura
from facturacion import calcular_factura, persistir_factura


class FacturacionTestCase(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cliente Facturación Test")
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-FAC-1",
            cliente=self.cliente,
            renta_base=Decimal("1000.00"),
            copias_incluidas_bn=1000,
            copias_incluidas_color=200,
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            iva_porcentaje=Decimal("16.00"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )

    def _equipo(self, serie, **kwargs):
        defaults = dict(marca="Canon", modelo="X1", metodo_lectura="manual")
        defaults.update(kwargs)
        return Equipo.objects.create(numero_serie=serie, **defaults)

    def _asignacion(self, equipo, **kwargs):
        defaults = dict(
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )
        defaults.update(kwargs)
        return Asignacion.objects.create(equipo=equipo, **defaults)

    def test_consumo_dentro_de_lo_incluido_no_genera_excedente(self):
        equipo = self._equipo("SN-FAC-1")
        asignacion = self._asignacion(equipo)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 31), lectura_bn=500, lectura_color=100, origen="manual"
        )

        resultado = calcular_factura(self.contrato, date(2026, 1, 1), date(2026, 1, 31), 1, 2026)

        self.assertEqual(resultado.consumo_excedente_bn, 0)
        self.assertEqual(resultado.consumo_excedente_color, 0)
        self.assertEqual(resultado.monto_excedente, Decimal("0.00"))
        self.assertEqual(resultado.monto_renta, Decimal("1000.00"))
        self.assertEqual(resultado.monto_iva, Decimal("160.00"))
        self.assertEqual(resultado.monto_total, Decimal("1160.00"))

    def test_excedente_y_iva_se_calculan_correctamente(self):
        equipo = self._equipo("SN-FAC-2")
        asignacion = self._asignacion(equipo)
        # consumo bn=1500 (500 de excedente), color=300 (100 de excedente)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 31), lectura_bn=1500, lectura_color=300, origen="manual"
        )

        resultado = calcular_factura(self.contrato, date(2026, 1, 1), date(2026, 1, 31), 1, 2026)

        self.assertEqual(resultado.consumo_excedente_bn, 500)
        self.assertEqual(resultado.consumo_excedente_color, 100)
        excedente_esperado = Decimal("500") * Decimal("0.50") + Decimal("100") * Decimal("1.50")  # 250 + 150 = 400
        self.assertEqual(resultado.monto_excedente, Decimal("400.00"))
        subtotal = Decimal("1000.00") + Decimal("400.00")
        iva_esperado = (subtotal * Decimal("16.00") / Decimal("100")).quantize(Decimal("0.01"))
        self.assertEqual(resultado.monto_iva, iva_esperado)
        self.assertEqual(resultado.monto_total, subtotal + iva_esperado)

    def test_primer_mes_de_asignacion_usa_lectura_referencia(self):
        equipo = self._equipo("SN-FAC-3")
        asignacion = self._asignacion(
            equipo,
            fecha_inicio=date(2026, 3, 1),
            lectura_inicial_referencia_bn=8000,
            lectura_inicial_referencia_color=500,
        )
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 3, 20), lectura_bn=8300, lectura_color=550, origen="manual"
        )

        resultado = calcular_factura(self.contrato, date(2026, 3, 1), date(2026, 3, 31), 3, 2026)

        equipo_resultado = resultado.consumo_por_equipo[0]
        self.assertEqual(equipo_resultado.consumo_bn, 300)
        self.assertEqual(equipo_resultado.consumo_color, 50)

    def test_consolida_consumo_de_varios_equipos_del_mismo_contrato(self):
        equipo1 = self._equipo("SN-FAC-4A")
        equipo2 = self._equipo("SN-FAC-4B")
        asignacion1 = self._asignacion(equipo1)
        asignacion2 = self._asignacion(equipo2)
        Lectura.objects.create(
            asignacion=asignacion1, fecha=date(2026, 1, 31), lectura_bn=700, lectura_color=0, origen="manual"
        )
        Lectura.objects.create(
            asignacion=asignacion2, fecha=date(2026, 1, 31), lectura_bn=800, lectura_color=0, origen="manual"
        )

        resultado = calcular_factura(self.contrato, date(2026, 1, 1), date(2026, 1, 31), 1, 2026)

        # consumo total bn = 1500, incluido=1000 -> excedente 500
        self.assertEqual(resultado.consumo_excedente_bn, 500)
        self.assertEqual(len(resultado.consumo_por_equipo), 2)

    def test_modo_simulacion_no_persiste_nada(self):
        equipo = self._equipo("SN-FAC-5")
        asignacion = self._asignacion(equipo)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 31), lectura_bn=500, lectura_color=50, origen="manual"
        )

        resultado = calcular_factura(
            self.contrato, date(2026, 1, 1), date(2026, 1, 31), 1, 2026, simulacion=True
        )

        self.assertIsNone(resultado.factura)
        self.assertFalse(Factura.objects.filter(contrato=self.contrato).exists())

    def test_persistir_factura_crea_registro_con_archivos(self):
        media_root_temporal = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root_temporal, ignore_errors=True)

        equipo = self._equipo("SN-FAC-6")
        asignacion = self._asignacion(equipo)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 31), lectura_bn=500, lectura_color=50, origen="manual"
        )

        resultado = calcular_factura(self.contrato, date(2026, 1, 1), date(2026, 1, 31), 1, 2026)
        with override_settings(MEDIA_ROOT=media_root_temporal):
            factura = persistir_factura(
                resultado, pdf_bytes=b"contenido-pdf-falso", excel_bytes=b"contenido-excel-falso"
            )

            self.assertEqual(Factura.objects.filter(contrato=self.contrato).count(), 1)
            self.assertEqual(factura.estado, Factura.Estado.OK)
            self.assertTrue(factura.pdf_archivo.name.endswith(".pdf"))
            self.assertEqual(factura.pdf_archivo.read(), b"contenido-pdf-falso")
            self.assertEqual(factura.monto_total, resultado.monto_total)

    def test_moneda_por_defecto_es_mxn(self):
        equipo = self._equipo("SN-FAC-7")
        asignacion = self._asignacion(equipo)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 31), lectura_bn=500, lectura_color=50, origen="manual"
        )

        resultado = calcular_factura(self.contrato, date(2026, 1, 1), date(2026, 1, 31), 1, 2026)

        self.assertEqual(resultado.moneda, "MXN")

    def test_moneda_del_contrato_se_propaga_a_la_factura_persistida(self):
        contrato_usd = Contrato.objects.create(
            numero_contrato="CT-FAC-USD",
            cliente=self.cliente,
            moneda="USD",
            renta_base=Decimal("500.00"),
            copias_incluidas_bn=1000,
            copias_incluidas_color=200,
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        equipo = self._equipo("SN-FAC-8")
        asignacion = self._asignacion(equipo, contrato=contrato_usd)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 31), lectura_bn=500, lectura_color=50, origen="manual"
        )

        resultado = calcular_factura(contrato_usd, date(2026, 1, 1), date(2026, 1, 31), 1, 2026)
        self.assertEqual(resultado.moneda, "USD")

        factura = persistir_factura(resultado)
        self.assertEqual(factura.moneda, "USD")

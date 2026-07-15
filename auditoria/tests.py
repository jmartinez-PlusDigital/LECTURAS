from datetime import date

from django.test import TestCase

from auditoria import auditar_contrato
from core.calculo_consumo import calcular_consumo
from core.models import Asignacion, Cliente, Contrato, Equipo, Lectura


class AuditoriaTestCase(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cliente Auditoría Test")
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-AUD-1",
            cliente=self.cliente,
            renta_base=1000,
            costo_excedente_bn="0.50",
            costo_excedente_color="1.50",
            dia_corte_facturacion=1,
            fecha_inicio=date(2025, 1, 1),
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

    # --- (b) Rollover de contador ---------------------------------------

    def test_rollover_de_contador_calcula_consumo_correcto(self):
        equipo = self._equipo("SN-ROLLOVER", tope_contador=10000, permite_reset_contador=True)
        asignacion = self._asignacion(equipo)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 10), lectura_bn=9800, lectura_color=0, origen="manual"
        )
        lectura_rollover = Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 20), lectura_bn=150, lectura_color=0, origen="manual"
        )

        resultado_consumo = calcular_consumo(asignacion, lectura_rollover)

        self.assertEqual(resultado_consumo.consumo_bn, (10000 - 9800) + 150)
        self.assertFalse(resultado_consumo.lectura_invertida_bn)

        resultado = auditar_contrato(self.contrato, date(2026, 1, 1), date(2026, 1, 31))
        self.assertFalse(any(a.candado == "a" for a in resultado.alertas))
        self.assertTrue(resultado.aprobado)

    def test_lectura_invertida_sin_rollover_bloquea(self):
        equipo = self._equipo("SN-INVERTIDA", tope_contador=None, permite_reset_contador=False)
        asignacion = self._asignacion(equipo)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 10), lectura_bn=5000, lectura_color=0, origen="manual"
        )
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 20), lectura_bn=200, lectura_color=0, origen="manual"
        )

        resultado = auditar_contrato(self.contrato, date(2026, 1, 1), date(2026, 1, 31))

        alertas_a = [a for a in resultado.alertas if a.candado == "a" and a.equipo_numero_serie == "SN-INVERTIDA"]
        self.assertEqual(len(alertas_a), 1)
        self.assertTrue(alertas_a[0].bloqueante)
        self.assertFalse(resultado.aprobado)

    # --- Primer mes de asignación nueva -----------------------------------

    def test_primer_mes_de_asignacion_nueva_usa_lectura_referencia(self):
        equipo = self._equipo("SN-NUEVA")
        asignacion = self._asignacion(
            equipo,
            fecha_inicio=date(2026, 2, 1),
            lectura_inicial_referencia_bn=5000,
            lectura_inicial_referencia_color=100,
        )
        lectura = Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 2, 15), lectura_bn=5300, lectura_color=150, origen="manual"
        )

        resultado_consumo = calcular_consumo(asignacion, lectura)
        self.assertTrue(resultado_consumo.es_primera_lectura_asignacion)
        self.assertEqual(resultado_consumo.consumo_bn, 300)
        self.assertEqual(resultado_consumo.consumo_color, 50)

        resultado = auditar_contrato(self.contrato, date(2026, 2, 1), date(2026, 2, 28))
        alertas_equipo = [a for a in resultado.alertas if a.equipo_numero_serie == "SN-NUEVA"]
        self.assertEqual(alertas_equipo, [])

    # --- (e) Equipo huérfano ----------------------------------------------

    def test_equipo_huerfano_lectura_fuera_de_vigencia(self):
        equipo = self._equipo("SN-HUERFANO")
        asignacion = self._asignacion(equipo, fecha_inicio=date(2026, 1, 1), fecha_fin=date(2026, 1, 15))
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 20), lectura_bn=100, lectura_color=0, origen="manual"
        )

        resultado = auditar_contrato(self.contrato, date(2026, 1, 1), date(2026, 1, 31))

        alertas_e = [a for a in resultado.alertas if a.candado == "e" and a.equipo_numero_serie == "SN-HUERFANO"]
        self.assertEqual(len(alertas_e), 1)
        self.assertTrue(alertas_e[0].bloqueante)
        self.assertFalse(resultado.aprobado)

    # --- (c) Equipo omitido -------------------------------------------------

    def test_equipo_omitido_sin_lecturas_en_el_periodo(self):
        equipo = self._equipo("SN-OMITIDO")
        self._asignacion(equipo, fecha_inicio=date(2026, 1, 1))

        resultado = auditar_contrato(self.contrato, date(2026, 1, 1), date(2026, 1, 31))

        alertas_c = [a for a in resultado.alertas if a.candado == "c" and a.equipo_numero_serie == "SN-OMITIDO"]
        self.assertEqual(len(alertas_c), 1)
        self.assertFalse(resultado.aprobado)

    # --- (d) Equipo en cero ---------------------------------------------

    def test_equipo_en_cero_requiere_confirmacion(self):
        equipo = self._equipo("SN-CERO")
        asignacion = self._asignacion(equipo)
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 15), lectura_bn=0, lectura_color=0, origen="manual"
        )

        resultado = auditar_contrato(self.contrato, date(2026, 1, 1), date(2026, 1, 31))

        alertas_d = [a for a in resultado.alertas if a.candado == "d" and a.equipo_numero_serie == "SN-CERO"]
        self.assertEqual(len(alertas_d), 1)
        self.assertFalse(resultado.aprobado)

    # --- (g) Falla de sincronización ----------------------------------------

    def test_falla_sincronizacion_equipo_api_sin_lectura_reciente(self):
        equipo = self._equipo("SN-APISTALE", metodo_lectura="api_3manager", id_externo_3manager="DEV-1")
        asignacion = self._asignacion(equipo)
        # última lectura muy anterior al fin del periodo -> falla de sincronización
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 1, 2), lectura_bn=100, lectura_color=0, origen="api_3manager"
        )

        resultado = auditar_contrato(
            self.contrato, date(2026, 1, 1), date(2026, 1, 31), dias_falla_sincronizacion=5
        )

        alertas_g = [a for a in resultado.alertas if a.candado == "g" and a.equipo_numero_serie == "SN-APISTALE"]
        self.assertEqual(len(alertas_g), 1)
        self.assertFalse(resultado.aprobado)

    # --- (f) Anomalía estadística (no bloqueante) ---------------------------

    def test_anomalia_estadistica_no_bloqueante(self):
        equipo = self._equipo("SN-ANOMALIA")
        asignacion = self._asignacion(equipo, fecha_inicio=date(2025, 6, 1))

        # Historial: consumo estable de 1000/mes entre febrero y junio 2026.
        contador = 1000
        for mes in range(1, 7):
            Lectura.objects.create(
                asignacion=asignacion,
                fecha=date(2026, mes, 28),
                lectura_bn=contador,
                lectura_color=0,
                origen="manual",
            )
            contador += 1000

        # Julio: salto a 10,000 de consumo (10x el promedio histórico de 1000).
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 7, 31), lectura_bn=contador + 10000, lectura_color=0, origen="manual"
        )

        resultado = auditar_contrato(self.contrato, date(2026, 7, 1), date(2026, 7, 31))

        alertas_equipo = [a for a in resultado.alertas if a.equipo_numero_serie == "SN-ANOMALIA"]
        self.assertEqual(len(alertas_equipo), 1)
        self.assertEqual(alertas_equipo[0].candado, "f")
        self.assertFalse(alertas_equipo[0].bloqueante)
        # Una alerta no bloqueante no debe tumbar la aprobación del contrato.
        self.assertTrue(resultado.aprobado)

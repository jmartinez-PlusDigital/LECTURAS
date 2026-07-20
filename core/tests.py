import io
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from core.dashboard import _facturado_mes_por_moneda, _ultimos_n_meses, excedente_mensual, facturacion_mensual
from core.historial_lecturas import historial_de_contrato
from core.importacion_lecturas import generar_plantilla_lecturas, procesar_archivo_lecturas
from core.models import Asignacion, Cliente, Contrato, Equipo, Factura, Lectura
from core.procesamiento_facturacion import _calcular_periodo


class CalcularPeriodoTestCase(TestCase):
    def setUp(self):
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-PERIODO-1",
            cliente=Cliente.objects.create(nombre="Cliente Periodo Test"),
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=27,
            fecha_inicio=date(2020, 11, 30),
        )

    def test_primera_factura_usa_fecha_inicio_del_contrato(self):
        fecha_inicio, fecha_fin = _calcular_periodo(self.contrato, date(2026, 7, 15))

        self.assertEqual(fecha_inicio, date(2020, 11, 30))
        self.assertEqual(fecha_fin, date(2026, 7, 15))

    def test_periodo_siguiente_arranca_donde_termino_la_ultima_factura_sin_dejar_hueco(self):
        Factura.objects.create(
            contrato=self.contrato,
            periodo_mes=7,
            periodo_anio=2026,
            fecha_inicio=date(2020, 11, 30),
            fecha_fin=date(2026, 7, 15),
            monto_renta=Decimal("0"),
            monto_excedente=Decimal("0"),
            monto_iva=Decimal("0"),
            monto_total=Decimal("0"),
        )

        # Se fuerza fuera del día de corte configurado (27): el periodo debe
        # arrancar de todas formas el 16/jul (día después del fin real de la
        # última factura), no recalcularse a partir del día de corte.
        fecha_inicio, fecha_fin = _calcular_periodo(self.contrato, date(2026, 8, 20))

        self.assertEqual(fecha_inicio, date(2026, 7, 16))
        self.assertEqual(fecha_fin, date(2026, 8, 20))


def _archivo(contenido: str):
    return io.BytesIO(contenido.encode("utf-8"))


class ImportacionLecturasTestCase(TestCase):
    def setUp(self):
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-LEC-1",
            cliente=Cliente.objects.create(nombre="Cliente Lecturas Test"),
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        self.equipo1 = Equipo.objects.create(
            numero_serie="SN-LEC-1", marca="Canon", modelo="X1", metodo_lectura="manual"
        )
        self.equipo2 = Equipo.objects.create(
            numero_serie="SN-LEC-2", marca="Canon", modelo="X2", metodo_lectura="manual"
        )
        self.asignacion1 = Asignacion.objects.create(
            equipo=self.equipo1,
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )
        self.asignacion2 = Asignacion.objects.create(
            equipo=self.equipo2,
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )

    def test_crea_lectura_para_equipo_con_asignacion_activa(self):
        contenido = (
            "numero_serie,fecha,lectura_bn,lectura_color\n"
            "SN-LEC-1,2026-01-31,500,100\n"
        )
        resultados = procesar_archivo_lecturas(_archivo(contenido), "lecturas.csv")

        self.assertEqual(resultados[0]["error"], "")
        self.assertEqual(resultados[0]["lectura"], "creada")
        lectura = Lectura.objects.get(asignacion=self.asignacion1)
        self.assertEqual(lectura.lectura_bn, 500)
        self.assertEqual(lectura.lectura_color, 100)
        self.assertEqual(lectura.origen, "manual")

    def test_fecha_se_usa_hoy_si_se_omite(self):
        contenido = "numero_serie,fecha,lectura_bn,lectura_color\nSN-LEC-1,,500,100\n"
        procesar_archivo_lecturas(_archivo(contenido), "lecturas.csv")

        lectura = Lectura.objects.get(asignacion=self.asignacion1)
        self.assertEqual(lectura.fecha, timezone.localdate())

    def test_fila_con_equipo_inexistente_no_detiene_las_demas(self):
        contenido = (
            "numero_serie,fecha,lectura_bn,lectura_color\n"
            "SN-NO-EXISTE,2026-01-31,500,100\n"
            "SN-LEC-2,2026-01-31,900,300\n"
        )
        resultados = procesar_archivo_lecturas(_archivo(contenido), "lecturas.csv")

        self.assertIn("no existe", resultados[0]["error"])
        self.assertEqual(resultados[1]["error"], "")
        self.assertTrue(Lectura.objects.filter(asignacion=self.asignacion2, lectura_bn=900).exists())

    def test_reprocesar_actualiza_en_vez_de_duplicar(self):
        primero = "numero_serie,fecha,lectura_bn,lectura_color\nSN-LEC-1,2026-01-31,500,100\n"
        segundo = "numero_serie,fecha,lectura_bn,lectura_color\nSN-LEC-1,2026-01-31,550,120\n"
        procesar_archivo_lecturas(_archivo(primero), "lecturas.csv")
        resultados = procesar_archivo_lecturas(_archivo(segundo), "lecturas.csv")

        self.assertEqual(resultados[0]["lectura"], "actualizada")
        self.assertEqual(Lectura.objects.filter(asignacion=self.asignacion1).count(), 1)
        lectura = Lectura.objects.get(asignacion=self.asignacion1)
        self.assertEqual(lectura.lectura_bn, 550)
        self.assertEqual(lectura.lectura_color, 120)

    def test_numero_contrato_que_no_coincide_es_error(self):
        otro_contrato = Contrato.objects.create(
            numero_contrato="CT-LEC-OTRO",
            cliente=self.contrato.cliente,
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        contenido = (
            f"numero_serie,fecha,lectura_bn,lectura_color,numero_contrato\n"
            f"SN-LEC-1,2026-01-31,500,100,{otro_contrato.numero_contrato}\n"
        )
        resultados = procesar_archivo_lecturas(_archivo(contenido), "lecturas.csv")

        self.assertIn("está asignado al contrato", resultados[0]["error"])
        self.assertFalse(Lectura.objects.filter(asignacion=self.asignacion1).exists())

    def test_dry_run_no_persiste_nada(self):
        contenido = "numero_serie,fecha,lectura_bn,lectura_color\nSN-LEC-1,2026-01-31,500,100\n"
        resultados = procesar_archivo_lecturas(_archivo(contenido), "lecturas.csv", dry_run=True)

        self.assertEqual(resultados[0]["lectura"], "creada")
        self.assertFalse(Lectura.objects.filter(asignacion=self.asignacion1).exists())

    def test_generar_plantilla_incluye_equipos_activos_del_contrato(self):
        contenido = generar_plantilla_lecturas(self.contrato).decode("utf-8-sig")

        self.assertIn("SN-LEC-1", contenido)
        self.assertIn("SN-LEC-2", contenido)
        self.assertIn("CT-LEC-1", contenido)


class HistorialLecturasTestCase(TestCase):
    def setUp(self):
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-HIST-1",
            cliente=Cliente.objects.create(nombre="Cliente Historial Test"),
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        self.equipo = Equipo.objects.create(
            numero_serie="SN-HIST-1", marca="Canon", modelo="X1", metodo_lectura="manual"
        )
        self.asignacion = Asignacion.objects.create(
            equipo=self.equipo,
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=1000,
            lectura_inicial_referencia_color=100,
        )

    def _factura(self, mes, anio, fecha_inicio, fecha_fin):
        return Factura.objects.create(
            contrato=self.contrato,
            periodo_mes=mes,
            periodo_anio=anio,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            monto_renta=Decimal("0"),
            monto_excedente=Decimal("0"),
            monto_iva=Decimal("0"),
            monto_total=Decimal("0"),
        )

    def test_sin_facturas_no_muestra_ninguna_lectura(self):
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 1, 31), lectura_bn=1500, lectura_color=200, origen="manual"
        )

        self.assertEqual(historial_de_contrato(self.contrato), [])

    def test_lectura_fuera_de_toda_factura_se_excluye(self):
        self._factura(1, 2026, date(2026, 1, 1), date(2026, 1, 15))
        # capturada después del cierre de la única factura: aún no se ha facturado
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 1, 31), lectura_bn=1500, lectura_color=200, origen="manual"
        )

        self.assertEqual(historial_de_contrato(self.contrato), [])

    def test_primera_lectura_facturada_se_marca_contra_referencia_inicial(self):
        self._factura(1, 2026, date(2026, 1, 1), date(2026, 1, 31))
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 1, 31), lectura_bn=1500, lectura_color=200, origen="manual"
        )

        filas = historial_de_contrato(self.contrato)

        self.assertEqual(len(filas), 1)
        self.assertTrue(filas[0].es_primera_lectura)
        self.assertEqual(filas[0].consumo_bn, 500)
        self.assertEqual(filas[0].consumo_color, 100)
        self.assertEqual(filas[0].factura_periodo, "01/2026")

    def test_lecturas_de_dos_facturas_ordenadas_con_consumo_incremental(self):
        self._factura(1, 2026, date(2026, 1, 1), date(2026, 1, 31))
        self._factura(2, 2026, date(2026, 2, 1), date(2026, 2, 28))
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 1, 31), lectura_bn=1500, lectura_color=200, origen="manual"
        )
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 2, 28), lectura_bn=1800, lectura_color=250, origen="manual"
        )

        filas = historial_de_contrato(self.contrato)

        self.assertEqual(len(filas), 2)
        self.assertFalse(filas[1].es_primera_lectura)
        self.assertEqual(filas[1].consumo_bn, 300)
        self.assertEqual(filas[1].consumo_color, 50)
        self.assertEqual(filas[1].factura_periodo, "02/2026")

    def test_rango_de_fechas_acota_a_solo_ese_periodo(self):
        self._factura(1, 2026, date(2026, 1, 1), date(2026, 1, 31))
        self._factura(2, 2026, date(2026, 2, 1), date(2026, 2, 28))
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 1, 31), lectura_bn=1500, lectura_color=200, origen="manual"
        )
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 2, 28), lectura_bn=1800, lectura_color=250, origen="manual"
        )

        filas = historial_de_contrato(self.contrato, fecha_desde=date(2026, 2, 1), fecha_hasta=date(2026, 2, 28))

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].fecha, date(2026, 2, 28))

    def test_incluye_equipos_ya_no_asignados_actualmente_si_fueron_facturados(self):
        self._factura(1, 2026, date(2026, 1, 1), date(2026, 1, 31))
        self.asignacion.fecha_fin = date(2026, 2, 1)
        self.asignacion.lectura_cierre_bn = 1500
        self.asignacion.lectura_cierre_color = 200
        self.asignacion.save()
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 1, 31), lectura_bn=1500, lectura_color=200, origen="manual"
        )

        otro_equipo = Equipo.objects.create(
            numero_serie="SN-HIST-2", marca="Canon", modelo="X2", metodo_lectura="manual"
        )
        Asignacion.objects.create(
            equipo=otro_equipo,
            contrato=self.contrato,
            fecha_inicio=date(2026, 2, 2),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )

        filas = historial_de_contrato(self.contrato)

        numeros_serie = {f.equipo_numero_serie for f in filas}
        self.assertIn("SN-HIST-1", numeros_serie)


class FacturadoMesPorMonedaTestCase(TestCase):
    def _contrato(self, numero, moneda):
        return Contrato.objects.create(
            numero_contrato=numero,
            cliente=Cliente.objects.create(nombre=f"Cliente {numero}"),
            moneda=moneda,
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )

    def _factura(self, contrato, monto_total):
        return Factura.objects.create(
            contrato=contrato,
            periodo_mes=7,
            periodo_anio=2026,
            moneda=contrato.moneda,
            monto_renta=monto_total,
            monto_excedente=Decimal("0"),
            monto_iva=Decimal("0"),
            monto_total=monto_total,
            estado=Factura.Estado.OK,
        )

    def test_no_mezcla_montos_de_distintas_monedas(self):
        contrato_mxn = self._contrato("CT-MXN", "MXN")
        contrato_usd = self._contrato("CT-USD", "USD")
        self._factura(contrato_mxn, Decimal("1000.00"))
        self._factura(contrato_usd, Decimal("200.00"))

        filas = _facturado_mes_por_moneda(date(2026, 7, 15))

        por_moneda = {f["moneda"]: f["total"] for f in filas}
        self.assertEqual(por_moneda["MXN"], Decimal("1000.00"))
        self.assertEqual(por_moneda["USD"], Decimal("200.00"))


class TendenciasMensualesTestCase(TestCase):
    def _contrato(self, numero, moneda="MXN"):
        return Contrato.objects.create(
            numero_contrato=numero,
            cliente=Cliente.objects.create(nombre=f"Cliente {numero}"),
            moneda=moneda,
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2025, 1, 1),
        )

    def _factura(self, contrato, mes, anio, monto_total, excedente_bn=0, excedente_color=0):
        return Factura.objects.create(
            contrato=contrato,
            periodo_mes=mes,
            periodo_anio=anio,
            moneda=contrato.moneda,
            consumo_excedente_bn=excedente_bn,
            consumo_excedente_color=excedente_color,
            monto_renta=monto_total,
            monto_excedente=Decimal("0"),
            monto_iva=Decimal("0"),
            monto_total=monto_total,
            estado=Factura.Estado.OK,
        )

    def test_ultimos_n_meses_incluye_el_mes_en_curso_y_retrocede(self):
        meses = _ultimos_n_meses(date(2026, 3, 15), 3)

        self.assertEqual(meses, [(2026, 1), (2026, 2), (2026, 3)])

    def test_ultimos_n_meses_cruza_el_cambio_de_anio(self):
        meses = _ultimos_n_meses(date(2026, 2, 1), 3)

        self.assertEqual(meses, [(2025, 12), (2026, 1), (2026, 2)])

    def test_facturacion_mensual_separa_por_moneda_y_excluye_fuera_de_rango(self):
        contrato_mxn = self._contrato("CT-TEND-MXN", "MXN")
        contrato_usd = self._contrato("CT-TEND-USD", "USD")
        self._factura(contrato_mxn, 7, 2026, Decimal("1000.00"))
        self._factura(contrato_usd, 7, 2026, Decimal("300.00"))
        self._factura(contrato_mxn, 1, 2020, Decimal("999999.00"))  # muy fuera del rango de 12 meses

        series = facturacion_mensual(meses=12, hoy=date(2026, 7, 15))

        por_moneda = {s["moneda"]: s for s in series}
        self.assertEqual(set(por_moneda), {"MXN", "USD"})
        punto_julio_mxn = next(p for p in por_moneda["MXN"]["puntos"] if p["etiqueta"] == "Jul 2026")
        self.assertEqual(punto_julio_mxn["valor"], Decimal("1000.00"))
        # la factura de 2020 no debe aparecer en ningún punto de la serie MXN
        total_mxn = sum(p["valor"] for p in por_moneda["MXN"]["puntos"])
        self.assertEqual(total_mxn, Decimal("1000.00"))

    def test_excedente_mensual_suma_bn_y_color_del_mes_correcto(self):
        contrato = self._contrato("CT-TEND-EXC")
        self._factura(contrato, 7, 2026, Decimal("1000.00"), excedente_bn=500, excedente_color=100)
        self._factura(contrato, 6, 2026, Decimal("1000.00"), excedente_bn=200, excedente_color=0)

        puntos = excedente_mensual(meses=12, hoy=date(2026, 7, 15))

        punto_julio = next(p for p in puntos if p["etiqueta"] == "Jul 2026")
        punto_junio = next(p for p in puntos if p["etiqueta"] == "Jun 2026")
        self.assertEqual(punto_julio["bn"], 500)
        self.assertEqual(punto_julio["color"], 100)
        self.assertEqual(punto_junio["bn"], 200)
        self.assertEqual(punto_julio["pct_bn"], 100.0)  # es el máximo de la serie

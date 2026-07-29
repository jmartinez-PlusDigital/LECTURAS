import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from auditoria.tipos import AlertaAuditoria, ResultadoAuditoria
from core.dashboard import (
    _advertencias_consumo_recientes,
    _contratos_lectura_desactualizada,
    _equipos_offline_3manager,
    _facturado_mes_por_moneda,
    _ultimos_n_meses,
    excedente_mensual,
    facturacion_mensual,
)
from core.historial_lecturas import historial_de_contrato
from core.importacion_clientes import procesar_archivo_clientes
from core.importacion_contratos import procesar_archivo_contratos
from core.importacion_equipos import asignar_equipo_a_contrato
from core.importacion_lecturas import generar_plantilla_lecturas, procesar_archivo_lecturas
from core.models import Asignacion, Cliente, Contrato, Equipo, Factura, Lectura, LogEjecucion
from core.procesamiento_facturacion import _advertencias_de, _calcular_periodo


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

    def test_lectura_con_consumo_anomalo_reporta_advertencia_sin_bloquear(self):
        # Historial estable de 1000/mes.
        contador = 0
        for mes in range(1, 7):
            contador += 1000
            Lectura.objects.create(
                asignacion=self.asignacion1, fecha=date(2026, mes, 1), lectura_bn=contador, lectura_color=0,
                origen="manual",
            )

        contenido = f"numero_serie,fecha,lectura_bn,lectura_color\nSN-LEC-1,2026-07-01,{contador + 10000},0\n"
        resultados = procesar_archivo_lecturas(_archivo(contenido), "lecturas.csv")

        self.assertEqual(resultados[0]["error"], "")
        self.assertEqual(resultados[0]["lectura"], "creada")
        self.assertIn("BN", resultados[0]["advertencia"])
        lectura = Lectura.objects.get(asignacion=self.asignacion1, fecha=date(2026, 7, 1))
        self.assertEqual(lectura.estado_auditoria, Lectura.EstadoAuditoria.ALERTA)


class ImportacionClientesTestCase(TestCase):
    def test_crea_cliente(self):
        contenido = "nombre,rfc,activo\nCliente Importado SA,CIM010101AAA,true\n"
        resultados = procesar_archivo_clientes(_archivo(contenido), "clientes.csv")

        self.assertEqual(resultados[0]["error"], "")
        self.assertEqual(resultados[0]["cliente"], "creado")
        cliente = Cliente.objects.get(nombre="Cliente Importado SA")
        self.assertEqual(cliente.rfc, "CIM010101AAA")
        self.assertTrue(cliente.activo)

    def test_reprocesar_actualiza_en_vez_de_duplicar(self):
        primero = "nombre,rfc\nCliente Dup,RFC-1\n"
        segundo = "nombre,rfc\nCliente Dup,RFC-2\n"
        procesar_archivo_clientes(_archivo(primero), "clientes.csv")
        resultados = procesar_archivo_clientes(_archivo(segundo), "clientes.csv")

        self.assertEqual(resultados[0]["cliente"], "actualizado")
        self.assertEqual(Cliente.objects.filter(nombre="Cliente Dup").count(), 1)
        self.assertEqual(Cliente.objects.get(nombre="Cliente Dup").rfc, "RFC-2")

    def test_nombre_vacio_es_error_y_no_detiene_las_demas(self):
        contenido = "nombre,rfc\n,RFC-X\nCliente Valido,RFC-Y\n"
        resultados = procesar_archivo_clientes(_archivo(contenido), "clientes.csv")

        self.assertIn("nombre es requerido", resultados[0]["error"])
        self.assertEqual(resultados[1]["error"], "")
        self.assertTrue(Cliente.objects.filter(nombre="Cliente Valido").exists())

    def test_activo_por_defecto_es_true_si_se_omite(self):
        contenido = "nombre\nCliente Sin Activo\n"
        procesar_archivo_clientes(_archivo(contenido), "clientes.csv")

        self.assertTrue(Cliente.objects.get(nombre="Cliente Sin Activo").activo)

    def test_dry_run_no_persiste_nada(self):
        contenido = "nombre\nCliente Dry Run\n"
        resultados = procesar_archivo_clientes(_archivo(contenido), "clientes.csv", dry_run=True)

        self.assertEqual(resultados[0]["cliente"], "creado")
        self.assertFalse(Cliente.objects.filter(nombre="Cliente Dry Run").exists())


class ImportacionContratosTestCase(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cliente Contratos Test")

    def test_crea_contrato_para_cliente_existente(self):
        contenido = (
            "numero_contrato,cliente,renta_base,costo_excedente_bn,costo_excedente_color,"
            "dia_corte_facturacion,fecha_inicio\n"
            f"CT-IMP-1,{self.cliente.nombre},1500.00,0.50,1.50,1,2026-01-01\n"
        )
        resultados = procesar_archivo_contratos(_archivo(contenido), "contratos.csv")

        self.assertEqual(resultados[0]["error"], "")
        self.assertEqual(resultados[0]["contrato"], "creado")
        contrato = Contrato.objects.get(numero_contrato="CT-IMP-1")
        self.assertEqual(contrato.cliente, self.cliente)
        self.assertEqual(contrato.moneda, "MXN")  # default al omitirse
        self.assertEqual(contrato.iva_porcentaje, Decimal("16.00"))  # default al omitirse

    def test_cliente_inexistente_reporta_error_y_no_detiene_las_demas(self):
        contenido = (
            "numero_contrato,cliente,renta_base,costo_excedente_bn,costo_excedente_color,"
            "dia_corte_facturacion,fecha_inicio\n"
            "CT-IMP-X,Cliente Que No Existe,1000,0.5,1.5,1,2026-01-01\n"
            f"CT-IMP-Y,{self.cliente.nombre},1000,0.5,1.5,1,2026-01-01\n"
        )
        resultados = procesar_archivo_contratos(_archivo(contenido), "contratos.csv")

        self.assertIn("no existe un cliente", resultados[0]["error"])
        self.assertEqual(resultados[1]["error"], "")
        self.assertTrue(Contrato.objects.filter(numero_contrato="CT-IMP-Y").exists())

    def test_reprocesar_actualiza_en_vez_de_duplicar(self):
        primero = (
            "numero_contrato,cliente,renta_base,costo_excedente_bn,costo_excedente_color,"
            "dia_corte_facturacion,fecha_inicio\n"
            f"CT-IMP-DUP,{self.cliente.nombre},1000,0.5,1.5,1,2026-01-01\n"
        )
        segundo = (
            "numero_contrato,cliente,renta_base,costo_excedente_bn,costo_excedente_color,"
            "dia_corte_facturacion,fecha_inicio\n"
            f"CT-IMP-DUP,{self.cliente.nombre},2000,0.5,1.5,1,2026-01-01\n"
        )
        procesar_archivo_contratos(_archivo(primero), "contratos.csv")
        resultados = procesar_archivo_contratos(_archivo(segundo), "contratos.csv")

        self.assertEqual(resultados[0]["contrato"], "actualizado")
        self.assertEqual(Contrato.objects.filter(numero_contrato="CT-IMP-DUP").count(), 1)
        self.assertEqual(Contrato.objects.get(numero_contrato="CT-IMP-DUP").renta_base, Decimal("2000"))

    def test_fecha_fin_anterior_a_fecha_inicio_es_error(self):
        contenido = (
            "numero_contrato,cliente,renta_base,costo_excedente_bn,costo_excedente_color,"
            "dia_corte_facturacion,fecha_inicio,fecha_fin\n"
            f"CT-IMP-FF,{self.cliente.nombre},1000,0.5,1.5,1,2026-06-01,2026-01-01\n"
        )
        resultados = procesar_archivo_contratos(_archivo(contenido), "contratos.csv")

        self.assertIn("fecha_fin debe ser posterior", resultados[0]["error"])
        self.assertFalse(Contrato.objects.filter(numero_contrato="CT-IMP-FF").exists())

    def test_dia_corte_fuera_de_rango_es_error(self):
        contenido = (
            "numero_contrato,cliente,renta_base,costo_excedente_bn,costo_excedente_color,"
            "dia_corte_facturacion,fecha_inicio\n"
            f"CT-IMP-DC,{self.cliente.nombre},1000,0.5,1.5,32,2026-01-01\n"
        )
        resultados = procesar_archivo_contratos(_archivo(contenido), "contratos.csv")

        self.assertIn("dia_corte_facturacion", resultados[0]["error"])

    def test_dry_run_no_persiste_nada(self):
        contenido = (
            "numero_contrato,cliente,renta_base,costo_excedente_bn,costo_excedente_color,"
            "dia_corte_facturacion,fecha_inicio\n"
            f"CT-IMP-DRY,{self.cliente.nombre},1000,0.5,1.5,1,2026-01-01\n"
        )
        resultados = procesar_archivo_contratos(_archivo(contenido), "contratos.csv", dry_run=True)

        self.assertEqual(resultados[0]["contrato"], "creado")
        self.assertFalse(Contrato.objects.filter(numero_contrato="CT-IMP-DRY").exists())


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


class LecturaAnomaliaTestCase(TestCase):
    """`Lectura.save()` marca `estado_auditoria=ALERTA` cuando el consumo
    implícito de la lectura se sale del rango normal del equipo — validación
    en el momento de captura, sin esperar a la auditoría de fin de mes."""

    def setUp(self):
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-ANOM-1",
            cliente=Cliente.objects.create(nombre="Cliente Anomalia Test"),
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        self.equipo = Equipo.objects.create(
            numero_serie="SN-ANOM-1", marca="Canon", modelo="X1", metodo_lectura="manual"
        )
        self.asignacion = Asignacion.objects.create(
            equipo=self.equipo,
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )

    def _historial_estable(self, meses=6, consumo_mensual=1000):
        contador = 0
        for mes in range(1, meses + 1):
            contador += consumo_mensual
            Lectura.objects.create(
                asignacion=self.asignacion, fecha=date(2026, mes, 1), lectura_bn=contador, lectura_color=0,
                origen="manual",
            )
        return contador

    def test_consumo_dentro_de_lo_normal_no_marca_alerta(self):
        ultimo = self._historial_estable()
        lectura = Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 7, 1), lectura_bn=ultimo + 1200, lectura_color=0,
            origen="manual",
        )

        self.assertEqual(lectura.estado_auditoria, Lectura.EstadoAuditoria.OK)
        self.assertEqual(lectura.detalle_auditoria, {})

    def test_consumo_muy_por_encima_del_promedio_marca_alerta(self):
        ultimo = self._historial_estable()
        lectura = Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 7, 1), lectura_bn=ultimo + 10000, lectura_color=0,
            origen="manual",
        )

        self.assertEqual(lectura.estado_auditoria, Lectura.EstadoAuditoria.ALERTA)
        advertencias = lectura.detalle_auditoria["advertencias"]
        self.assertEqual(len(advertencias), 1)
        self.assertEqual(advertencias[0]["categoria"], "bn")

    def test_primera_lectura_de_la_asignacion_no_marca_alerta_aunque_sea_grande(self):
        lectura = Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 1, 1), lectura_bn=999999, lectura_color=0,
            origen="manual",
        )

        self.assertEqual(lectura.estado_auditoria, Lectura.EstadoAuditoria.OK)

    def test_sin_historial_suficiente_no_marca_alerta(self):
        Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 1, 1), lectura_bn=1000, lectura_color=0, origen="manual",
        )
        # Solo una lectura previa: promedio_historico_mensual exige al menos 2
        # para establecer una base confiable.
        lectura = Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 2, 1), lectura_bn=999999, lectura_color=0,
            origen="manual",
        )

        self.assertEqual(lectura.estado_auditoria, Lectura.EstadoAuditoria.OK)

    def test_lectura_invertida_sin_rollover_no_se_evalua_como_anomalia(self):
        # Ya se marca por otro motivo (candado a, ver auditoria/motor.py);
        # detectar_anomalias no debe pisar ni duplicar esa evaluación.
        self._historial_estable()
        lectura = Lectura.objects.create(
            asignacion=self.asignacion, fecha=date(2026, 7, 1), lectura_bn=100, lectura_color=0, origen="manual",
        )

        self.assertEqual(lectura.estado_auditoria, Lectura.EstadoAuditoria.OK)


class AdvertenciasDeAuditoriaTestCase(TestCase):
    def test_convierte_alertas_no_bloqueantes_a_dicts_serializables(self):
        resultado = ResultadoAuditoria(contrato_id=1, fecha_inicio=date(2026, 1, 1), fecha_fin=date(2026, 1, 31))
        resultado.alertas.append(
            AlertaAuditoria(
                candado="f",
                equipo_numero_serie="SN-1",
                bloqueante=False,
                mensaje="Consumo BN (5000) supera 3.5x el promedio histórico (1000.0).",
                datos={"consumo": 5000, "promedio_historico": 1000.0},
            )
        )

        self.assertEqual(
            _advertencias_de(resultado),
            [
                {
                    "candado": "f",
                    "equipo": "SN-1",
                    "mensaje": "Consumo BN (5000) supera 3.5x el promedio histórico (1000.0).",
                    "datos": {"consumo": 5000, "promedio_historico": 1000.0},
                }
            ],
        )


class AdvertenciasConsumoRecientesTestCase(TestCase):
    def _contrato(self, numero):
        return Contrato.objects.create(
            numero_contrato=numero,
            cliente=Cliente.objects.create(nombre=f"Cliente {numero}"),
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )

    def test_incluye_advertencias_de_facturas_aprobadas(self):
        contrato = self._contrato("CT-ADV-1")
        LogEjecucion.objects.create(
            contrato=contrato,
            estado=LogEjecucion.Estado.OK,
            detalle={
                "fase": "completo",
                "factura_id": 1,
                "advertencias": [
                    {"candado": "f", "equipo": "SN-1", "mensaje": "Consumo BN fuera de lo normal.", "datos": {}}
                ],
            },
        )

        filas = _advertencias_consumo_recientes()

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["equipo"], "SN-1")
        self.assertEqual(filas[0]["contrato"], contrato)
        self.assertEqual(filas[0]["mensaje"], "Consumo BN fuera de lo normal.")

    def test_ignora_logs_sin_advertencias(self):
        contrato = self._contrato("CT-ADV-2")
        LogEjecucion.objects.create(
            contrato=contrato,
            estado=LogEjecucion.Estado.OK,
            detalle={"fase": "completo", "factura_id": 1},
        )

        self.assertEqual(_advertencias_consumo_recientes(), [])

    def test_ignora_logs_que_no_estan_ok(self):
        # Un LogEjecucion PENDIENTE ya se muestra aparte, como alerta
        # bloqueante (ver dashboard._alertas_pendientes_activas); no debe
        # duplicarse en el panel de advertencias no bloqueantes.
        contrato = self._contrato("CT-ADV-3")
        LogEjecucion.objects.create(
            contrato=contrato,
            estado=LogEjecucion.Estado.PENDIENTE,
            detalle={"fase": "auditoria", "advertencias": [{"candado": "f", "equipo": "SN-2", "mensaje": "x", "datos": {}}]},
        )

        self.assertEqual(_advertencias_consumo_recientes(), [])


class AsignarEquipoAContratoTestCase(TestCase):
    """`asignar_equipo_a_contrato` es pública y compartida entre el
    importador CSV y la acción masiva del Admin (ver EquipoAdmin en
    core/admin.py) — se prueba directo aquí, sin depender de ninguno de los
    dos caminos que la llaman."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cliente Asignar Test")
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-ASIG-1",
            cliente=self.cliente,
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        self.equipo = Equipo.objects.create(
            numero_serie="SN-ASIG-1", marca="Canon", modelo="X1", metodo_lectura="manual"
        )

    def test_crea_la_asignacion_y_activa_el_equipo(self):
        detalle = asignar_equipo_a_contrato(
            self.equipo,
            self.contrato.numero_contrato,
            {"fecha_inicio_asignacion": "2026-02-01", "lectura_inicial_bn": 0, "lectura_inicial_color": 0},
        )

        self.assertIn("creada", detalle)
        asignacion = Asignacion.objects.get(equipo=self.equipo)
        self.assertEqual(asignacion.contrato, self.contrato)
        self.assertEqual(asignacion.fecha_inicio, date(2026, 2, 1))
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado_actual, Equipo.EstadoActual.ACTIVO)

    def test_equipo_ya_asignado_a_otro_contrato_es_error(self):
        otro_contrato = Contrato.objects.create(
            numero_contrato="CT-ASIG-2",
            cliente=self.cliente,
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        Asignacion.objects.create(
            equipo=self.equipo,
            contrato=otro_contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )

        with self.assertRaises(ValueError):
            asignar_equipo_a_contrato(
                self.equipo,
                self.contrato.numero_contrato,
                {"fecha_inicio_asignacion": "2026-02-01", "lectura_inicial_bn": 0, "lectura_inicial_color": 0},
            )

    def test_equipo_ya_asignado_al_mismo_contrato_no_duplica(self):
        Asignacion.objects.create(
            equipo=self.equipo,
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )

        detalle = asignar_equipo_a_contrato(
            self.equipo,
            self.contrato.numero_contrato,
            {"fecha_inicio_asignacion": "2026-02-01", "lectura_inicial_bn": 0, "lectura_inicial_color": 0},
        )

        self.assertIn("ya estaba asignado", detalle)
        self.assertEqual(Asignacion.objects.filter(equipo=self.equipo).count(), 1)


class AsignarMasivoViewTestCase(TestCase):
    """Vista del Admin detrás de la acción "Asignar equipos seleccionados a
    un contrato" del listado de Equipos (ver EquipoAdmin.asignar_masivo_view)."""

    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="admin-asignar-test", email="admin@test.com", password="clave-segura-123"
        )
        self.client = Client()
        self.client.force_login(self.admin_user)

        self.cliente = Cliente.objects.create(nombre="Cliente Asignar Masivo Test")
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-MASIVO-1",
            cliente=self.cliente,
            estado=Contrato.Estado.ACTIVO,
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        self.equipo1 = Equipo.objects.create(
            numero_serie="SN-MASIVO-1", marca="Canon", modelo="X1", metodo_lectura="manual"
        )
        self.equipo2 = Equipo.objects.create(
            numero_serie="SN-MASIVO-2", marca="Canon", modelo="X2", metodo_lectura="manual"
        )

    def test_post_asigna_todos_los_equipos_seleccionados(self):
        url = reverse("admin:core_equipo_asignar_masivo")
        respuesta = self.client.post(
            url,
            {
                "ids": f"{self.equipo1.pk},{self.equipo2.pk}",
                "contrato": self.contrato.pk,
                "fecha_inicio": "2026-02-01",
                f"lectura_bn_{self.equipo1.pk}": 100,
                f"lectura_color_{self.equipo1.pk}": 0,
                f"lectura_bn_{self.equipo2.pk}": 200,
                f"lectura_color_{self.equipo2.pk}": 50,
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(Asignacion.objects.filter(contrato=self.contrato).count(), 2)
        asignacion1 = Asignacion.objects.get(equipo=self.equipo1)
        self.assertEqual(asignacion1.lectura_inicial_referencia_bn, 100)
        asignacion2 = Asignacion.objects.get(equipo=self.equipo2)
        self.assertEqual(asignacion2.lectura_inicial_referencia_color, 50)

    def test_equipos_ya_asignados_se_omiten_de_la_pantalla(self):
        Asignacion.objects.create(
            equipo=self.equipo1,
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )
        url = reverse("admin:core_equipo_asignar_masivo") + f"?ids={self.equipo1.pk},{self.equipo2.pk}"

        respuesta = self.client.get(url)

        self.assertEqual(respuesta.status_code, 200)
        equipos_en_formulario = [fila[0] for fila in respuesta.context["filas_formulario"]]
        self.assertEqual(equipos_en_formulario, [self.equipo2])

    def test_sin_ids_redirige_al_listado(self):
        url = reverse("admin:core_equipo_asignar_masivo")

        respuesta = self.client.get(url)

        self.assertRedirects(respuesta, reverse("admin:core_equipo_changelist"))


class EquiposOffline3ManagerTestCase(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cliente Offline Test")
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-OFFLINE-1",
            cliente=self.cliente,
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )

    def test_incluye_equipos_3manager_offline_con_su_contrato_activo(self):
        equipo = Equipo.objects.create(
            numero_serie="SN-DASH-OFFLINE-1",
            marca="Ricoh",
            modelo="IM 550",
            metodo_lectura="api_3manager",
            en_linea_api=False,
        )
        Asignacion.objects.create(
            equipo=equipo,
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )

        equipos = _equipos_offline_3manager()

        self.assertEqual(len(equipos), 1)
        self.assertEqual(equipos[0].numero_serie, "SN-DASH-OFFLINE-1")
        self.assertEqual(equipos[0].contrato_activo, self.contrato)

    def test_no_incluye_equipos_en_linea_ni_manuales(self):
        Equipo.objects.create(
            numero_serie="SN-DASH-ONLINE",
            marca="Canon",
            modelo="X1",
            metodo_lectura="api_3manager",
            en_linea_api=True,
        )
        Equipo.objects.create(
            numero_serie="SN-DASH-MANUAL", marca="Canon", modelo="X2", metodo_lectura="manual"
        )

        self.assertEqual(_equipos_offline_3manager(), [])

    def test_no_incluye_equipos_dados_de_baja(self):
        Equipo.objects.create(
            numero_serie="SN-DASH-BAJA",
            marca="Canon",
            modelo="X3",
            metodo_lectura="api_3manager",
            en_linea_api=False,
            estado_actual=Equipo.EstadoActual.BAJA,
        )

        self.assertEqual(_equipos_offline_3manager(), [])


class SincronizarLecturasEstatusApiTestCase(TestCase):
    """La parte nueva de sincronizar_lecturas: además de crear la Lectura,
    persiste en_linea_api/ultima_actualizacion_api en el Equipo cuando la
    fuente los reporta (ver integrations.LecturaExterna)."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cliente Sync Estatus Test")
        self.contrato = Contrato.objects.create(
            numero_contrato="CT-SYNC-1",
            cliente=self.cliente,
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=1,
            fecha_inicio=date(2026, 1, 1),
        )
        self.equipo = Equipo.objects.create(
            numero_serie="SN-SYNC-1",
            marca="Ricoh",
            modelo="IM 550",
            metodo_lectura="api_3manager",
            id_externo_3manager="DEV-SYNC-1",
        )
        Asignacion.objects.create(
            equipo=self.equipo,
            contrato=self.contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )

    def test_actualiza_estatus_del_equipo_al_sincronizar(self):
        from unittest.mock import Mock, patch

        from django.core.management import call_command

        from integrations import LecturaExterna

        lectura_externa = LecturaExterna(
            id_externo="DEV-SYNC-1",
            lectura_bn=500,
            lectura_color=0,
            fecha=timezone.localdate(),
            en_linea=False,
            ultima_actualizacion=timezone.now(),
        )
        cliente_falso = Mock()
        cliente_falso.obtener_lecturas.return_value = {"DEV-SYNC-1": lectura_externa}

        with patch(
            "core.sincronizacion_lecturas.get_client",
            side_effect=lambda metodo: cliente_falso if metodo == "api_3manager" else Mock(obtener_lecturas=Mock(return_value={})),
        ):
            call_command("sincronizar_lecturas")

        self.equipo.refresh_from_db()
        self.assertFalse(self.equipo.en_linea_api)
        self.assertIsNotNone(self.equipo.ultima_actualizacion_api)

    def test_fuente_sin_equipos_no_tumba_el_estado_aunque_no_este_configurada(self):
        """Regresión: con 0 equipos api_printaudit en el sistema, esa fuente
        sin configurar (API_PRINTAUDIT_BASE_URL/API_KEY vacíos) marcaba
        error_fuente y el ciclo completo quedaba en estado Error, aunque
        3-Manager sí hubiera sincronizado bien y nada dependiera de PrintAudit."""
        from unittest.mock import Mock, patch

        from integrations import LecturaExterna
        from core.sincronizacion_lecturas import sincronizar_lecturas

        lectura_externa = LecturaExterna(
            id_externo="DEV-SYNC-1", lectura_bn=500, lectura_color=0, fecha=timezone.localdate()
        )
        cliente_falso = Mock()
        cliente_falso.obtener_lecturas.return_value = {"DEV-SYNC-1": lectura_externa}

        with patch("core.sincronizacion_lecturas.get_client", return_value=cliente_falso):
            log = sincronizar_lecturas()

        self.assertEqual(log.estado, LogEjecucion.Estado.OK)
        self.assertNotIn("error_fuente", log.detalle["fuentes"]["api_printaudit"])
        self.assertEqual(log.detalle["fuentes"]["api_printaudit"]["equipos_evaluados"], 0)


class SincronizarLecturasAhoraViewTestCase(TestCase):
    """Botón "Sincronizar lecturas ahora" del dashboard (ver
    sincronizar_lecturas_ahora_view en core/admin.py)."""

    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="admin-sync-test", email="admin-sync@test.com", password="clave-segura-123"
        )
        self.client = Client()
        self.client.force_login(self.admin_user)

    def test_post_corre_la_sincronizacion_y_redirige_al_dashboard(self):
        from unittest.mock import Mock, patch

        with patch("core.admin.sincronizar_lecturas") as mock_sincronizar:
            mock_sincronizar.return_value = Mock(
                estado=LogEjecucion.Estado.OK,
                detalle={"fuentes": {"api_3manager": {"lecturas_creadas": 3, "lecturas_actualizadas": 1}}},
            )
            respuesta = self.client.post(reverse("dashboard-sincronizar-lecturas"))

        mock_sincronizar.assert_called_once()
        self.assertRedirects(respuesta, reverse("admin:index"))

    def test_get_no_esta_permitido(self):
        respuesta = self.client.get(reverse("dashboard-sincronizar-lecturas"))

        self.assertEqual(respuesta.status_code, 403)


class ContratosLecturaDesactualizadaTestCase(TestCase):
    """Alerta temprana del dashboard: contratos donde ningún equipo activo
    reportó lectura en `dias_alerta` días, sin importar qué tan lejos esté
    su corte (ver core.dashboard._contratos_lectura_desactualizada)."""

    def _contrato(self, numero):
        return Contrato.objects.create(
            numero_contrato=numero,
            cliente=Cliente.objects.create(nombre=f"Cliente {numero}"),
            renta_base=Decimal("1000.00"),
            costo_excedente_bn=Decimal("0.50"),
            costo_excedente_color=Decimal("1.50"),
            dia_corte_facturacion=27,  # lejos de "hoy" en las pruebas, a propósito
            fecha_inicio=date(2026, 1, 1),
        )

    def _asignacion_activa(self, contrato, serie):
        equipo = Equipo.objects.create(numero_serie=serie, marca="Canon", modelo="X1", metodo_lectura="manual")
        return Asignacion.objects.create(
            equipo=equipo,
            contrato=contrato,
            fecha_inicio=date(2026, 1, 1),
            lectura_inicial_referencia_bn=0,
            lectura_inicial_referencia_color=0,
        )

    def test_contrato_sin_lectura_reciente_aparece_con_dias_correctos(self):
        contrato = self._contrato("CT-DESACT-1")
        asignacion = self._asignacion_activa(contrato, "SN-DESACT-1")
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 7, 20), lectura_bn=100, lectura_color=0, origen="manual"
        )

        resultado = _contratos_lectura_desactualizada(date(2026, 7, 29), dias_alerta=3)

        contratos_en_resultado = {r["contrato"].numero_contrato: r for r in resultado}
        self.assertIn("CT-DESACT-1", contratos_en_resultado)
        self.assertEqual(contratos_en_resultado["CT-DESACT-1"]["dias_sin_lectura"], 9)

    def test_contrato_con_lectura_reciente_no_aparece(self):
        contrato = self._contrato("CT-DESACT-2")
        asignacion = self._asignacion_activa(contrato, "SN-DESACT-2")
        Lectura.objects.create(
            asignacion=asignacion, fecha=date(2026, 7, 28), lectura_bn=100, lectura_color=0, origen="manual"
        )

        resultado = _contratos_lectura_desactualizada(date(2026, 7, 29), dias_alerta=3)

        self.assertNotIn("CT-DESACT-2", {r["contrato"].numero_contrato for r in resultado})

    def test_contrato_sin_ninguna_lectura_aparece_como_nunca(self):
        contrato = self._contrato("CT-DESACT-3")
        self._asignacion_activa(contrato, "SN-DESACT-3")

        resultado = _contratos_lectura_desactualizada(date(2026, 7, 29), dias_alerta=3)

        contratos_en_resultado = {r["contrato"].numero_contrato: r for r in resultado}
        self.assertIn("CT-DESACT-3", contratos_en_resultado)
        self.assertIsNone(contratos_en_resultado["CT-DESACT-3"]["ultima_lectura_fecha"])
        self.assertIsNone(contratos_en_resultado["CT-DESACT-3"]["dias_sin_lectura"])

    def test_contrato_sin_equipos_activos_no_aparece(self):
        # Un contrato sin ninguna asignación vigente no es un problema de
        # conectividad — es otro escenario (candado c, equipo omitido).
        self._contrato("CT-DESACT-4")

        resultado = _contratos_lectura_desactualizada(date(2026, 7, 29), dias_alerta=3)

        self.assertNotIn("CT-DESACT-4", {r["contrato"].numero_contrato for r in resultado})

    def test_ordena_los_mas_desactualizados_primero(self):
        contrato_a = self._contrato("CT-DESACT-5A")
        asignacion_a = self._asignacion_activa(contrato_a, "SN-DESACT-5A")
        Lectura.objects.create(
            asignacion=asignacion_a, fecha=date(2026, 7, 10), lectura_bn=100, lectura_color=0, origen="manual"
        )
        contrato_b = self._contrato("CT-DESACT-5B")
        asignacion_b = self._asignacion_activa(contrato_b, "SN-DESACT-5B")
        Lectura.objects.create(
            asignacion=asignacion_b, fecha=date(2026, 7, 24), lectura_bn=100, lectura_color=0, origen="manual"
        )

        resultado = _contratos_lectura_desactualizada(date(2026, 7, 29), dias_alerta=3)

        numeros = [r["contrato"].numero_contrato for r in resultado]
        self.assertLess(numeros.index("CT-DESACT-5A"), numeros.index("CT-DESACT-5B"))

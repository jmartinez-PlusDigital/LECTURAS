from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from core.models import Cliente
from integrations import Cliente3Manager, IntegrationConnectionError


def _mock_response(payload):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = payload
    return response


class Cliente3ManagerTestCase(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cuenta Test", id_cuenta_3manager="ACC-1")

    def test_sin_configuracion_lanza_error_controlado(self):
        # Cliente3Manager.__init__ hace `tenant or settings.API_3MANAGER_TENANT`:
        # si el entorno real ya tiene credenciales configuradas (como en este
        # proyecto, para poder probar contra la API real), pasar tenant=""
        # no basta para simular "sin configurar" — también hay que vaciar el
        # setting, si no la prueba depende de que el .env esté vacío.
        with override_settings(API_3MANAGER_TENANT="", API_3MANAGER_USERNAME="", API_3MANAGER_PASSWORD=""):
            with self.assertRaises(IntegrationConnectionError):
                Cliente3Manager(tenant="", username="", password="").obtener_lecturas()

    def test_sin_clientes_con_cuenta_configurada_devuelve_vacio(self):
        Cliente.objects.all().delete()
        with patch("integrations.tresmanager.requests.post") as mock_post:
            resultado = Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()
        self.assertEqual(resultado, {})
        mock_post.assert_not_called()  # ni siquiera se pide token si no hay cuentas que consultar

    def test_parsea_campos_reales_totalbw_totalcolor(self):
        """Regresión: el campo real es `totalBw` (no `totalBW`), confirmado contra la API real de Demant."""
        token_response = _mock_response({"access_token": "TOKEN"})
        devices_response = _mock_response(
            {
                "items": [
                    {"deviceId": "DEV-1", "serialNumber": "0301836500", "totalBw": 144773, "totalColor": 94340},
                ]
            }
        )
        with patch("integrations.tresmanager.requests.post", return_value=token_response), \
                patch("integrations.tresmanager.requests.get", return_value=devices_response):
            resultado = Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()

        self.assertEqual(resultado["0301836500"].lectura_bn, 144773)
        self.assertEqual(resultado["0301836500"].lectura_color, 94340)
        self.assertIs(resultado["DEV-1"], resultado["0301836500"])  # indexado también por deviceId

    def test_equipo_solo_bn_con_totalcolor_null_no_se_descarta(self):
        """Regresión: equipos BN reportan totalColor=null (presente, no ausente) — no debe tronar ni descartarse."""
        token_response = _mock_response({"access_token": "TOKEN"})
        devices_response = _mock_response(
            {
                "items": [
                    {"deviceId": "DEV-2", "serialNumber": "0F00254Y00", "totalBw": 479367, "totalColor": None},
                ]
            }
        )
        with patch("integrations.tresmanager.requests.post", return_value=token_response), \
                patch("integrations.tresmanager.requests.get", return_value=devices_response):
            resultado = Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()

        self.assertIn("0F00254Y00", resultado)
        self.assertEqual(resultado["0F00254Y00"].lectura_bn, 479367)
        self.assertEqual(resultado["0F00254Y00"].lectura_color, 0)

    def test_una_cuenta_con_error_no_tumba_la_sincronizacion_de_las_demas(self):
        """Regresión: un id_cuenta_3manager inválido/sin acceso en un Cliente
        tumbaba obtener_lecturas() para TODOS los clientes, no solo el malo
        (encontrado en producción: un cliente de prueba con GUID inventado
        rompía la sincronización completa)."""
        import requests

        Cliente.objects.create(nombre="Cliente Cuenta Mala", id_cuenta_3manager="ACC-MALA")
        token_response = _mock_response({"access_token": "TOKEN"})
        ok_response = _mock_response(
            {"items": [{"deviceId": "DEV-OK", "serialNumber": "SN-OK", "totalBw": 100, "totalColor": 0}]}
        )

        def get_side_effect(url, **kwargs):
            if "ACC-MALA" in url:
                respuesta = Mock()
                respuesta.raise_for_status.side_effect = requests.HTTPError("403 Client Error")
                return respuesta
            return ok_response

        with patch("integrations.tresmanager.requests.post", return_value=token_response), \
                patch("integrations.tresmanager.requests.get", side_effect=get_side_effect):
            resultado = Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()

        self.assertIn("SN-OK", resultado)

    def test_fallo_de_token_se_envuelve_en_error_controlado(self):
        import requests

        with patch("integrations.tresmanager.requests.post", side_effect=requests.ConnectionError("timeout")):
            with self.assertRaises(IntegrationConnectionError):
                Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()

    def test_extrae_en_linea_y_ultima_actualizacion_del_payload_real(self):
        token_response = _mock_response({"access_token": "TOKEN"})
        devices_response = _mock_response(
            {
                "items": [
                    {
                        "deviceId": "DEV-3",
                        "serialNumber": "3202XB56278",
                        "totalBw": 476520,
                        "totalColor": None,
                        "isOffline": False,
                        "latestReadingTime": "2026-07-29T17:04:04.6017326Z",
                    },
                ]
            }
        )
        with patch("integrations.tresmanager.requests.post", return_value=token_response), \
                patch("integrations.tresmanager.requests.get", return_value=devices_response):
            resultado = Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()

        lectura = resultado["3202XB56278"]
        self.assertTrue(lectura.en_linea)
        self.assertEqual(lectura.ultima_actualizacion.year, 2026)
        self.assertEqual(lectura.ultima_actualizacion.hour, 17)

    def test_isoffline_true_se_invierte_a_en_linea_false(self):
        token_response = _mock_response({"access_token": "TOKEN"})
        devices_response = _mock_response(
            {"items": [{"deviceId": "DEV-4", "serialNumber": "SN-OFFLINE", "totalBw": 100, "isOffline": True}]}
        )
        with patch("integrations.tresmanager.requests.post", return_value=token_response), \
                patch("integrations.tresmanager.requests.get", return_value=devices_response):
            resultado = Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()

        self.assertFalse(resultado["SN-OFFLINE"].en_linea)

    def test_sin_isoffline_ni_latestreadingtime_quedan_en_none(self):
        """Compatibilidad con equipos/respuestas donde 3-Manager no incluye
        estos campos (o para no romper si algún día deja de mandarlos)."""
        token_response = _mock_response({"access_token": "TOKEN"})
        devices_response = _mock_response(
            {"items": [{"deviceId": "DEV-5", "serialNumber": "SN-SIN-ESTATUS", "totalBw": 100}]}
        )
        with patch("integrations.tresmanager.requests.post", return_value=token_response), \
                patch("integrations.tresmanager.requests.get", return_value=devices_response):
            resultado = Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()

        lectura = resultado["SN-SIN-ESTATUS"]
        self.assertIsNone(lectura.en_linea)
        self.assertIsNone(lectura.ultima_actualizacion)

    def test_client_id_usa_tenant_configurado(self):
        token_response = _mock_response({"access_token": "TOKEN"})
        devices_response = _mock_response({"items": []})
        with patch("integrations.tresmanager.requests.post", return_value=token_response) as mock_post, \
                patch("integrations.tresmanager.requests.get", return_value=devices_response):
            Cliente3Manager(tenant="plusconnect", username="u", password="p").obtener_lecturas()

        self.assertEqual(mock_post.call_args.kwargs["data"]["client_id"], "plusconnect-3m-api")
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "password")

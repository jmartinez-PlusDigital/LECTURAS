from unittest.mock import Mock, patch

from django.test import TestCase

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

    def test_fallo_de_token_se_envuelve_en_error_controlado(self):
        import requests

        with patch("integrations.tresmanager.requests.post", side_effect=requests.ConnectionError("timeout")):
            with self.assertRaises(IntegrationConnectionError):
                Cliente3Manager(tenant="app", username="u", password="p").obtener_lecturas()

    def test_client_id_usa_tenant_configurado(self):
        token_response = _mock_response({"access_token": "TOKEN"})
        devices_response = _mock_response({"items": []})
        with patch("integrations.tresmanager.requests.post", return_value=token_response) as mock_post, \
                patch("integrations.tresmanager.requests.get", return_value=devices_response):
            Cliente3Manager(tenant="plusconnect", username="u", password="p").obtener_lecturas()

        self.assertEqual(mock_post.call_args.kwargs["data"]["client_id"], "plusconnect-3m-api")
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "password")

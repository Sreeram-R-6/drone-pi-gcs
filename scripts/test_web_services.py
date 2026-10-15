"""HTTP and installation checks; no aircraft or camera access."""
import http.client
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import web_dashboard as dashboard


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "web" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


qr = module("qr_receiver")
installer = module("install_services")


class Tests(unittest.TestCase):
    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, method="GET", body=None):
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = client.getresponse()
        result = response.status, response.read()
        client.close()
        return result

    def test_config_sites_and_units(self):
        config = dashboard.load_config(dashboard.ASSETS / "sites.json")
        self.assertEqual([row["port"] for row in config["sites"]], [8071, 8080, 8090, 5000])
        for unit in installer.UNITS:
            rendered = installer.render((installer.ASSETS / (unit + ".in")).read_text(), installer.ROOT, "user")
            self.assertNotIn("@ROOT@", rendered)
            self.assertIn("User=user", rendered)
            self.assertIn("WantedBy=multi-user.target", rendered)
            self.assertNotIn("--execute", rendered)

    def test_gcs_service_can_write_shared_controller_lock(self):
        template = (installer.ASSETS / "drone-mavproxy-web.service.in").read_text()
        self.assertIn('ReadWritePaths="@ROOT@/video" /tmp', template)
        self.assertNotIn("PrivateTmp=true", template)

    def test_root_service_account_rejected(self):
        with self.assertRaises(ValueError):
            installer.render("User=@USER@", installer.ROOT, "root")

    def test_dashboard_lists_availability_and_boot_status(self):
        server = self.start(dashboard.DashboardServer(("127.0.0.1", 0), {"sites": [{"name": "Videos", "port": 8080, "unit": "drone-video-gallery.service"}]}))
        with patch.object(dashboard, "site_status", return_value={"online": True, "startup": "enabled"}):
            status, data = self.request(server, "/api/sites")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["sites"][0]["startup"], "enabled")
        self.assertIn(b"Your drone workspace", self.request(server, "/")[1])

    def test_scripts_page_embeds_terminal_at_matching_host(self):
        server=self.start(dashboard.DashboardServer(('127.0.0.1',0),{'sites':[]}))
        client=http.client.HTTPConnection('127.0.0.1',server.server_port)
        client.request('GET','/scripts',headers={'Host':'100.112.205.54:8070'})
        response=client.getresponse();page=response.read()
        self.assertEqual(response.status,200)
        self.assertIn(b'id="codex-terminal"',page)
        self.assertIn('frame-src http://100.112.205.54:8071;',response.getheader('Content-Security-Policy'))
        client.close()

    def test_qr_ingestion_and_escaped_display(self):
        server = self.start(qr.QRServer(("127.0.0.1", 0)))
        status, data = self.request(server, "/qr", "POST", json.dumps({"data": "<script>alert(1)</script>"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.request(server, "/api/latest")[1])["data"], "<script>alert(1)</script>")
        page = self.request(server, "/")[1]
        self.assertIn(b"&lt;script&gt;", page)
        self.assertNotIn(b"<script>", page)

    def test_invalid_qr_request(self):
        server = self.start(qr.QRServer(("127.0.0.1", 0)))
        self.assertEqual(self.request(server, "/qr", "POST", "bad-json")[0], 400)
        self.assertEqual(self.request(server, "/qr", "POST", "{}")[0], 400)
        self.assertEqual(self.request(server, "/missing", "POST", "{}")[0], 404)

    def test_qr_dashboard_link_uses_request_host(self):
        server = self.start(qr.QRServer(("127.0.0.1", 0)))
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("GET", "/", headers={"Host": "100.112.205.54:5000"})
        response = client.getresponse()
        page = response.read()
        client.close()
        self.assertIn(b'href="http://100.112.205.54:8070/"', page)
        self.assertNotIn(b'href="http://localhost:8070/"', page)


if __name__ == "__main__":
    unittest.main()

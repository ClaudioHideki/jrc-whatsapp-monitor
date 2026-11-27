import base64
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests
from cryptography.fernet import Fernet
from flask import Flask, session

from pabx import PABXClient, PABXConfig, PABXError, pabx_origin, pages
from pabx_monitor import PABXMonitor, extension_state, recording_filename
from pabx_routes import register_pabx
from pabx_store import PABXProfile, PABXService, PABXStore


PROFILE = PABXProfile(
    company="Empresa demonstração", url="https://pabx.example.invalid",
    email="demo@example.invalid", password="senha-simulada", account="demo",
    interval=5, enabled=True, scope_confirmed=True, cdr_customer="55", cdr_confirmed=True,
)
CONFIG = PABXConfig(PROFILE.url, PROFILE.email, PROFILE.password, PROFILE.account, cdr_customer="55")
HTTP_ORIGIN = "http://pabx.example.invalid:8080"
DATES = {"start": "2026-11-01", "end": "2026-11-07"}
EXTENSIONS = [
    {"name": "demo-101", "accountcode": "demo", "alias": "101", "alias_name": "101 - Ana Teste", "secret": "excluded"},
    {"name": "demo-102", "accountcode": "demo", "alias": "102", "alias_name": "102 - Bruno Teste"},
]
QUEUE = {
    "name": "demo-fila", "descricao": "Suporte teste", "accountcode": "demo", "strategy": "ringall",
    "membros": [{"ramal": EXTENSIONS[0], "paused": 1}, {"ramal": EXTENSIONS[1], "paused": 0}],
}
CDR = [
    {
        "uniqueid": "123.1", "calldate": "2026-11-05 09:00:00", "cliente_id": 55,
        "origem": "00000123456", "destino": "101", "status": "Atendida", "direcao": "entrada",
        "arquivo_audio": "https://recordings.example.invalid/private.wav",
    },
    {
        "uniqueid": "123.1", "calldate": "2026-11-05 09:01:00", "cliente_id": 55,
        "origem": "00000654321", "destino": "102", "status": "Não atendida", "direcao": "entrada",
    },
    {
        "uniqueid": "123.2", "calldate": "2026-11-05 09:02:00", "cliente_id": 55,
        "origem": "101", "destino": "00000999999", "status": "Atendida", "direcao": "saida",
    },
]


def response(status, data=None):
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = json.dumps(data).encode()
    return result


def pagination(rows, page, size=1):
    return {
        "current_page": page, "last_page": max(1, (len(rows) + size - 1) // size),
        "total": len(rows), "data": rows[(page - 1) * size:page * size],
        "next_page_url": "https://other.example.invalid/never-follow",
    }


class Provider:
    def __init__(self):
        self.calls = []
        self.cdr = CDR
        self.directory = EXTENSIONS
        self.failure = None
        self.live_total = 1
        self.live_filters = {"accountcode": "demo"}
        self.live_states = {"PJSIP/demo-101": "ringing", "demo-102": {"state": "free"}}
        self.extensions_in_call = []
        self.collected = threading.Event()

    def __call__(self, method, url, **args):
        self.calls.append((method, url, args))
        resource = url.rsplit("/", 1)[1]
        if resource == "login":
            return response(200, {"access_token": "fake.token", "expires_in": 300})
        if self.failure:
            return response(self.failure, {})
        params = args["params"]
        if resource == "search":
            return response(200, {"resultado": pagination(self.cdr, params["page"])})
        if params["accountcode"] != PROFILE.account:
            return response(403)
        if resource == "ramais":
            return response(200, {"ramais": pagination(self.directory, params["page"])})
        if resource == "filas":
            return response(200, pagination([QUEUE], params["page"]))
        if resource == "dashboard-extension":
            return response(200, {
                "filters": self.live_filters, "summary": {"totalCalls": self.live_total},
                "extensions": {"total": 2, "items": EXTENSIONS},
                "statusMap": self.live_states, "extensionsInCall": self.extensions_in_call,
            })
        if resource == "dashboard-queue":
            self.collected.set()
            return response(200, {"status": True, "queues": [QUEUE]})
        return response(404)


class ClientTests(unittest.TestCase):
    def test_http_origin_must_be_explicitly_allowed(self):
        self.assertEqual(pabx_origin("https://PABX.example.invalid:443/"), PROFILE.url)
        allowed = "http://pabx.example.invalid:8080"
        self.assertEqual(pabx_origin(allowed + "/", allowed), allowed)
        for value in (allowed, "http://pabx.example.invalid", "http://other.example.invalid:8080"):
            with self.assertRaisesRegex(PABXError, "http_origin"):
                pabx_origin(value, "" if value == allowed else allowed)
        for value in ("https://x:y@example.invalid", "https://pabx.example.invalid/api"):
            with self.assertRaisesRegex(PABXError, "connection_fields"):
                pabx_origin(value)

    def test_reuse_renew_and_preserve_tls_settings(self):
        calls = []

        def transport(method, url, **args):
            calls.append((url, args))
            if url.endswith("login"):
                return response(200, {"token": f"token-{len(calls)}"})
            return response(401 if len(calls) == 3 else 200, {"ok": True})

        client = PABXClient(CONFIG, transport)
        self.assertTrue(client.query("GET", "ramais")["ok"])
        self.assertTrue(client.query("GET", "filas")["ok"])
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[2][1]["headers"]["Authorization"], "Bearer token-1")
        self.assertEqual(calls[4][1]["headers"]["Authorization"], "Bearer token-4")
        self.assertTrue(all(args["verify"] and not args["allow_redirects"] for _, args in calls))

    def test_advertised_expiry_and_concurrent_login(self):
        now = 1000
        calls = []

        def transport(method, url, **args):
            if url.endswith("login"):
                calls.append(url)
                claim = base64.urlsafe_b64encode(json.dumps({"exp": now + 100}).encode()).decode().strip("=")
                return response(200, {"token": f"a.{claim}.b"})
            return response(200, {})

        client = PABXClient(CONFIG, transport)
        with patch("pabx.time.time", side_effect=lambda: now):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: client.query("GET", "ramais"), range(8)))
            self.assertEqual(len(calls), 1)
            now += 95
            client.query("GET", "ramais")
            self.assertEqual(len(calls), 2)

    def test_invalid_credentials_cooldown_and_double_rejection(self):
        def login_failure(method, url, **args):
            calls.append(url)
            return response(401)

        calls = []
        client = PABXClient(CONFIG, login_failure)
        for _ in range(2):
            with self.assertRaisesRegex(PABXError, "credentials"):
                client.query("GET", "ramais")
        self.assertEqual(len(calls), 1)

        def double_rejection(method, url, **args):
            calls.append(url)
            return response(200, {"token": "reissued"}) if url.endswith("login") else response(401)

        calls = []
        client = PABXClient(CONFIG, double_rejection)
        for _ in range(2):
            with self.assertRaisesRegex(PABXError, "token_rejected"):
                client.query("GET", "ramais")
        self.assertEqual(len(calls), 4)

    def test_malformed_token_never_becomes_an_authorization_header(self):
        for invalid in ("token\x00", "token\x7f", "token☃", "token com espaço"):
            with self.subTest(invalid=invalid):
                transport = Mock(return_value=response(200, {"token": invalid}))
                client = PABXClient(CONFIG, transport)
                with self.assertRaisesRegex(PABXError, "upstream_response"):
                    client.query("GET", "ramais")
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(transport.call_args.args[0], "POST")

    def test_unavailable_forbidden_and_invalid_json(self):
        for status, error in [(403, "forbidden"), (503, "unavailable"), (302, "upstream_response")]:
            provider = Provider()
            provider.failure = status
            with self.assertRaisesRegex(PABXError, error):
                PABXClient(CONFIG, provider).query("GET", "ramais", params={"accountcode": "demo"})
        for exception, error in [(requests.Timeout(), "timeout"), (requests.exceptions.SSLError(), "tls")]:
            with patch("pabx.requests.request", side_effect=exception) as transport:
                with self.assertRaisesRegex(PABXError, error):
                    PABXClient(CONFIG, transport).query("GET", "ramais")
        with self.assertRaisesRegex(PABXError, "upstream_response"):
            PABXClient(CONFIG, lambda *args, **values: response(200, ["wrong"])).query("GET", "ramais")

    def test_pagination_empty_and_incomplete(self):
        self.assertEqual(pages(lambda page: pagination([], page), limit=2), [])
        with self.assertRaisesRegex(PABXError, "page_limit"):
            pages(lambda page: pagination(CDR, page), limit=2)
        with self.assertRaisesRegex(PABXError, "upstream_response"):
            pages(lambda _: pagination(CDR, 1), limit=5)


class MonitorTests(unittest.TestCase):
    def test_recording_filename_uses_cdr_time_and_only_registered_extension_identifiers(self):
        scoped = {"demo-101": "101", "demo-102": "102", "101": "101", "102": "102"}
        outbound = {
            "calldate": "2026-11-05", "start": "2026-11-05T21:00:01-03:00", "direcao": "saida",
            "canal": "SIP/demo-102-000000ff", "destino": "101",
        }
        self.assertEqual(recording_filename(outbound, scoped), "gravacao-2026-11-05_21-00-01_ramal-102")
        unknown = {"calldate": "inválida", "origem": "00000123456", "destino": "00000654321"}
        self.assertEqual(recording_filename(unknown, scoped), "gravacao-data-hora-nao-informadas_ramal-nao-identificado")

    def test_http_collection_reuses_login_and_reads_ramais_filas_and_dashboards(self):
        provider = Provider()
        client = PABXClient(replace(CONFIG, url=HTTP_ORIGIN, cdr_customer=""), provider)
        monitor = PABXMonitor(client)
        monitor._collect()
        monitor._collect()
        indicators = monitor.snapshot()["data"]["indicators"]
        self.assertEqual(indicators["total"], 2)
        self.assertEqual(indicators["queues"], 1)
        self.assertEqual(indicators["available"], 1)
        self.assertEqual(indicators["ringing"], 1)
        self.assertEqual(indicators["busy"], 0)
        self.assertTrue(monitor.snapshot()["data"]["queues"][0]["monitored"])
        with self.assertRaisesRegex(PABXError, "cdr_disabled"):
            monitor.history(date(2026, 11, 1), date(2026, 11, 7))
        for method, resource in (("POST", "filas"), ("GET", "relatorio-cdr/search"), ("POST", "relatorio-cdr/search")):
            with self.assertRaisesRegex(PABXError, "http_read_only"):
                client.query(method, resource)
        self.assertEqual(
            [(method, url.rsplit("/", 1)[1]) for method, url, _ in provider.calls],
            [("POST", "login")] + [
                ("GET", "ramais"), ("GET", "ramais"), ("GET", "filas"),
                ("GET", "dashboard-extension"), ("GET", "dashboard-queue"),
            ] * 2,
        )
        self.assertTrue(all(not args["allow_redirects"] for _, _, args in provider.calls))

    def test_extension_state_uses_provider_status_over_registration(self):
        for reported, expected in (("NOT_INUSE", "available"), ("INUSE", "busy"), ("RINGING", "ringing")):
            with self.subTest(reported=reported):
                self.assertEqual(extension_state({"registered": True}, reported), (expected, True))

    def test_calls_are_scoped_and_busy_is_not_call_membership(self):
        provider = Provider()
        provider.live_total = 2
        provider.live_states = {"demo-101": "INUSE", "demo-102": "BUSY"}
        provider.extensions_in_call = [
            "PJSIP/demo-101",
            {"ramal": "101", "accountcode": "demo"},
            {"ramal": "102", "accountcode": "outside"},
            "unknown-provider-id",
        ]
        monitor = PABXMonitor(PABXClient(replace(CONFIG, url=HTTP_ORIGIN), provider))
        monitor._collect()
        data = monitor.snapshot()["data"]
        self.assertEqual(data["indicators"]["active_calls"], 2)
        self.assertEqual(data["indicators"]["extensions_in_call"], 1)
        self.assertEqual(data["indicators"]["busy"], 2)
        self.assertEqual([row["in_call"] for row in data["extensions"]], [True, False])
        self.assertTrue(data["queues"][0]["members"][0]["in_call"])

        provider.extensions_in_call = ["unknown-provider-id"]
        monitor._collect()
        data = monitor.snapshot()["data"]
        self.assertEqual(data["indicators"]["active_calls"], 2)
        self.assertIsNone(data["indicators"]["extensions_in_call"])

        provider.live_filters = {}
        monitor._collect()
        data = monitor.snapshot()["data"]
        self.assertIsNone(data["indicators"]["active_calls"])
        self.assertIsNone(data["indicators"]["extensions_in_call"])

    def test_operational_status_scope_and_last_valid_collection(self):
        provider = Provider()
        monitor = PABXMonitor(PABXClient(CONFIG, provider))
        self.assertIsNone(monitor.snapshot()["data"])
        monitor._collect()
        initial = monitor.snapshot()
        self.assertEqual(initial["data"]["indicators"]["ringing"], 1)
        self.assertEqual(initial["data"]["indicators"]["active_calls"], 1)
        self.assertEqual(initial["data"]["extensions"][0]["queues"][0]["paused"], True)
        self.assertNotIn("excluded", json.dumps(initial))
        provider.failure = 503
        monitor._collect()
        failed = monitor.snapshot()
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["last_success"], initial["last_success"])
        self.assertEqual(failed["data"], initial["data"])
        provider.failure = None
        provider.directory = [{**EXTENSIONS[0], "accountcode": "other"}]
        monitor._collect()
        self.assertEqual(monitor.snapshot()["state"], "scope_mismatch")
        self.assertIsNone(monitor.snapshot()["data"])

    def test_cdr_preserves_duplicate_rows_and_scope(self):
        provider = Provider()
        monitor = PABXMonitor(PABXClient(CONFIG, provider))
        rows = monitor.history(date(2026, 11, 1), date(2026, 11, 7))
        self.assertEqual([row["id_occurrences"] for row in rows], [2, 2, 1])
        self.assertEqual([row["recording"] for row in rows], [True, False, False])
        self.assertNotIn("00000123456", json.dumps(rows))
        self.assertNotIn("private.wav", json.dumps(rows))
        posts = [call for call in provider.calls if call[0] == "POST" and call[1].endswith("search")]
        self.assertEqual([call[2]["params"]["page"] for call in posts], [1, 2, 3])
        self.assertNotIn("accountcode", posts[0][2]["json"])
        provider.cdr = [{**CDR[0], "cliente_id": 99}]
        with self.assertRaisesRegex(PABXError, "scope_mismatch"):
            monitor.history(date(2026, 11, 2), date(2026, 11, 7))
        provider.cdr = []
        self.assertEqual(monitor.history(date(2026, 11, 2), date(2026, 11, 7)), [])

    def test_permission_loss_invalidates_cached_cdr_and_recordings(self):
        provider = Provider()
        media = f"{PROFILE.url}/recordings/fixture.mp3"
        provider.cdr = [{**CDR[0], "arquivo_audio": media}]
        monitor = PABXMonitor(PABXClient(CONFIG, provider))
        period = (date(2026, 11, 1), date(2026, 11, 7))
        recording_id = monitor.history(*period)[0]["recording_id"]
        self.assertEqual(monitor.recording_url(recording_id), media)
        provider.failure = 403
        monitor._collect()
        self.assertFalse(monitor.accepts_recordings())
        with self.assertRaisesRegex(PABXError, "recording_missing"):
            monitor.recording_url(recording_id)
        with self.assertRaisesRegex(PABXError, "forbidden"):
            monitor.history(*period)
        provider.failure = None
        monitor._collect()
        with self.assertRaisesRegex(PABXError, "recording_missing"):
            monitor.recording_url(recording_id)
        self.assertEqual(monitor.history(*period)[0]["recording_id"], recording_id)

    def test_encrypted_profiles_and_start_without_a_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "demo.sqlite3"
            secret = Fernet.generate_key()
            store = PABXStore(path, secret)
            store.save(1, PROFILE, 0)
            store.save(2, replace(PROFILE, company="Outra empresa"), 0)
            with closing(sqlite3.connect(path)) as db:
                blob = db.execute("SELECT payload FROM pabx_configs WHERE id=1").fetchone()[0]
            self.assertNotIn(PROFILE.email.encode(), blob)
            self.assertNotIn(PROFILE.password.encode(), blob)
            self.assertEqual(store.load(2)[0].company, "Outra empresa")
            with self.assertRaisesRegex(PABXError, "storage_failure"):
                PABXStore(path, Fernet.generate_key()).load(1)
            provider = Provider()
            service = PABXService(store, client_factory=lambda config: PABXClient(config, provider))
            try:
                self.assertTrue(provider.collected.wait(2))
                service.save(replace(PROFILE, enabled=False), 1)
                self.assertIsNone(service.current()[2])
                self.assertFalse(PABXStore(path, secret).load(1)[0].enabled)
            finally:
                if monitor := service.current()[2]:
                    monitor.stop().join(2)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.provider = Provider()
        self.service = PABXService(
            PABXStore(Path(self.folder.name) / "routes.sqlite3", Fernet.generate_key()),
            client_factory=lambda config: PABXClient(config, self.provider),
            local_http_origin=HTTP_ORIGIN,
        )
        self.addCleanup(self.stop)
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        app.secret_key = "test-secret-for-local-exercise-with-entropy"
        for endpoint in ("login", "dashboard", "logout", "whatsapp_tv"):
            app.add_url_rule(f"/{endpoint}", endpoint, lambda: "")
        with patch.object(PABXService, "from_env", return_value=self.service):
            register_pabx(app, lambda: session.get("authenticated") is True, "admin", "test-local-password")
        self.client = app.test_client()

    def stop(self):
        if monitor := self.service.current()[2]:
            monitor.stop().join(2)

    def sign_in(self, username="admin"):
        with self.client.session_transaction() as state:
            state.update(authenticated=True, username=username, pabx_csrf="a" * 64)

    def test_pabx_tv_requires_admin_and_renders_collected_names_without_javascript(self):
        path = "/tv/pabx"
        self.assertEqual(self.client.get(path).location, "/login?tv=pabx")
        self.sign_in("viewer")
        self.assertEqual(self.client.get(path).location, "/dashboard")
        self.sign_in()
        monitor = PABXMonitor(PABXClient(CONFIG, self.provider))
        monitor._collect()
        with patch.object(self.service, "current", return_value=(PROFILE, 1, monitor)):
            result = self.client.get(path)
            free = self.client.get(f"{path}?status=available")
            charts = self.client.get(f"{path}?visao=indicadores")
            queues = self.client.get(f"{path}?visao=filas")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertIn(b'http-equiv="refresh" content="15"', result.data)
        self.assertIn(b"Ana Teste", result.data)
        self.assertIn(b"Suporte teste", result.data)
        self.assertNotIn(b"<script", result.data)
        self.assertNotIn(PROFILE.password.encode(), result.data)
        self.assertIn(b"Bruno Teste", free.data)
        self.assertNotIn(b"Ana Teste", free.data)
        self.assertIn(b"Estado dos ramais", charts.data)
        self.assertIn(b"Registro dos ramais", charts.data)
        self.assertIn(b"Ramal 102", queues.data)
        self.assertIn(b"Bruno Teste", queues.data)

    def test_tv_renders_all_40_extensions_even_with_an_old_page_parameter(self):
        self.sign_in()
        self.provider.directory = EXTENSIONS + [
            {"name": f"demo-{ext}", "accountcode": "demo", "alias": str(ext),
             "alias_name": f"{ext} - Ramal Teste {ext}"}
            for ext in range(201, 239)
        ]
        monitor = PABXMonitor(PABXClient(CONFIG, self.provider))
        monitor._collect()
        with patch.object(self.service, "current", return_value=(PROFILE, 1, monitor)):
            result = self.client.get("/tv/pabx?pagina=4")
            filtered = self.client.get("/tv/pabx?busca=Ramal+Teste+238")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data.count(b'class="tv-extension '), 40)
        self.assertNotIn(b'class="tv-pager"', result.data)
        self.assertIn(b"Ramal Teste 238", result.data)
        self.assertEqual(filtered.data.count(b'class="tv-extension '), 1)
        self.assertIn(b"Ramal Teste 238", filtered.data)

    def test_tv_history_is_opt_in_and_reuses_cdr_permissions_and_downloads(self):
        self.sign_in()
        self.provider.cdr = [{**CDR[0], "arquivo_audio": f"{PROFILE.url}/recordings/private.wav"}]
        monitor = PABXMonitor(PABXClient(CONFIG, self.provider))
        source = "/tv/pabx/historico"
        filters = {**DATES, "recording_only": "1", "csrf": "a" * 64}
        with patch.object(self.service, "current", return_value=(PROFILE, 1, monitor)):
            landing = self.client.get(source)
            self.assertEqual(landing.status_code, 200)
            self.assertNotIn(b'http-equiv="refresh"', landing.data)
            self.assertEqual(self.client.post(source, data={**filters, "csrf": "invalid"}).status_code, 403)
            self.assertEqual(self.provider.calls, [])
            with patch("pabx_routes.requests.Session") as media:
                report = self.client.post(source, data=filters)
                self.assertEqual(report.status_code, 200)
                self.assertIn(b"1 registro CDR", report.data)
                self.assertIn(b"Registros por situa", report.data)
                self.assertIn(b"Baixar grava", report.data)
                self.assertIn(b"name=\"recording_id\"", report.data)
                self.assertNotIn(b"private.wav", report.data)
                exported = self.client.post(source, data={**filters, "action": "export"})
                self.assertEqual(exported.mimetype, "text/csv")
                self.assertEqual(len(exported.get_data(as_text=True).splitlines()), 2)
                media.assert_not_called()

    def test_dokploy_http_profile_is_fixed_for_remote_admin(self):
        fixed = PABXProfile(company="JRC", url=HTTP_ORIGIN, account="demo", interval=15, cdr_customer="55")
        self.service.fixed_http_profile = fixed
        self.sign_in()
        path = "/monitoramento-pabx/configuracao"
        remote = {"REMOTE_ADDR": "192.0.2.1"}
        page = self.client.get(path, environ_overrides=remote)
        self.assertIn(b'name="company" value="JRC" readonly', page.data)
        self.assertIn(b'name="account" value="demo" readonly', page.data)
        self.assertIn(b'name="cdr_customer" value="55" readonly', page.data)
        self.assertIn(f'value="{HTTP_ORIGIN}" readonly'.encode(), page.data)

        form = {"action": "test", "csrf": "a" * 64, "revision": "0", "email": PROFILE.email, "password": PROFILE.password}
        for alternate in ("http://other.example.invalid:8080", HTTP_ORIGIN + "/path", PROFILE.url):
            rejected = self.client.post(path, data={**form, "url": alternate}, environ_overrides=remote)
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(rejected.json["error"], "http_origin")
        for field, new_value in (("account", "other"), ("cdr_customer", "99"), ("interval", "5")):
            rejected = self.client.post(path, data={**form, field: new_value}, environ_overrides=remote)
            self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.provider.calls, [])

        self.assertEqual(self.client.post(path, data=form, environ_overrides=remote).status_code, 200)
        calls = [(method, url.split(HTTP_ORIGIN, 1)[1]) for method, url, _ in self.provider.calls]
        self.assertEqual(calls, [("POST", "/api/v1/auth/login")] + [
            ("GET", "/api/v1/admin/ramais"), ("GET", "/api/v1/admin/ramais"),
            ("GET", "/api/v1/admin/filas"), ("GET", "/api/v1/admin/dashboard-extension"),
            ("GET", "/api/v1/admin/dashboard-queue"),
        ])
        saved = self.client.post(path, data={**form, "action": "save"}, environ_overrides=remote)
        self.assertEqual(saved.status_code, 303)
        profile, revision, monitor = self.service.current()
        self.assertEqual(profile.fixed_fields(), fixed.fixed_fields())
        self.assertFalse(profile.scope_confirmed)
        self.assertFalse(profile.cdr_confirmed)
        self.assertEqual(revision, 1)
        self.assertIsNone(monitor)

    def test_dokploy_http_requires_its_own_environment_option(self):
        variables = {
            "SERVER_SOFTWARE": "gunicorn/27.0.1", "PABX_CONFIG_KEY": Fernet.generate_key().decode(),
            "PABX_CONFIG_DB": str(Path(self.folder.name) / "dokploy.sqlite3"),
            "PABX_LOCAL_HTTP_ORIGIN": HTTP_ORIGIN,
        }
        with patch.dict("os.environ", variables, clear=True):
            default = PABXService.from_env()
            with closing(default._ownership):
                self.assertFalse(default.transport_allowed(replace(PROFILE, url=HTTP_ORIGIN)))
            with patch.dict("os.environ", {"PABX_DOKPLOY_HTTP_ORIGIN": HTTP_ORIGIN}):
                with self.assertRaisesRegex(PABXError, "storage_setup"):
                    PABXService.from_env()
            with patch.dict("os.environ", {
                "PABX_DOKPLOY_HTTP_ORIGIN": HTTP_ORIGIN,
                "PABX_DOKPLOY_ACCOUNTCODE": "demo", "PABX_DOKPLOY_CDR_CUSTOMER": "55",
            }):
                restricted = PABXService.from_env()
                with closing(restricted._ownership):
                    profile = restricted.fixed_http_profile
                    self.assertTrue(restricted.transport_allowed(profile))
                    self.assertFalse(restricted.transport_allowed(replace(profile, cdr_customer="99")))
                    self.assertFalse(restricted.transport_allowed(replace(profile, url=PROFILE.url)))

    def test_admin_test_save_and_cdr_export_permissions(self):
        self.assertEqual(self.client.get("/monitoramento-pabx/configuracao").status_code, 302)
        self.assertEqual(self.client.get("/api/pabx/status").status_code, 401)
        self.assertEqual(self.client.post("/api/pabx/cdr/export", json=DATES).status_code, 401)
        self.sign_in("viewer")
        self.assertEqual(self.client.post("/api/pabx/cdr", json=DATES).status_code, 403)
        self.assertEqual(self.client.get("/monitoramento-pabx/configuracao").status_code, 302)
        self.sign_in()
        path = "/monitoramento-pabx/configuracao"
        form = {
            "csrf": "a" * 64, "revision": "0", "company": PROFILE.company, "url": PROFILE.url,
            "email": PROFILE.email, "password": PROFILE.password, "account": PROFILE.account,
            "interval": "5", "enabled": "1", "scope_confirmed": "1", "cdr_confirmed": "1", "cdr_customer": "55",
        }
        self.assertEqual(self.client.post(path, data={**form, "action": "save", "csrf": "invalid"}).status_code, 403)
        self.assertEqual(self.client.post(path, data={**form, "action": "test"}).status_code, 200)
        self.assertEqual(self.service.current()[1], 0)
        self.assertEqual(self.client.post(path, data={**form, "action": "save"}).status_code, 303)
        self.assertEqual(self.client.post(path, data={**form, "action": "save"}).status_code, 409)
        self.assertNotIn(PROFILE.password.encode(), self.client.get(path).data)
        self.assertNotIn(PROFILE.email.encode(), self.client.get(path).data)
        self.assertEqual(self.client.get("/monitoramento-pabx").status_code, 200)
        self.assertEqual(self.client.post("/api/pabx/cdr", json={"start": "wrong"}).status_code, 400)
        filtered = {**DATES, "status": "atendida", "direction": "entrada"}
        history = self.client.post("/api/pabx/cdr", json=filtered)
        self.assertEqual(len(history.json["records"]), 1)
        self.assertEqual(history.json["records"][0]["id_occurrences"], 2)
        self.assertEqual(history.headers["Cache-Control"], "no-store")
        export = self.client.post("/api/pabx/cdr/export", json=filtered)
        self.assertEqual(export.mimetype, "text/csv")
        self.assertEqual(len(export.get_data(as_text=True).splitlines()), 2)
        self.assertNotIn("private.wav", export.get_data(as_text=True))

    def test_new_host_requires_new_credentials_and_cdr_remains_gated(self):
        self.assertEqual(0, self.service.current()[1])
        self.service.save(replace(PROFILE, cdr_confirmed=False), 0)
        self.sign_in()
        self.assertEqual(self.client.post("/api/pabx/cdr", json=DATES).json["error"], "cdr_disabled")
        with self.assertRaisesRegex(PABXError, "connection_fields"):
            PABXProfile.from_form(
                {"url": "https://other.example.invalid", "company": "Demo", "account": "demo", "interval": "30"},
                PROFILE,
            )

    def test_recording_filter_and_csv_do_not_request_audio(self):
        self.service.save(PROFILE, 0)
        self.sign_in()
        dates_and_status = {**DATES, "status": "atendida"}
        with patch("pabx_routes.requests.Session") as media:
            regular = self.client.post("/api/pabx/cdr", json=dates_and_status)
            self.assertEqual(len(regular.json["records"]), 2)
            filtered = {**dates_and_status, "recording_only": True}
            history = self.client.post("/api/pabx/cdr", json=filtered)
            self.assertEqual(len(history.json["records"]), 1)
            self.assertTrue(history.json["records"][0]["recording"])
            self.assertEqual(history.json["records"][0]["id_occurrences"], 2)
            exported = self.client.post("/api/pabx/cdr/export", json=filtered)
            records = exported.get_data(as_text=True).splitlines()
            self.assertEqual(len(records), 2)
            self.assertTrue(records[1].endswith(";Sim"))
            invalid = self.client.post("/api/pabx/cdr", json={**filtered, "recording_only": "true"})
            self.assertEqual(invalid.status_code, 400)
            media.assert_not_called()

    def test_recording_zip_requires_click_selection_and_keeps_audio_scoped(self):
        self.sign_in()
        first_url, second_url = (
            f"{PROFILE.url}/recordings/{name}.mp3" for name in ("first", "second")
        )
        inbound = {**CDR[0], "destino": "00000123456", "canal2": "PJSIP/demo-101-0000000a"}
        self.provider.cdr = [{**inbound, "arquivo_audio": first_url}, {**inbound, "arquivo_audio": second_url}]
        monitor = PABXMonitor(PABXClient(CONFIG, self.provider))
        monitor._collect()
        filename = "gravacao-2026-11-05_09-00-00_ramal-101.mp3"
        second_filename = "gravacao-2026-11-05_09-00-00_ramal-101_2.mp3"
        zip_url = "/api/pabx/recordings/zip"
        with patch.object(self.service, "current", return_value=(PROFILE, 1, monitor)), patch(
            "pabx_routes.requests.Session",
        ) as media:
            page = self.client.get("/monitoramento-pabx")
            self.assertIn(b"pabx-recordings-zip", page.data)
            history = self.client.post("/api/pabx/cdr", json=DATES)
            ids = [row["recording_id"] for row in history.json["records"]]
            self.assertTrue(all(ids))
            self.assertEqual(self.client.post(zip_url, data={"recording_id": ids}).status_code, 403)
            form = {"csrf": "a" * 64, "recording_id": ids + [ids[0]]}
            foreign = self.client.post(zip_url, data={**form, "recording_id": ids + ["0" * 64]})
            self.assertEqual(foreign.status_code, 404)
            media.assert_not_called()

            upstream = media.return_value.__enter__.return_value.request

            def audio_response(method, url, **kwargs):
                self.assertEqual(method, "GET")
                self.assertNotIn("Authorization", kwargs["headers"])
                self.assertFalse(kwargs["allow_redirects"])
                self.assertTrue(kwargs["stream"])
                reply = requests.Response()
                reply.status_code = 200
                reply.headers["Content-Type"] = "audio/mpeg"
                reply._content = b"primeiro" if url == first_url else b"segundo"
                reply._content_consumed = True
                return reply

            upstream.side_effect = audio_response
            archive = self.client.post(zip_url, data=form, buffered=True)
            self.assertEqual(archive.status_code, 200)
            self.assertEqual(archive.mimetype, "application/zip")
            self.assertEqual(archive.headers["Content-Disposition"], 'attachment; filename="gravacoes-pabx.zip"')
            self.assertEqual(archive.headers["Cache-Control"], "no-store")
            with ZipFile(io.BytesIO(archive.data)) as tracks:
                self.assertEqual(tracks.namelist(), [filename, second_filename])
                self.assertEqual(tracks.read(filename), b"primeiro")
                self.assertEqual(tracks.read(second_filename), b"segundo")
            self.assertEqual([call.args[1] for call in upstream.call_args_list], [first_url, second_url])
            archive.close()

            missing = response(404)
            missing._content_consumed = True
            upstream.side_effect = [
                audio_response("GET", first_url, headers={}, allow_redirects=False, stream=True),
                missing,
            ]
            partial = self.client.post(zip_url, data=form, buffered=True)
            with ZipFile(io.BytesIO(partial.data)) as tracks:
                self.assertEqual(tracks.namelist(), [filename, "resultado.txt"])
                self.assertIn("A gravação 2 não pôde", tracks.read("resultado.txt").decode())
            partial.close()

            upstream.reset_mock()
            upstream.side_effect = audio_response
            too_many = self.client.post(zip_url, data={**form, "recording_id": [ids[0]] * 1001})
            self.assertEqual(too_many.json["error"], "recording_selection")
            upstream.assert_not_called()
            with patch("pabx_routes.MAX_ZIP_AUDIO_BYTES", 8):
                with self.client.post(zip_url, data=form, buffered=True) as limited:
                    with ZipFile(io.BytesIO(limited.data)) as tracks:
                        self.assertEqual(tracks.namelist(), [filename, "resultado.txt"])
                        self.assertEqual(tracks.read(filename), b"primeiro")
                        self.assertIn("Limite de tamanho", tracks.read("resultado.txt").decode())

            upstream.reset_mock()
            with self.client.post(zip_url, data=form, buffered=False) as open_zip:
                self.assertEqual(open_zip.status_code, 200)
                self.assertEqual(self.client.post(zip_url, data=form).json["error"], "recording_busy")
                media_url = f"/api/pabx/recordings/{ids[0]}"
                with self.client.get(media_url, buffered=False) as open_mp3:
                    self.assertEqual(open_mp3.status_code, 200)
                    self.assertEqual(self.client.get(media_url).json["error"], "recording_busy")
                self.assertEqual(upstream.call_count, 2)
            with self.client.post(zip_url, data=form, buffered=True) as retried:
                self.assertEqual(retried.status_code, 200)

    def test_zip_stops_unknown_length_audio_at_the_size_limit(self):
        from types import SimpleNamespace
        from flask import Response
        from pabx_routes import recording_archive

        recordings = [SimpleNamespace(url=PROFILE.url, filename_base="first"),
                      SimpleNamespace(url=PROFILE.url, filename_base="second")]
        response = Response(iter((b"123", b"456", b"789")), content_type="audio/mpeg")
        with patch("pabx_routes.stream_recording", return_value=response) as audio, patch("pabx_routes.MAX_ZIP_AUDIO_BYTES", 5):
            with ZipFile(io.BytesIO(b"".join(recording_archive(recordings, CONFIG, lambda: True)))) as tracks:
                self.assertIsNone(tracks.testzip())
                self.assertEqual(tracks.namelist(), ["first.mp3", "resultado.txt"])
                self.assertEqual(tracks.read("first.mp3"), b"123")
                self.assertIn("first.mp3 ficou incompleto", tracks.read("resultado.txt").decode())
            audio.assert_called_once()

    def test_customer_change_requires_scope_confirmation_again(self):
        self.service.save(PROFILE, 0)
        self.sign_in()
        saved = self.client.post("/monitoramento-pabx/configuracao", data={
            "action": "save", "csrf": "a" * 64, "revision": "1", "company": "Outro cliente",
            "url": PROFILE.url, "account": "other", "interval": "5", "enabled": "1",
            "scope_confirmed": "1", "cdr_confirmed": "1", "cdr_customer": "88",
        })
        self.assertEqual(saved.status_code, 303)
        profile, _, monitor = self.service.current()
        self.assertEqual(profile.password, PROFILE.password)
        self.assertFalse(profile.scope_confirmed)
        self.assertFalse(profile.cdr_confirmed)
        self.assertIsNone(monitor)
        self.assertEqual(self.client.get("/api/pabx/status").json["state"], "scope_pending")

    def test_http_cdr_requires_confirmation_and_streams_audio_only_on_request(self):
        self.sign_in()
        path = "/monitoramento-pabx/configuracao"
        values = {
            "action": "save", "csrf": "a" * 64, "revision": "0", "company": PROFILE.company,
            "url": HTTP_ORIGIN, "email": PROFILE.email, "password": PROFILE.password,
            "account": PROFILE.account, "interval": "5", "enabled": "1", "scope_confirmed": "1",
            "cdr_customer": "55",
        }
        self.assertEqual(
            self.client.post(path, data=values, environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code, 400,
        )
        self.assertEqual(self.client.post(path, data={**values, "action": "test"}).status_code, 200)
        self.assertEqual(self.client.post(path, data=values).status_code, 303)
        profile, _, _ = self.service.current()
        self.assertFalse(profile.cdr_confirmed)
        self.assertIn(b"Coleta por HTTP", self.client.get("/monitoramento-pabx").data)
        self.assertEqual(self.client.post("/api/pabx/cdr", json=DATES).json["error"], "cdr_disabled")
        self.assertIsNone(PABXService(self.service.store).current()[2])

        local_audio = HTTP_ORIGIN + "/recordings/example.mp3"
        wav_audio = HTTP_ORIGIN + "/recordings/gsm-example.wav"
        self.provider.cdr = [
            {**CDR[0], "arquivo_audio": local_audio},
            {**CDR[1], "arquivo_audio": "https://other.example.invalid/private.m4a"},
            {**CDR[2], "arquivo_audio": HTTP_ORIGIN + "/recordings/%2e%2e/private.wav"},
            {**CDR[0], "arquivo_audio": wav_audio},
        ]
        saved = self.client.post(path, data={**values, "revision": "1", "email": "", "password": "", "cdr_confirmed": "1"})
        self.assertEqual(saved.status_code, 303)
        self.assertIn(b"pabx-history-form", self.client.get("/monitoramento-pabx").data)

        with patch("pabx_routes.requests.Session") as media:
            report = self.client.post("/api/pabx/cdr", json=DATES)
            self.assertEqual(report.status_code, 200)
            self.assertNotIn(local_audio, report.get_data(as_text=True))
            rows = report.json["records"]
            recording_id = rows[0]["recording_id"]
            self.assertTrue(recording_id)
            self.assertIsNone(rows[1]["recording_id"])
            self.assertIsNone(rows[2]["recording_id"])
            media.assert_not_called()
            self.assertEqual(self.client.get(f"/api/pabx/recordings/{'0' * 64}").status_code, 404)
            media.assert_not_called()

            upstream = requests.Response()
            upstream.status_code = 206
            upstream.headers.update({
                "Content-Type": "audio/mpeg", "Accept-Ranges": "bytes",
                "Content-Length": "2", "Content-Range": "bytes 0-1/10",
            })
            upstream._content = b"RI"
            upstream._content_consumed = True
            session = media.return_value.__enter__.return_value
            session.request.return_value = upstream
            audio = self.client.get(f"/api/pabx/recordings/{recording_id}", headers={"Range": "bytes=0-1"}, buffered=True)
            self.assertEqual(audio.status_code, 206)
            self.assertEqual(audio.get_data(), b"RI")
            self.assertEqual(audio.headers["Content-Range"], "bytes 0-1/10")
            self.assertNotIn("Content-Disposition", audio.headers)
            self.assertEqual(session.request.call_args.args, ("GET", local_audio))
            self.assertEqual(session.request.call_args.kwargs["headers"]["Range"], "bytes=0-1")
            self.assertNotIn("Authorization", session.request.call_args.kwargs["headers"])
            self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
            audio.close()

            upstream = requests.Response()
            upstream.status_code = 200
            upstream.headers.update({"Content-Type": "audio/x-wav", "Content-Length": "1040"})
            upstream._content_consumed = True
            session.request.return_value = upstream
            wav_id = rows[3]["recording_id"]
            self.assertNotIn(wav_audio, report.get_data(as_text=True))
            with patch("pabx_routes.transcode_wav", side_effect=lambda *_: iter([b"ID3"])) as convert:
                head = self.client.head(f"/api/pabx/recordings/{wav_id}")
                self.assertEqual(head.mimetype, "audio/mpeg")
                self.assertEqual(head.get_data(), b"")
                convert.assert_not_called()
                head.close()
                audio = self.client.get(f"/api/pabx/recordings/{wav_id}", headers={"Range": "bytes=0-"}, buffered=True)
                self.assertEqual(audio.status_code, 200)
                self.assertEqual(audio.mimetype, "audio/mpeg")
                self.assertEqual(audio.get_data(), b"ID3")
                self.assertNotIn("Content-Range", audio.headers)
                self.assertNotIn("Range", session.request.call_args.kwargs["headers"])
                convert.assert_called_once()
                audio.close()

                download = self.client.get(f"/api/pabx/recordings/{wav_id}?download=1", buffered=True)
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.mimetype, "audio/mpeg")
                self.assertEqual(download.headers["Content-Disposition"], "attachment; filename=gravacao-pabx.mp3")
                self.assertEqual(download.headers["Cache-Control"], "no-store")
                self.assertEqual(download.get_data(), b"ID3")
                self.assertEqual(session.request.call_args.args, ("GET", wav_audio))
                download.close()


class WhatsAppTests(unittest.TestCase):
    def test_whatsapp_tv_keeps_login_and_renders_status_without_javascript(self):
        import app as module

        client = module.app.test_client()
        data = {
            "summary": {"total": 1, "connected": 0, "connecting": 0, "disconnected": 1,
                        "unknown": 0, "apis_unavailable": 0},
            "generated_at": 1795608000, "sources": [{"name": "API teste", "online": True}],
            "instances": [{"client": "Cliente teste", "status": "disconnected", "status_label": "Desconectado",
                           "source": "API teste", "inbox": "Atendimento"}],
        }
        with patch.multiple(module, LOGIN_CONFIGURED=True, LOGIN_USER="demo", LOGIN_PASSWORD="fixture-pass"), patch.object(
            module, "get_monitor_data", return_value=data,
        ) as provider:
            self.assertEqual(client.get("/tv").location, "/login?tv=whatsapp")
            provider.assert_not_called()
            login = client.post("/login?tv=whatsapp", data={"username": "demo", "password": "fixture-pass"})
            self.assertEqual(login.location, "/tv")
            result = client.get("/tv")
            provider.assert_called_once()
            directory = client.get("/tv?visao=instancias&status=disconnected")
            source = client.get("/tv?visao=apis")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertIn(b'http-equiv="refresh" content="60"', result.data)
        self.assertIn(b"Cliente teste", result.data)
        self.assertIn(b"Desconectado", result.data)
        self.assertNotIn(b"<script", result.data)
        self.assertIn(b"Cliente teste", directory.data)
        self.assertIn(b"API teste", source.data)

    def test_login_rejects_public_default_password_or_session_key(self):
        import runpy

        for dummy in ({}, {"LOGIN_PASSWORD": "admin", "SECRET_KEY": "fixture-long-key-1234567890-12345"},
                      {"LOGIN_PASSWORD": "fixture-unique", "SECRET_KEY": "trocar_secret_key_bem_grande"}):
            with patch.dict("os.environ", dummy, clear=True), patch("dotenv.load_dotenv"):
                client = runpy.run_module("app", run_name="audit_app")["app"].test_client()
                with client.session_transaction() as state:
                    state.update(authenticated=True, username="admin")
                self.assertEqual(client.get("/api/status").status_code, 401)
                self.assertEqual(client.post("/login", data={"username": "admin", "password": "admin"}).status_code, 503)

    def test_original_page_and_get_only_provider(self):
        import app as module

        client = module.app.test_client()
        with tempfile.TemporaryDirectory() as folder, patch.multiple(
            module, LOGIN_CONFIGURED=True, LOGIN_USER="demo", LOGIN_PASSWORD="fixture-pass",
        ), patch.dict("os.environ", {
            "EVOLUTION_01_NAME": "WhatsApp teste",
            "EVOLUTION_01_URL": "https://whatsapp.example.invalid",
            "EVOLUTION_01_KEY": "fixture",
            "PABX_CONFIG_DB": str(Path(folder) / "config.sqlite3"),
        }, clear=True), patch("app.requests.get", return_value=response(200, [{"connectionStatus": "open"}])) as api:
            self.assertEqual(client.get("/api/status").status_code, 401)
            self.assertEqual(client.post("/login", data={"username": "demo", "password": "fixture-pass"}).status_code, 302)
            self.assertEqual(client.get("/dashboard").status_code, 200)
            status = client.get("/api/status")
        self.assertEqual(status.json["summary"]["connected"], 1)
        self.assertEqual(api.call_args.args[0], "https://whatsapp.example.invalid/instance/fetchInstances")

    def test_pause_is_shared_and_scoped_to_one_source_and_can_be_reversed_offline(self):
        import app as module

        first = {"id": "id-1", "name": "Atendimento Norte", "connectionStatus": "close"}
        other = {"name": "Atendimento Sul", "connectionStatus": "open"}
        first_online = True

        def fetch(url, **kwargs):
            if "first.example.invalid" in url:
                return response(200, [first, other]) if first_online else response(503, {})
            return response(200, [first])

        with tempfile.TemporaryDirectory() as folder, patch.multiple(
            module, LOGIN_CONFIGURED=True, LOGIN_USER="demo", LOGIN_PASSWORD="fixture-pass",
        ), patch.dict("os.environ", {
            "EVOLUTION_01_NAME": "Fonte Norte",
            "EVOLUTION_01_URL": "https://first.example.invalid",
            "EVOLUTION_01_KEY": "fixture",
            "EVOLUTION_02_NAME": "Fonte Sul",
            "EVOLUTION_02_URL": "https://second.example.invalid",
            "EVOLUTION_02_KEY": "fixture",
            "PABX_CONFIG_DB": str(Path(folder) / "config.sqlite3"),
        }, clear=True), patch("app.requests.get", side_effect=fetch) as api:
            client = module.app.test_client()
            second_client = module.app.test_client()
            login = {"username": "demo", "password": "fixture-pass"}
            self.assertEqual(client.post("/api/monitoring", json={}).status_code, 401)
            client.post("/login", data=login)
            client.get("/dashboard")
            with client.session_transaction() as state:
                headers = {"X-CSRF-Token": state["monitoring_csrf"]}
            snapshot = client.get("/api/status").json
            self.assertEqual(snapshot["summary"]["total"], 3)
            target = next(row for row in snapshot["instances"] if row["source"] == "Fonte Norte" and row["instance"] == first["name"])
            pause = {"token": target["monitoring_token"], "paused": True}
            self.assertEqual(client.post("/api/monitoring", json=pause).status_code, 403)
            self.assertEqual(client.post("/api/monitoring", json={**pause, "token": "invalid"}, headers=headers).status_code, 400)
            api.reset_mock()
            for _ in range(2):
                self.assertEqual(client.post("/api/monitoring", json=pause, headers=headers).status_code, 204)
            api.assert_not_called()

            second_client.post("/login", data=login)
            first["name"] = "Atendimento Renomeado"
            snapshot = second_client.get("/api/status").json
            self.assertEqual(snapshot["summary"]["total"], 2)
            self.assertEqual(snapshot["summary"]["disconnected"], 1)
            self.assertEqual([row["count"] for row in snapshot["sources"]], [1, 1])
            self.assertEqual([row["source"] for row in snapshot["instances"] if row["instance"] == first["name"]], ["Fonte Sul"])
            self.assertNotIn(first["name"].encode(), second_client.get("/tv?visao=instancias&api=Fonte+Norte").data)
            self.assertEqual(len(snapshot["paused_instances"]), 1)

            first_online = False
            paused = client.get("/api/status").json["paused_instances"]
            self.assertEqual(len(paused), 1)
            resume = {"token": paused[0]["monitoring_token"], "paused": False}
            self.assertEqual(client.post("/api/monitoring", json=resume, headers=headers).status_code, 204)
            first_online = True
            snapshot = second_client.get("/api/status").json
            self.assertEqual(snapshot["summary"]["total"], 3)
            self.assertEqual(snapshot["paused_instances"], [])


if __name__ == "__main__":
    unittest.main()

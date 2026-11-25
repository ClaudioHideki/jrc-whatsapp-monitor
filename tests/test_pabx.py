import base64
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
from unittest.mock import patch

import requests
from cryptography.fernet import Fernet
from flask import Flask, session

from pabx import PABXClient, PABXConfig, PABXError, pabx_origin, pages
from pabx_monitor import PABXMonitor, extension_state
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
        for endpoint in ("login", "dashboard", "logout"):
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

        local_audio = HTTP_ORIGIN + "/recordings/example.wav"
        self.provider.cdr = [
            {**CDR[0], "arquivo_audio": local_audio},
            {**CDR[1], "arquivo_audio": "https://other.example.invalid/private.m4a"},
            {**CDR[2], "arquivo_audio": HTTP_ORIGIN + "/recordings/%2e%2e/private.wav"},
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
                "Content-Type": "audio/x-wav", "Accept-Ranges": "bytes",
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
            self.assertEqual(session.request.call_args.args, ("GET", local_audio))
            self.assertEqual(session.request.call_args.kwargs["headers"]["Range"], "bytes=0-1")
            self.assertNotIn("Authorization", session.request.call_args.kwargs["headers"])
            self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
            audio.close()


class WhatsAppTests(unittest.TestCase):
    def test_original_page_and_get_only_provider(self):
        from app import app

        client = app.test_client()
        self.assertEqual(client.get("/api/status").status_code, 401)
        with client.session_transaction() as state:
            state.update(authenticated=True, username="demo")
        self.assertEqual(client.get("/dashboard").status_code, 200)
        with patch.dict("os.environ", {
            "EVOLUTION_01_NAME": "WhatsApp teste",
            "EVOLUTION_01_URL": "https://whatsapp.example.invalid",
            "EVOLUTION_01_KEY": "fixture",
        }, clear=True), patch("app.requests.get", return_value=response(200, [{"connectionStatus": "open"}])) as api:
            status = client.get("/api/status")
        self.assertEqual(status.json["summary"]["connected"], 1)
        self.assertEqual(api.call_args.args[0], "https://whatsapp.example.invalid/instance/fetchInstances")


if __name__ == "__main__":
    unittest.main()

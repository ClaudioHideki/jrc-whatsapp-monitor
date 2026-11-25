import json
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from pabx import PABXClient, PABXConfig, PABXError, object_rows, pabx_origin
from pabx_monitor import PABXMonitor, check_scope, operational_data, scoped_dashboard


DEFAULT_PROFILE = 1


@dataclass(frozen=True)
class PABXProfile:
    company: str = ""
    url: str = ""
    email: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    account: str = field(default="", repr=False)
    interval: int = 30
    enabled: bool = False
    scope_confirmed: bool = False
    cdr_customer: str = field(default="", repr=False)
    cdr_confirmed: bool = False

    @classmethod
    def from_form(cls, form, saved, allowed_http=""):
        submitted_url = form.get("url", "").strip()
        if len(submitted_url) > 500:
            raise PABXError("connection_fields")
        url = pabx_origin(submitted_url, allowed_http)
        company = form.get("company", "").strip()
        email = form.get("email", "").strip() or (saved.email if url == saved.url else "")
        password = form.get("password", "")
        if not password and url == saved.url and email == saved.email:
            password = saved.password
        account = form.get("account", "").strip()
        cdr_customer = form.get("cdr_customer", "").strip()
        cdr_confirmed = form.get("cdr_confirmed") == "1"
        if (
            not all((company, email, password, account))
            or len(company) > 150 or len(email) > 254 or len(password) > 1024
            or len(account) > 150 or len(cdr_customer) > 150
            or (cdr_confirmed and not cdr_customer)
        ):
            raise PABXError("connection_fields")
        try:
            raw_interval = form.get("interval", "")
            interval = int(raw_interval) if len(raw_interval) <= 4 else 0
        except ValueError:
            raise PABXError("connection_fields") from None
        if not 5 <= interval <= 3600:
            raise PABXError("connection_fields")
        return cls(
            company, url, email, password, account, interval,
            form.get("enabled") == "1", form.get("scope_confirmed") == "1",
            cdr_customer, cdr_confirmed,
        )


class PABXStore:
    def __init__(self, path, secret):
        self.path = Path(path)
        try:
            self._cipher = Fernet(secret)
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS pabx_configs "
                    "(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, payload BLOB NOT NULL)"
                )
        except (OSError, sqlite3.Error, ValueError):
            raise PABXError("storage_setup") from None

    def load(self, profile_id):
        try:
            with closing(sqlite3.connect(self.path)) as db:
                row = db.execute("SELECT revision, payload FROM pabx_configs WHERE id = ?", (profile_id,)).fetchone()
            if row is None:
                return PABXProfile(), 0
            revision, encrypted = row
            contents = json.loads(self._cipher.decrypt(encrypted))
            if contents["id"] != profile_id or contents["revision"] != revision:
                raise PABXError("storage_failure")
            return PABXProfile(**contents["profile"]), revision
        except (sqlite3.Error, InvalidToken, ValueError, TypeError, KeyError):
            raise PABXError("storage_failure") from None

    def save(self, profile_id, profile, revision):
        contents = {"id": profile_id, "revision": revision + 1, "profile": asdict(profile)}
        encrypted = self._cipher.encrypt(json.dumps(contents).encode())
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                if revision == 0:
                    result = db.execute(
                        "INSERT OR IGNORE INTO pabx_configs VALUES (?, 1, ?)", (profile_id, encrypted),
                    )
                else:
                    result = db.execute(
                        "UPDATE pabx_configs SET payload = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
                        (encrypted, profile_id, revision),
                    )
                if result.rowcount != 1:
                    raise PABXError("config_changed")
        except sqlite3.Error:
            raise PABXError("storage_failure") from None
        return revision + 1


class PABXService:
    def __init__(self, store, *, timeout=10, verify=True, client_factory=PABXClient, local_http_origin=""):
        self.store = store
        self.timeout = timeout
        self.verify = verify
        self.local_http_origin = local_http_origin
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._test_lock = threading.Lock()
        self._profile, self._revision = store.load(DEFAULT_PROFILE)
        self._monitor = None
        self._previous = None
        self._replace_monitor()

    @classmethod
    def from_env(cls):
        secret = os.getenv("PABX_CONFIG_KEY")
        if not secret:
            raise PABXError("storage_setup")
        try:
            timeout = int(os.getenv("PABX_TIMEOUT_SECONDS", "10"))
            encoded = secret.encode("ascii")
            local_http = os.getenv("PABX_LOCAL_HTTP_ORIGIN", "").strip().rstrip("/")
            if local_http:
                if not local_http.startswith("http://"):
                    raise ValueError
                local_http = pabx_origin(local_http, local_http)
        except (ValueError, UnicodeError, PABXError):
            raise PABXError("storage_setup") from None
        if not 1 <= timeout <= 30:
            raise PABXError("storage_setup")
        path = Path(os.getenv("PABX_CONFIG_DB") or ".pabx/config.sqlite3").resolve()
        store = PABXStore(path, encoded)
        # Keep this transaction open for the process lifetime. A separate file leaves profile writes free.
        try:
            ownership = sqlite3.connect(path.with_name(f"{path.name}.collector.sqlite3"), timeout=35)
        except sqlite3.Error:
            raise PABXError("storage_setup") from None
        try:
            ownership.execute("BEGIN EXCLUSIVE")
        except sqlite3.Error:
            ownership.close()
            raise PABXError("collector_busy") from None
        try:
            service = cls(
                store, timeout=timeout, verify=os.getenv("PABX_CA_BUNDLE") or True, local_http_origin=local_http,
            )
        except BaseException:
            ownership.close()
            raise
        service._ownership = ownership
        return service

    def _client_config(self, profile):
        return PABXConfig(
            profile.url, profile.email, profile.password, profile.account,
            profile.interval, self.timeout, self.verify,
            profile.cdr_customer if profile.cdr_confirmed else "",
        )

    def transport_allowed(self, profile):
        return not profile.url.startswith("http://") or profile.url == self.local_http_origin

    def _replace_monitor(self):
        if self._monitor is not None:
            self._previous = self._monitor.stop()
            self._monitor = None
        if self._profile.enabled and self._profile.scope_confirmed and self.transport_allowed(self._profile):
            self._monitor = PABXMonitor(self._client_factory(self._client_config(self._profile)))
            self._monitor.start(previous=self._previous)

    def current(self):
        with self._lock:
            return self._profile, self._revision, self._monitor

    def save(self, profile, revision):
        with self._lock:
            if revision != self._revision:
                raise PABXError("config_changed")
            self._revision = self.store.save(DEFAULT_PROFILE, profile, revision)
            if (
                self._client_config(profile) != self._client_config(self._profile)
                or profile.enabled != self._profile.enabled
                or profile.scope_confirmed != self._profile.scope_confirmed
            ):
                self._profile = profile
                self._replace_monitor()
            else:
                self._profile = profile

    def test(self, profile):
        if not self.transport_allowed(profile):
            raise PABXError("http_origin")
        if not self._test_lock.acquire(blocking=False):
            raise PABXError("connection_busy")
        try:
            config = self._client_config(profile)
            _, _, monitor = self.current()
            client = monitor.client if monitor and monitor.client.config == config else self._client_factory(config)
            if config.uses_http:
                operational_data(client, threading.Event())
                return
            data = scoped_dashboard(
                client.query("GET", "dashboard-extension", params={"accountcode": config.account}),
                config.account,
            )
            extensions = data.get("extensions")
            if not isinstance(extensions, dict) or not isinstance(data.get("summary"), dict):
                raise PABXError("upstream_response")
            check_scope(object_rows(extensions.get("items")), "accountcode", config.account)
        finally:
            self._test_lock.release()

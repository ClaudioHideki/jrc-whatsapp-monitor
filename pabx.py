import base64
import binascii
import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import requests


class PABXError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PABXConfig:
    url: str
    email: str = field(repr=False)
    password: str = field(repr=False)
    account: str = field(repr=False)
    interval: int = 30
    timeout: int = 10
    verify: bool | str = True
    cdr_customer: str = field(default="", repr=False)

    @property
    def uses_http(self):
        return self.url.startswith("http://")


def pabx_origin(value, allowed_http=""):
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise PABXError("connection_fields")
    try:
        address = urlsplit(value)
        hostname, port = address.hostname, address.port
    except ValueError:
        raise PABXError("connection_fields") from None
    if (
        address.scheme not in ("http", "https") or not hostname or port == 0
        or address.username is not None or address.password is not None
        or address.query or address.fragment or address.path not in ("", "/")
        or "\\" in value
    ):
        raise PABXError("connection_fields")
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if address.scheme == "https" else 80
    origin = f"{address.scheme}://{host}"
    if port is not None and port != default_port:
        origin += f":{port}"
    if address.scheme == "http" and origin != allowed_http:
        raise PABXError("http_origin")
    return origin


def _timestamp(value):
    try:
        instant = float(value)
    except (TypeError, ValueError, OverflowError):
        if not isinstance(value, str):
            return None
        try:
            date = datetime.fromisoformat(value)
        except ValueError:
            return None
        if date.tzinfo is None:
            return None
        try:
            instant = date.timestamp()
        except (OverflowError, OSError):
            return None
    return instant if math.isfinite(instant) else None


def _token_expiry(data, token, now):
    # JWT claims are only a scheduling hint. The provider checks the bearer token.
    expires_in = _timestamp(data.get("expires_in"))
    expires_at = _timestamp(data.get("expires_at"))
    expirations = [value for value in (expires_at,) if value is not None]
    if expires_in is not None:
        expirations.append(now + expires_in)
    parts = token.split(".")
    if len(parts) == 3:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            expiry = _timestamp(claims.get("exp")) if isinstance(claims, dict) else None
            if expiry is not None:
                expirations.append(expiry)
        except (ValueError, UnicodeError, binascii.Error):
            pass
    if not expirations:
        return math.inf
    expiry = min(expirations)
    return expiry - min(30, max(0, (expiry - now) * 0.1))


class PABXClient:
    LOGIN_COOLDOWN = 30
    HTTP_READ_RESOURCES = frozenset(("ramais", "filas", "dashboard-extension", "dashboard-queue"))

    def __init__(self, config, transport=None):
        self.config = config
        self._transport = transport or (self._direct_request if config.uses_http else requests.request)
        self._requests = threading.BoundedSemaphore(2)
        self._auth_lock = threading.Lock()
        self._token = None
        self._generation = 0
        self._renew_at = 0
        self._retry_at = 0
        self._login_error = None

    @staticmethod
    def _direct_request(method, url, **kwargs):
        with requests.Session() as client:
            client.trust_env = False
            return client.request(method, url, **kwargs)

    def _request(self, method, path, *, token=None, params=None, payload=None, deadline=None):
        timeout = self.config.timeout
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0 or not self._requests.acquire(timeout=timeout):
            raise PABXError("timeout")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise PABXError("timeout")
            response = self._transport(
                method, self.config.url + path, params=params, json=payload,
                headers=headers, timeout=timeout, verify=self.config.verify,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError:
            raise PABXError("tls") from None
        except requests.Timeout:
            raise PABXError("timeout") from None
        except (requests.RequestException, OSError):
            raise PABXError("unavailable") from None
        finally:
            self._requests.release()
        return response

    @staticmethod
    def _read(response):
        status = response.status_code
        if status == 403:
            raise PABXError("forbidden")
        if status == 429 or status >= 500:
            raise PABXError("unavailable")
        if not 200 <= status < 300:
            raise PABXError("upstream_response")
        try:
            data = response.json()
        except ValueError:
            raise PABXError("upstream_response") from None
        if not isinstance(data, dict):
            raise PABXError("upstream_response")
        return data

    def _access_token(self, rejected=None, deadline=None):
        with self._auth_lock:
            if rejected is not None and self._token == rejected:
                self._token = None
            if self._token and time.time() < self._renew_at:
                return self._token
            if time.monotonic() < self._retry_at:
                raise PABXError(self._login_error)

            try:
                response = self._request(
                    "POST", "/api/v1/auth/login", deadline=deadline,
                    payload={"email": self.config.email, "password": self.config.password},
                )
                if response.status_code in (400, 401, 422):
                    raise PABXError("credentials")
                data = self._read(response)
                token = data.get("token") or data.get("access_token")
                if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
                    raise PABXError("upstream_response")
                now = time.time()
                renew_at = _token_expiry(data, token, now)
                if renew_at <= now:
                    raise PABXError("token_expired")
            except PABXError as error:
                self._retry_at = time.monotonic() + self.LOGIN_COOLDOWN
                self._login_error = error.code
                raise

            self._generation += 1
            self._token, self._renew_at = (token, self._generation), renew_at
            return self._token

    def query(self, method, resource, *, params=None, payload=None, deadline=None):
        if self.config.uses_http:
            allowed_get = method == "GET" and resource in self.HTTP_READ_RESOURCES
            allowed_cdr = method == "POST" and resource == "relatorio-cdr/search" and self.config.cdr_customer
            if not (allowed_get or allowed_cdr):
                raise PABXError("http_read_only")
        path = "/api/v1/admin/" + resource
        token = self._access_token(deadline=deadline)
        response = self._request(method, path, token=token[0], params=params, payload=payload, deadline=deadline)
        if response.status_code == 401:
            token = self._access_token(rejected=token, deadline=deadline)
            response = self._request(method, path, token=token[0], params=params, payload=payload, deadline=deadline)
            if response.status_code == 401:
                with self._auth_lock:
                    if self._token == token:
                        self._token = None
                        self._login_error = "token_rejected"
                        self._retry_at = time.monotonic() + self.LOGIN_COOLDOWN
                raise PABXError("token_rejected")
        return self._read(response)


def nonnegative_int(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
        return int(value)
    return None


def object_rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise PABXError("upstream_response")
    return value


def pages(fetch, *, limit):
    rows = []
    expected = total = None
    for page in range(1, limit + 1):
        result = fetch(page)
        if not isinstance(result, dict):
            raise PABXError("upstream_response")
        batch = object_rows(result.get("data"))
        last = nonnegative_int(result.get("last_page"))
        reported_total = nonnegative_int(result.get("total"))
        if last is not None and last > limit:
            raise PABXError("page_limit")
        if (
            nonnegative_int(result.get("current_page")) != page
            or last is None or last < page
            or reported_total is None
            or (expected is not None and (expected != last or total != reported_total))
        ):
            raise PABXError("upstream_response")
        expected, total = last, reported_total
        if not batch and (page < last or total):
            raise PABXError("upstream_response")
        rows.extend(batch)
        if page == last:
            if len(rows) != total:
                raise PABXError("upstream_response")
            return rows
    raise PABXError("page_limit")

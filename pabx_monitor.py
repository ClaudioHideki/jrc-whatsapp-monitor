import hmac
import re
import secrets
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from pabx import PABXError, nonnegative_int, object_rows, pages


EXTENSION_STATES = {
    "available": ("available", "free", "not in use", "not inuse", "idle", "disponível"),
    "busy": ("busy", "inuse", "in use", "incall", "in call", "ocupado", "em ligação", "ringinuse", "on hold"),
    "ringing": ("ringing", "ring", "tocando"),
    "unregistered": ("unregistered", "unavailable", "offline", "unreachable", "não registrado"),
    "registered": ("registered", "online"),
}
STATE_BY_VALUE = {
    value: state for state, values in EXTENSION_STATES.items() for value in values
}


def label(value):
    if type(value) not in (str, int):
        return ""
    return " ".join(str(value).split())[:150]


def key(value):
    return re.sub(r"^(?:PJSIP|SIP)/", "", label(value), flags=re.I)


def flag(value):
    if value in (True, 1, "1"):
        return True
    if value in (False, 0, "0"):
        return False
    return None


def extension_info(item):
    alias = label(item.get("alias_name"))
    match = re.match(r"^(\d+)\s*[-–]\s*(.+)$", alias)
    return {
        "extension": label(item.get("alias")) or (match[1] if match else key(item.get("name"))),
        "name": match[2] if match else alias,
    }


def recording_extensions(people):
    found = defaultdict(set)
    for identifier, person in people.items():
        extension = person["extension"]
        if re.fullmatch(r"[0-9]{1,15}", extension):
            found[identifier].add(extension)
            found[extension].add(extension)
    return {identifier: next(iter(aliases)) for identifier, aliases in found.items() if len(aliases) == 1}


def extension_state(item, value):
    if isinstance(value, dict):
        value = value.get("status") or value.get("state") or value.get("registration_status")
    state = next(
        (
            STATE_BY_VALUE[token]
            for candidate in (value, item.get("status"), item.get("state"), item.get("register"))
            if (token := label(candidate).lower().replace("_", " ")) in STATE_BY_VALUE
        ),
        None,
    )
    registered = flag(item.get("registered"))
    if registered is None:
        registered = flag(item.get("register"))
    busy = flag(item.get("busy"))
    if state is None:
        if busy is True:
            state = "busy"
        elif registered is False:
            state = "unregistered"
        elif registered is True:
            state = "available" if busy is False else "registered"
    if state == "unregistered":
        registered = False
    elif registered is None and state in ("available", "registered"):
        registered = True
    return state or "unknown", registered


def check_scope(rows, field, expected):
    if any(type(row.get(field)) not in (str, int) or str(row[field]).strip() != expected for row in rows):
        raise PABXError("scope_mismatch")
    return rows


def scoped_dashboard(data, account):
    if "status" in data and data["status"] in (False, 0, "0", "error", "failed"):
        raise PABXError("upstream_response")
    filters = data.get("filters")
    if isinstance(filters, dict) and filters.get("accountcode") is not None:
        check_scope([filters], "accountcode", account)
    return data


def call_extension_ids(entries, account):
    if not isinstance(entries, list):
        return None
    identifiers = set()
    for entry in entries:
        if isinstance(entry, dict):
            scope = entry.get("accountcode")
            if scope is not None and str(scope).strip() != account:
                continue
            entry = entry.get("extension") or entry.get("name") or entry.get("ramal")
        identifier = key(entry)
        if not identifier:
            return None
        identifiers.add(identifier)
    return identifiers


def operational_status(query, account):
    live = scoped_dashboard(query("dashboard-extension"), account)
    queue_live = scoped_dashboard(query("dashboard-queue"), account)
    extensions = live.get("extensions")
    summary = live.get("summary")
    if not isinstance(extensions, dict) or not isinstance(summary, dict):
        raise PABXError("upstream_response")
    live_items = check_scope(object_rows(extensions.get("items")), "accountcode", account)
    if nonnegative_int(extensions.get("total")) not in (None, len(live_items)):
        raise PABXError("upstream_response")
    live_queues = check_scope(object_rows(queue_live.get("queues")), "accountcode", account)
    live_queue_codes = {label(item.get("name")) for item in live_queues}

    status_map = live.get("statusMap")
    if isinstance(status_map, dict):
        states = {key(name): state for name, state in status_map.items()}
    elif isinstance(status_map, list):
        states = {
            key(item.get("extension") or item.get("name") or item.get("ramal")): item
            for item in object_rows(status_map)
        }
    else:
        states = {}
    states.pop("", None)
    filters = live.get("filters")
    call_scope_confirmed = isinstance(filters, dict) and str(filters.get("accountcode")).strip() == account
    active_calls = nonnegative_int(summary.get("totalCalls")) if call_scope_confirmed else None
    in_call = call_extension_ids(live.get("extensionsInCall"), account) if call_scope_confirmed else None
    return live_items, states, live_queue_codes, active_calls, in_call


def operational_data(client, stop):
    config = client.config
    account = config.account
    deadline = time.monotonic() + max(60, 4 * config.timeout)

    def query(resource, page=None):
        if stop.is_set():
            raise PABXError("config_changed")
        params = {"accountcode": account}
        if page is not None:
            params["page"] = page
        return client.query("GET", resource, params=params, deadline=deadline)

    directory = check_scope(pages(lambda page: query("ramais", page).get("ramais"), limit=50), "accountcode", account)
    queues = check_scope(pages(lambda page: query("filas", page), limit=50), "accountcode", account)
    live_items, states, live_queue_codes, active_calls, in_call = operational_status(query, account)

    people = {}
    for item in directory:
        identifier = key(item.get("name"))
        if not identifier:
            raise PABXError("upstream_response")
        people[identifier] = {**extension_info(item), "status": "unknown", "registered": None}
    for item in live_items:
        identifier = key(item.get("name"))
        if not identifier:
            raise PABXError("upstream_response")
        person = people.setdefault(identifier, extension_info(item))
        value = states.get(identifier, states.get(person["extension"]))
        person["status"], person["registered"] = extension_state(item, value)

    aliases = Counter(person["extension"] for person in people.values())
    recognized_in_call = set()
    if in_call is not None:
        for identifier, person in people.items():
            alias = person["extension"]
            if identifier in in_call or (alias in in_call and aliases[alias] == 1 and alias not in people):
                recognized_in_call.add(identifier)
        if in_call and not recognized_in_call:
            in_call = None
    for identifier, person in people.items():
        person["in_call"] = None if in_call is None else identifier in recognized_in_call

    memberships = defaultdict(list)
    queue_results = []
    for queue in queues:
        code = label(queue.get("name"))
        if not code:
            raise PABXError("upstream_response")
        name = label(queue.get("descricao")) or code
        members = []
        for member in object_rows(queue["membros"] if queue.get("membros") is not None else []):
            ramal = member.get("ramal")
            if ramal is not None and not isinstance(ramal, dict):
                raise PABXError("upstream_response")
            ramal = ramal or {}
            if ramal.get("accountcode") is not None:
                check_scope([ramal], "accountcode", account)
            identifier = key(ramal.get("name") or member.get("interface"))
            person = people.get(identifier)
            if not person:
                members.append({
                    "extension": "", "name": "", "status": "unknown",
                    "in_call": None, "paused": flag(member.get("paused")),
                })
                continue
            paused = flag(member.get("paused"))
            memberships[identifier].append({"code": code, "name": name, "paused": paused})
            members.append({
                "extension": person["extension"],
                "name": person["name"] or label(member.get("membername")),
                "status": person["status"],
                "in_call": person["in_call"],
                "paused": paused,
            })
        queue_results.append({
            "code": code, "name": name, "strategy": label(queue.get("strategy")),
            "monitored": code in live_queue_codes,
            "members": members,
        })

    ramais = sorted(
        ({**person, "queues": memberships[identifier]} for identifier, person in people.items()),
        key=lambda person: (person["extension"], person["name"]),
    )
    counts = Counter(person["status"] for person in ramais)
    known_state = not ramais or counts["unknown"] < len(ramais)
    known_registration = not ramais or any(person["registered"] is not None for person in ramais)
    data = {
        "indicators": {
            "total": len(ramais),
            "queues": len(queue_results),
            "active_calls": active_calls,
            "extensions_in_call": len(recognized_in_call) if in_call is not None else None,
            "available": counts["available"] if known_state else None,
            "busy": counts["busy"] if known_state else None,
            "ringing": counts["ringing"] if known_state else None,
            "registered": sum(person["registered"] is True for person in ramais) if known_registration else None,
            "unregistered": sum(person["registered"] is False for person in ramais) if known_registration else None,
            "unknown": counts["unknown"],
        },
        "extensions": ramais,
        "queues": sorted(queue_results, key=lambda queue: queue["name"].casefold()),
    }
    return data, recording_extensions(people)


def hide_number(value):
    digits = re.sub(r"\D", "", label(value))
    if not digits:
        return ""
    visible = min(4, len(digits) // 2)
    return "••••" + (digits[-visible:] if visible else "")


def cdr_value(value):
    text = label(value)
    return text if re.fullmatch(r"[-\w .:/+]+", text) and "://" not in text else ""


def recording_reference(value, origin):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(ord(char) < 32 or char in "\\ " for char in value):
        return None
    try:
        address = urlsplit(value)
    except ValueError:
        return None
    if address.fragment or f"{address.scheme}://{address.netloc}" != origin:
        return None
    path = unquote(address.path)
    if any(ord(char) < 32 or char in "\\%" for char in path) or "//" in path:
        return None
    if set(path.split("/")) & {".", ".."} or PurePosixPath(path).suffix.lower() not in {".wav", ".mp3", ".ogg", ".m4a"}:
        return None
    return value


@dataclass(frozen=True)
class Recording:
    url: str = field(repr=False)
    filename_base: str


def recording_filename(row, extensions):
    stamp = "data-hora-nao-informadas"
    for value in (row.get("calldate"), row.get("start")):
        text = label(value)
        if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}", text):
            continue
        try:
            stamp = datetime.fromisoformat(text).strftime("%Y-%m-%d_%H-%M-%S")
        except ValueError:
            continue
        break

    primary = ("canal2", "destino") if label(row.get("direcao")).casefold() == "entrada" else ("canal", "origem")
    secondary = ("canal", "origem") if primary[0] == "canal2" else ("canal2", "destino")
    ramal = "nao-identificado"
    for name in (*primary, *secondary):
        value = label(row.get(name))
        identifier = key(value)
        match = re.fullmatch(r"(?:PJSIP|SIP)/(.+)-[a-f0-9]{8}", value, re.I)
        confirmed = extensions.get(identifier) or (extensions.get(match[1]) if match else None)
        if confirmed:
            ramal = confirmed
            break
    return f"gravacao-{stamp}_ramal-{ramal}"


def cdr_data(client, start, end, stop, recording_key, extensions):
    payload = {
        "datainicial": start.isoformat(), "datafinal": end.isoformat(),
        "horainicial": "00:00", "horafinal": "23:59", "paginacao": 10,
    }
    deadline = time.monotonic() + 50
    def query(page):
        if stop.is_set():
            raise PABXError("config_changed")
        return client.query(
            "POST", "relatorio-cdr/search", params={"page": page}, payload=payload, deadline=deadline,
        ).get("resultado")

    rows = pages(query, limit=100)
    check_scope(rows, "cliente_id", client.config.cdr_customer)
    entries = []
    recordings = {}
    for row in rows:
        reference = recording_reference(row.get("arquivo_audio"), client.config.url)
        recording_id = None
        if reference:
            recording_id = hmac.digest(recording_key, reference.encode(), "sha256").hex()
            if recording_id not in recordings:
                recordings[recording_id] = Recording(reference, recording_filename(row, extensions))
        entries.append({
            "uniqueid": cdr_value(row.get("uniqueid")),
            "date": cdr_value(row.get("calldate") or row.get("start")),
            "source": hide_number(row.get("origem")),
            "destination": hide_number(row.get("destino")),
            "status": cdr_value(row.get("status")),
            "direction": cdr_value(row.get("direcao")),
            "recording": bool(row.get("arquivo_audio")),
            "recording_id": recording_id,
        })
    ids = Counter(row["uniqueid"] for row in entries if row["uniqueid"])
    for row in entries:
        row["id_occurrences"] = ids.get(row["uniqueid"]) or None
    return entries, recordings


class PABXMonitor:
    def __init__(self, client):
        self.client = client
        self._state_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._collecting = False
        self._data = None
        self._recording_extensions = {}
        self._last_success = None
        self._success_clock = None
        self._error = None
        self._history_cache = None
        self._recording_key = secrets.token_bytes(32)

    def _collect(self):
        with self._state_lock:
            self._collecting = True
        try:
            data, extensions = operational_data(self.client, self._stop)
        except PABXError as error:
            with self._state_lock:
                self._error = error.code
                if error.code in ("forbidden", "scope_mismatch", "credentials"):
                    self._data = None
                    self._recording_extensions = {}
        else:
            with self._state_lock:
                self._data = data
                self._recording_extensions = extensions
                self._last_success = datetime.now(timezone.utc).isoformat()
                self._success_clock = time.monotonic()
                self._error = None
        finally:
            with self._state_lock:
                self._collecting = False

    def _poll(self):
        while not self._stop.is_set():
            self._collect()
            self._stop.wait(self.client.config.interval)

    def start(self, previous=None):
        def run():
            if previous is not None:
                previous.join()
            self._poll()

        with self._state_lock:
            self._collecting = True
            self._thread = threading.Thread(target=run, name="pabx-monitor", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        return self._thread

    def snapshot(self):
        interval = self.client.config.interval
        with self._state_lock:
            return {
                "state": self._error or ("connected" if self._data is not None else "starting"),
                "last_success": self._last_success,
                "collecting": self._collecting,
                "stale": self._data is not None and (
                    self._error is not None or time.monotonic() - self._success_clock > interval * 2
                ),
                "data": self._data,
                "poll_seconds": min(interval, 10),
            }

    def history(self, start, end):
        if not self.client.config.cdr_customer:
            raise PABXError("cdr_disabled")
        if not self._history_lock.acquire(blocking=False):
            raise PABXError("history_busy")
        try:
            period = (start, end)
            cached = self._history_cache
            if cached and cached[0] == period and time.monotonic() < cached[1]:
                return cached[2]
            with self._state_lock:
                extensions = self._recording_extensions
            rows, recordings = cdr_data(self.client, start, end, self._stop, self._recording_key, extensions)
            self._history_cache = (period, time.monotonic() + 60, rows, recordings)
            return rows
        except PABXError:
            self._history_cache = None
            raise
        finally:
            self._history_lock.release()

    def recording_url(self, recording_id):
        return self.recording_entries([recording_id])[0].url

    def recording_entries(self, recording_ids):
        cached = self._history_cache
        if self._stop.is_set() or cached is None or any(recording_id not in cached[3] for recording_id in recording_ids):
            raise PABXError("recording_missing")
        return [cached[3][recording_id] for recording_id in recording_ids]

import json
import os
import secrets
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadData, URLSafeSerializer

from pabx_routes import register_pabx
from tv_views import paginate

load_dotenv()

app = Flask(__name__)
configured_secret = os.getenv("SECRET_KEY", "")
valid_secret = configured_secret.strip() not in ("", "change-me", "trocar_secret_key_bem_grande")
app.secret_key = configured_secret if valid_secret else secrets.token_hex(32)
monitor_tokens = URLSafeSerializer(app.secret_key, salt="whatsapp-monitoring")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE") == "1",
)

LOGIN_USER = os.getenv("LOGIN_USER", "admin")
LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD", "")
LOGIN_CONFIGURED = bool(
    valid_secret and LOGIN_USER.strip() and LOGIN_PASSWORD.strip() not in ("", "admin", "trocar_senha")
)

def load_sources():
    sources = []
    for i in range(1, 20):
        prefix = f"EVOLUTION_{i:02d}"
        name = os.getenv(f"{prefix}_NAME")
        url = os.getenv(f"{prefix}_URL")
        key = os.getenv(f"{prefix}_KEY")
        if name and url and key:
            sources.append({
                "id": i,
                "name": name,
                "url": url.rstrip("/"),
                "key": key,
            })
    return sources

def safe_host(url):
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url

def map_status(value):
    mapping = {
        "open": ("connected", "Conectado"),
        "connecting": ("connecting", "Conectando"),
        "close": ("disconnected", "Desconectado"),
    }
    return mapping.get(value, ("unknown", "Desconhecido"))

def sanitize_instance(instance, source):
    chatwoot = instance.get("Chatwoot") or {}
    status_key, status_label = map_status(instance.get("connectionStatus"))
    instance_name = instance.get("name") or ""
    provider_id = instance.get("id")
    identity_kind, identity_value = "name", instance_name
    if type(provider_id) in (str, int) and str(provider_id).strip():
        identity_kind, identity_value = "id", str(provider_id)
    monitor_key = None
    if identity_value:
        identity = [source["id"], source["url"], identity_kind, identity_value]
        monitor_key = sha256(json.dumps(identity).encode()).hexdigest()
    return {
        "monitor_key": monitor_key,
        "source": source["name"],
        "source_host": safe_host(source["url"]),
        "client": chatwoot.get("organization") or "",
        "inbox": chatwoot.get("nameInbox") or "",
        "instance": instance_name,
        "number": instance.get("number") or "",
        "profile": instance.get("profileName") or "",
        "status": status_key,
        "status_label": status_label,
        "last_disconnect": instance.get("disconnectionAt"),
        "disconnect_code": instance.get("disconnectionReasonCode"),
    }

def fetch_source(source):
    started = time.time()
    endpoint = f'{source["url"]}/instance/fetchInstances'
    result = {
        "name": source["name"],
        "host": safe_host(source["url"]),
        "online": False,
        "http_status": None,
        "error": None,
        "duration_ms": None,
        "instances": [],
        "checked_at": int(time.time()),
    }

    try:
        response = requests.get(
            endpoint,
            headers={"apikey": source["key"]},
            timeout=15,
        )
        result["http_status"] = response.status_code
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Resposta inesperada: esperado uma lista de instâncias.")

        result["instances"] = [sanitize_instance(item, source) for item in data]
        result["online"] = True

    except requests.RequestException as exc:
        result["error"] = f"Falha de conexão/HTTP: {exc.__class__.__name__}"
    except Exception as exc:
        result["error"] = f"Falha ao processar resposta: {exc.__class__.__name__}"
    finally:
        result["duration_ms"] = int((time.time() - started) * 1000)

    return result

@contextmanager
def monitoring_db():
    path = Path(os.getenv("PABX_CONFIG_DB") or ".pabx/config.sqlite3")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS whatsapp_paused "
                "(monitor_key TEXT PRIMARY KEY, instance TEXT NOT NULL, inbox TEXT NOT NULL, source TEXT NOT NULL)"
            )
            yield db
    except (OSError, sqlite3.Error):
        app.logger.error("Falha ao acessar o banco do monitoramento WhatsApp.")
        abort(503, description="Não foi possível acessar as pausas do monitoramento.")


def get_monitor_data():
    sources = load_sources()
    results = []

    if sources:
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(sources)))) as pool:
            futures = [pool.submit(fetch_source, source) for source in sources]
            for future in as_completed(futures):
                results.append(future.result())

    with monitoring_db() as db:
        db.row_factory = sqlite3.Row
        paused = {
            row["monitor_key"]: dict(row)
            for row in db.execute(
                "SELECT monitor_key, instance, inbox, source FROM whatsapp_paused ORDER BY source, inbox, instance"
            )
        }

    results.sort(key=lambda x: x["name"].lower())
    all_instances = []
    unavailable = 0

    for result in results:
        if not result["online"]:
            unavailable += 1
        active = [row for row in result["instances"] if row["monitor_key"] not in paused]
        result["count"] = len(active)
        all_instances.extend(active)

    summary = {
        "total": len(all_instances),
        "connected": sum(1 for x in all_instances if x["status"] == "connected"),
        "connecting": sum(1 for x in all_instances if x["status"] == "connecting"),
        "disconnected": sum(1 for x in all_instances if x["status"] == "disconnected"),
        "unknown": sum(1 for x in all_instances if x["status"] == "unknown"),
        "apis_unavailable": unavailable,
    }

    return {
        "generated_at": int(time.time()),
        "summary": summary,
        "sources": [
            {
                "name": r["name"],
                "host": r["host"],
                "online": r["online"],
                "http_status": r["http_status"],
                "count": r["count"],
                "error": r["error"],
                "duration_ms": r["duration_ms"],
                "checked_at": r["checked_at"],
            } for r in results
        ],
        "instances": all_instances,
        "paused_instances": list(paused.values()),
    }

def login_required():
    return LOGIN_CONFIGURED and session.get("authenticated") is True

register_pabx(app, login_required, LOGIN_USER, LOGIN_PASSWORD if LOGIN_CONFIGURED else "")

@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("dashboard" if login_required() else "login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if not LOGIN_CONFIGURED:
        return render_template("login.html", error="Configure o acesso ao monitor no servidor."), 503
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == LOGIN_USER and password == LOGIN_PASSWORD:
            session.clear()
            session["authenticated"] = True
            session["username"] = username
            tv = request.args.get("tv")
            if tv in ("whatsapp", "pabx"):
                return redirect(url_for("whatsapp_tv" if tv == "whatsapp" else "pabx.tv"))
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Usuário ou senha inválidos."), 401
    return render_template("login.html", error=None)

@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard", methods=["GET"])
def dashboard():
    if not login_required():
        return redirect(url_for("login"))
    csrf = session.setdefault("monitoring_csrf", secrets.token_hex(32))
    return render_template("dashboard.html", monitoring_csrf=csrf), 200, {"Cache-Control": "no-store"}

@app.get("/tv")
def whatsapp_tv():
    if not login_required():
        return redirect(url_for("login", tv="whatsapp"))
    snapshot = get_monitor_data()
    attention = [instance for instance in snapshot["instances"] if instance["status"] != "connected"]
    attention.sort(key=lambda instance: instance["status"] != "disconnected")
    view = request.args.get("visao", "resumo")
    if view not in ("resumo", "instancias", "apis"):
        view = "resumo"
    entries = snapshot["sources"] if view == "apis" else snapshot["instances"]
    filters = {"busca": "", "status": "", "api": ""}
    if view == "instancias":
        filters.update(
            busca=request.args.get("busca", "").strip()[:150], status=request.args.get("status", ""),
            api=request.args.get("api", ""),
        )
        term = filters["busca"].casefold()
        entries = [
            row for row in entries
            if (not filters["status"] or row["status"] == filters["status"])
            and (not filters["api"] or row["source"] == filters["api"])
            and term in " ".join(str(row.get(field) or "") for field in ("client", "inbox", "number", "profile", "instance")).casefold()
        ]
    return render_template(
        "tv.html", mode="whatsapp", refresh=60, indicators=snapshot["summary"],
        snapshot=snapshot, attention=attention, view=view, filters=filters,
        page=paginate(entries, request.args.get("pagina", 1, type=int), 10),
        updated=datetime.fromtimestamp(snapshot["generated_at"], timezone.utc),
    ), 200, {"Cache-Control": "no-store"}

@app.route("/api/status", methods=["GET"])
def api_status():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    snapshot = get_monitor_data()
    for row in snapshot["instances"] + snapshot["paused_instances"]:
        key = row.pop("monitor_key")
        if key:
            labels = [str(row[field])[:250] for field in ("instance", "inbox", "source")]
            row["monitoring_token"] = monitor_tokens.dumps([key, *labels])
    return jsonify(snapshot), 200, {"Cache-Control": "no-store"}


@app.post("/api/monitoring")
def update_monitoring():
    if not login_required():
        return jsonify(error="Sua sessão expirou."), 401

    csrf = session.get("monitoring_csrf")
    if not csrf or not secrets.compare_digest(csrf.encode(), request.headers.get("X-CSRF-Token", "").encode()):
        return jsonify(error="Recarregue a página para atualizar o monitoramento."), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("paused")) is not bool or not isinstance(data.get("token"), str):
        return jsonify(error="Solicitação de monitoramento inválida."), 400
    try:
        key, instance, inbox, source = monitor_tokens.loads(data["token"])
    except BadData:
        return jsonify(error="Instância inválida. Atualize o painel e tente novamente."), 400

    with monitoring_db() as db:
        if data["paused"]:
            db.execute(
                "INSERT INTO whatsapp_paused (monitor_key, instance, inbox, source) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(monitor_key) DO NOTHING",
                (key, instance, inbox, source),
            )
        else:
            db.execute("DELETE FROM whatsapp_paused WHERE monitor_key = ?", (key,))
    return "", 204

if __name__ == "__main__":
    http_enabled = os.getenv("PABX_LOCAL_HTTP_ORIGIN") or os.getenv("PABX_DOKPLOY_HTTP_ORIGIN")
    host = "127.0.0.1" if http_enabled else "0.0.0.0"
    app.run(host=host, port=5000, debug=False)

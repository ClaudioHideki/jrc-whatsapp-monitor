import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from pabx_routes import register_pabx

load_dotenv()

app = Flask(__name__)
configured_secret = os.getenv("SECRET_KEY", "")
valid_secret = configured_secret.strip() not in ("", "change-me", "trocar_secret_key_bem_grande")
app.secret_key = configured_secret if valid_secret else secrets.token_hex(32)
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
    return {
        "source": source["name"],
        "source_host": safe_host(source["url"]),
        "client": chatwoot.get("organization") or "",
        "inbox": chatwoot.get("nameInbox") or "",
        "instance": instance.get("name") or "",
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
        "count": 0,
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
        result["count"] = len(result["instances"])
        result["online"] = True

    except requests.RequestException as exc:
        result["error"] = f"Falha de conexão/HTTP: {exc.__class__.__name__}"
    except Exception as exc:
        result["error"] = f"Falha ao processar resposta: {exc.__class__.__name__}"
    finally:
        result["duration_ms"] = int((time.time() - started) * 1000)

    return result

def get_monitor_data():
    sources = load_sources()
    results = []

    if sources:
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(sources)))) as pool:
            futures = [pool.submit(fetch_source, source) for source in sources]
            for future in as_completed(futures):
                results.append(future.result())

    results.sort(key=lambda x: x["name"].lower())
    all_instances = []
    unavailable = 0

    for result in results:
        if not result["online"]:
            unavailable += 1
        all_instances.extend(result["instances"])

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
    return render_template("dashboard.html", username=session.get("username", ""))

@app.get("/tv")
def whatsapp_tv():
    if not login_required():
        return redirect(url_for("login", tv="whatsapp"))
    snapshot = get_monitor_data()
    attention = [instance for instance in snapshot["instances"] if instance["status"] != "connected"]
    attention.sort(key=lambda instance: instance["status"] != "disconnected")
    return render_template(
        "tv.html", mode="whatsapp", refresh=60, indicators=snapshot["summary"],
        snapshot=snapshot, attention=attention, updated=datetime.fromtimestamp(snapshot["generated_at"], timezone.utc),
    ), 200, {"Cache-Control": "no-store"}

@app.route("/api/status", methods=["GET"])
def api_status():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(get_monitor_data())

if __name__ == "__main__":
    http_enabled = os.getenv("PABX_LOCAL_HTTP_ORIGIN") or os.getenv("PABX_DOKPLOY_HTTP_ORIGIN")
    host = "127.0.0.1" if http_enabled else "0.0.0.0"
    app.run(host=host, port=5000, debug=False)

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

LOGIN_USER = os.getenv("LOGIN_USER", "admin")
LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD", "admin")

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
    return session.get("authenticated") is True

@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("dashboard" if login_required() else "login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == LOGIN_USER and password == LOGIN_PASSWORD:
            session.clear()
            session["authenticated"] = True
            session["username"] = username
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

@app.route("/api/status", methods=["GET"])
def api_status():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(get_monitor_data())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

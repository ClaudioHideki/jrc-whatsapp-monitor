import csv
import io
import re
import secrets
from dataclasses import replace
from datetime import date
from functools import wraps

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, session, url_for

from pabx import PABXError
from pabx_store import PABXProfile, PABXService


ERROR_STATUS = {
    "disabled": 503,
    "scope_pending": 503,
    "configuration": 503,
    "storage_setup": 503,
    "storage_failure": 503,
    "cdr_disabled": 403,
    "forbidden": 403,
    "scope_mismatch": 403,
    "filters": 400,
    "page_limit": 422,
    "history_busy": 409,
    "config_changed": 409,
    "connection_busy": 409,
    "connection_fields": 400,
    "csrf": 403,
    "timeout": 504,
    "unavailable": 503,
}

ADMIN_MESSAGES = {
    "storage_setup": "Configure a chave de criptografia do PABX e o caminho do banco no servidor.",
    "storage_failure": "Não foi possível acessar a configuração. Verifique o banco e a chave no servidor.",
    "configuration": "Defina uma senha de acesso exclusiva e uma SECRET_KEY forte para o monitor.",
    "connection_fields": "Confira os campos obrigatórios, a URL HTTPS e o intervalo de 5 a 3600 segundos.",
    "config_changed": "A configuração foi alterada. Recarregue a página antes de continuar.",
    "connection_busy": "Já existe um teste de conexão em andamento.",
    "csrf": "A sessão do formulário expirou. Recarregue a página.",
    "credentials": "O PABX recusou as credenciais de login.",
    "token_expired": "O PABX entregou um token expirado.",
    "token_rejected": "O PABX recusou o token depois da renovação.",
    "forbidden": "A conta do PABX não tem permissão para consultar o painel de ramais.",
    "scope_mismatch": "A resposta do PABX contém dados fora do escopo configurado ou sem identificação.",
    "upstream_response": "O PABX respondeu com um formato inesperado.",
    "timeout": "O PABX não respondeu dentro do prazo.",
    "tls": "Não foi possível validar o certificado HTTPS do PABX.",
    "unavailable": "Não foi possível comunicar com o PABX.",
}


def filters_from_request():
    filters = request.get_json(silent=True)
    if not isinstance(filters, dict):
        raise PABXError("filters")
    dates = (filters.get("start"), filters.get("end"))
    if any(not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in dates):
        raise PABXError("filters")
    try:
        start, end = (date.fromisoformat(value) for value in dates)
    except ValueError:
        raise PABXError("filters") from None
    status, direction = filters.get("status", ""), filters.get("direction", "")
    if not 0 <= (end - start).days <= 30 or any(
        not isinstance(value, str) or len(value) > 150 for value in (status, direction)
    ):
        raise PABXError("filters")
    return start, end, status.casefold(), direction.casefold()


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def cdr_csv(rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(("Data", "Identificador CDR", "Ocorrências do ID no período", "Origem", "Destino",
                     "Situação", "Direção", "Referência de gravação"))
    for row in rows:
        fields = ("date", "uniqueid", "id_occurrences", "source", "destination", "status", "direction")
        writer.writerow([csv_cell(row[field]) for field in fields] + ["Sim" if row["recording"] else ""])
    return Response(
        "\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="pabx-cdr.csv"'},
    )


def register_pabx(app, login_required, login_user, login_password):
    routes = Blueprint("pabx", __name__)
    try:
        if not login_user.strip() or login_password in ("", "admin", "trocar_senha") or len(app.secret_key) < 32:
            raise PABXError("configuration")
        service = PABXService.from_env()
        config_error = None
    except PABXError as error:
        service, config_error = None, error.code
    app.extensions["pabx_service"] = service

    def authorized():
        return login_required() and session.get("username") == login_user

    @routes.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    def api_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not login_required():
                return jsonify(error="unauthorized"), 401
            if not authorized():
                return jsonify(error="forbidden"), 403
            try:
                return view(*args, **kwargs)
            except PABXError as error:
                return jsonify(error=error.code), ERROR_STATUS.get(error.code, 502)
        return wrapped

    @routes.get("/monitoramento-pabx")
    def page():
        if not login_required():
            return redirect(url_for("login"))
        if not authorized():
            return redirect(url_for("dashboard"))
        profile, revision, monitor = service.current() if service else (PABXProfile(), 0, None)
        return render_template("pabx.html", cdr_enabled=bool(monitor and profile.cdr_confirmed), revision=revision)

    @routes.route("/monitoramento-pabx/configuracao", methods=["GET", "POST"])
    def settings():
        if not login_required():
            return redirect(url_for("login"))
        if not authorized():
            return redirect(url_for("dashboard"))
        if service is None:
            return render_template("pabx_settings.html", setup_error=ADMIN_MESSAGES[config_error])

        profile, revision, _ = service.current()
        csrf = session.setdefault("pabx_csrf", secrets.token_hex(32))
        message = None
        if request.method == "POST":
            if request.content_length is None or request.content_length > 16384:
                return jsonify(error="connection_fields", message=ADMIN_MESSAGES["connection_fields"]), 400
            mode = request.form.get("action")
            try:
                submitted_csrf = request.form.get("csrf", "")
                if not re.fullmatch(r"[a-f0-9]{64}", submitted_csrf) or not secrets.compare_digest(csrf, submitted_csrf):
                    raise PABXError("csrf")
                if request.form.get("revision") != str(revision):
                    raise PABXError("config_changed")
                submitted = PABXProfile.from_form(request.form, profile)
                if mode == "test":
                    service.test(submitted)
                    return jsonify(message="Conexão validada. O login e a consulta de status dos ramais responderam.")
                if mode != "save":
                    raise PABXError("connection_fields")
                operational_changed = (profile.url, profile.email, profile.account) != (
                    submitted.url, submitted.email, submitted.account,
                )
                cdr_changed = operational_changed or profile.cdr_customer != submitted.cdr_customer
                operational_recheck = bool(revision) and operational_changed and profile.scope_confirmed
                cdr_recheck = bool(revision) and cdr_changed and profile.cdr_confirmed
                reconfirm = operational_recheck or cdr_recheck
                if reconfirm:
                    submitted = replace(
                        submitted,
                        scope_confirmed=submitted.scope_confirmed and not operational_recheck,
                        cdr_confirmed=submitted.cdr_confirmed and not cdr_recheck,
                    )
                service.save(submitted, revision)
                flash(
                    "Configuração salva. Confirme novamente o escopo alterado antes de retomar a coleta."
                    if reconfirm else "Configuração do PABX salva.",
                    "pabx",
                )
                return redirect(url_for("pabx.settings"), code=303)
            except PABXError as error:
                message = ADMIN_MESSAGES[error.code]
                if mode == "test":
                    return jsonify(error=error.code, message=message), ERROR_STATUS.get(error.code, 502)
                return render_template(
                    "pabx_settings.html", profile=profile, revision=revision, csrf=csrf, message=message,
                ), ERROR_STATUS.get(error.code, 502)
        return render_template("pabx_settings.html", profile=profile, revision=revision, csrf=csrf, message=message)

    @routes.get("/api/pabx/status")
    @api_auth
    def status():
        profile, revision, monitor = service.current() if service else (PABXProfile(), 0, None)
        if monitor is None:
            state = config_error or ("scope_pending" if profile.enabled else "disabled")
            return jsonify(state=state, last_success=None, collecting=False,
                           stale=False, data=None, poll_seconds=10, company=profile.company, revision=revision)
        return jsonify(company=profile.company, revision=revision, **monitor.snapshot())

    @routes.post("/api/pabx/cdr")
    @routes.post("/api/pabx/cdr/export", endpoint="export_history", defaults={"export": True})
    @api_auth
    def history(export=False):
        _, _, monitor = service.current() if service else (None, 0, None)
        if monitor is None:
            raise PABXError(config_error or "cdr_disabled")
        start, end, status, direction = filters_from_request()
        rows = monitor.history(start, end)
        if service.current()[2] is not monitor:
            raise PABXError("config_changed")
        selected = [
            row for row in rows
            if (not status or row["status"].casefold() == status)
            and (not direction or row["direction"].casefold() == direction)
        ]
        if export:
            return cdr_csv(selected)
        return jsonify(
            records=selected,
            statuses=sorted({row["status"] for row in rows if row["status"]}, key=str.casefold),
            directions=sorted({row["direction"] for row in rows if row["direction"]}, key=str.casefold),
        )

    app.register_blueprint(routes)

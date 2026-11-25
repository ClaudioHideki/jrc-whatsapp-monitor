import csv
import io
import os
import re
import secrets
import subprocess
import threading
from collections import Counter
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import date, datetime
from functools import wraps
from itertools import chain
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

import requests
from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, session, url_for
from imageio_ffmpeg import get_ffmpeg_exe

from pabx import PABXError
from pabx_store import PABXProfile, PABXService


ERROR_STATUS = {
    "disabled": 503,
    "scope_pending": 503,
    "configuration": 503,
    "storage_setup": 503,
    "storage_failure": 503,
    "collector_busy": 503,
    "cdr_disabled": 403,
    "forbidden": 403,
    "scope_mismatch": 403,
    "filters": 400,
    "page_limit": 422,
    "history_busy": 409,
    "config_changed": 409,
    "connection_busy": 409,
    "connection_fields": 400,
    "http_origin": 400,
    "http_read_only": 403,
    "csrf": 403,
    "recording_missing": 404,
    "recording_range": 416,
    "recording_selection": 400,
    "recording_busy": 409,
    "timeout": 504,
    "unavailable": 503,
}

WAV_TYPES = frozenset(("audio/wav", "audio/x-wav", "audio/wave"))
MAX_ZIP_RECORDINGS = 1000
MAX_ZIP_REQUEST_BYTES = 96000
MAX_ZIP_AUDIO_BYTES = 256 * 1024 * 1024
RECORDING_EXTENSIONS = {
    **dict.fromkeys(WAV_TYPES, "wav"),
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
}

ADMIN_MESSAGES = {
    "storage_setup": "Configure a chave de criptografia do PABX e o caminho do banco no servidor.",
    "storage_failure": "Não foi possível acessar a configuração. Verifique o banco e a chave no servidor.",
    "collector_busy": "Outra instância do monitor está usando o mesmo banco do PABX.",
    "configuration": "Defina uma senha de acesso exclusiva e uma SECRET_KEY forte para o monitor.",
    "connection_fields": "Confira os campos obrigatórios, a URL do servidor e o intervalo de 5 a 3600 segundos.",
    "http_origin": "Use HTTPS ou o endereço HTTP autorizado no servidor do monitor.",
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
    recording_only = filters.get("recording_only", False)
    if (
        not 0 <= (end - start).days <= 30
        or not isinstance(recording_only, bool)
        or any(not isinstance(value, str) or len(value) > 150 for value in (status, direction))
    ):
        raise PABXError("filters")
    return start, end, status.casefold(), direction.casefold(), recording_only


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


def transcode_wav(upstream, resources, strict=False):
    try:
        encoder = resources.enter_context(subprocess.Popen(
            [get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-protocol_whitelist", "pipe",
             "-f", "wav", "-i", "pipe:0",
             "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", "32k",
             "-write_xing", "0", "-f", "mp3", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ},
        ))
    except (OSError, RuntimeError):
        raise PABXError("recording_missing") from None

    def stop_encoder():
        if encoder.poll() is None:
            encoder.kill()

    resources.callback(stop_encoder)
    feed_failed = threading.Event()

    def feed():
        try:
            with encoder.stdin as input_stream:
                for chunk in upstream.iter_content(chunk_size=16 * 1024):
                    input_stream.write(chunk)
        except (OSError, requests.RequestException):
            feed_failed.set()

    def output():
        threading.Thread(target=feed, name="pabx-recording", daemon=True).start()
        yield from iter(lambda: encoder.stdout.read1(16 * 1024), b"")
        if strict and (encoder.wait() != 0 or feed_failed.is_set()):
            raise PABXError("recording_missing")

    return output()


def stream_recording(url, config, *, method="GET", requested_range=None, strict=False):
    headers = {"Accept-Encoding": "identity"}
    wav_path = unquote(urlsplit(url).path).lower().endswith(".wav")
    if requested_range is not None:
        if not re.fullmatch(r"bytes=(?:[0-9]{1,16}-[0-9]{0,16}|-[0-9]{1,16})", requested_range):
            raise PABXError("recording_range")
        if not wav_path:
            headers["Range"] = requested_range

    with ExitStack() as stack:
        client = stack.enter_context(requests.Session())
        client.trust_env = False
        try:
            upstream = stack.enter_context(client.request(
                method, url, headers=headers, stream=True, allow_redirects=False,
                timeout=config.timeout, verify=config.verify,
            ))
        except requests.Timeout:
            raise PABXError("timeout") from None
        except requests.RequestException:
            raise PABXError("unavailable") from None

        media = upstream.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if upstream.status_code == 416:
            raise PABXError("recording_range")
        if upstream.status_code not in (200, 206) or media not in RECORDING_EXTENSIONS:
            raise PABXError("recording_missing")

        response_headers = {
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
        }
        if wav_path and media in WAV_TYPES:
            if upstream.status_code != 200:
                raise PABXError("recording_missing")
            # A byte range of the WAV cannot serve as a byte range of the converted MP3.
            body = transcode_wav(upstream, stack, strict) if method == "GET" else iter(())
            response = Response(body, content_type="audio/mpeg", headers=response_headers)
            response.call_on_close(stack.pop_all().close)
            return response

        if upstream.headers.get("Accept-Ranges") == "bytes":
            response_headers["Accept-Ranges"] = "bytes"
        if upstream.status_code == 206:
            content_range = upstream.headers.get("Content-Range", "")
            if not re.fullmatch(r"bytes [0-9]+-[0-9]+/(?:[0-9]+|\*)", content_range):
                raise PABXError("recording_missing")
            response_headers["Content-Range"] = content_range
        length = upstream.headers.get("Content-Length", "")
        if re.fullmatch(r"[0-9]{1,17}", length) and upstream.headers.get("Content-Encoding", "identity") == "identity":
            response_headers["Content-Length"] = length

        def chunks():
            try:
                yield from upstream.iter_content(chunk_size=64 * 1024)
            except requests.RequestException:
                # A closed upstream connection ends the stream without logging its URL.
                if strict:
                    raise PABXError("recording_missing") from None

        response = Response(chunks(), status=upstream.status_code, content_type=media, headers=response_headers)
        response.call_on_close(stack.pop_all().close)
        return response


class ZipOutput:
    def __init__(self):
        self.chunks = []

    def write(self, chunk):
        if chunk:
            self.chunks.append(chunk)
        return len(chunk)

    def flush(self):
        pass

    def drain(self):
        chunks, self.chunks = self.chunks, []
        return chunks


def recording_archive(recordings, config, is_current):
    output = ZipOutput()
    warnings = []
    names = Counter()
    remaining = MAX_ZIP_AUDIO_BYTES
    with ZipFile(output, "w") as archive:
        for index, recording in enumerate(recordings, 1):
            if not is_current():
                warnings.append("O acesso às gravações mudou. Os arquivos restantes não foram incluídos.")
                break

            filename = None
            try:
                if not remaining:
                    raise PABXError("archive_limit")
                with closing(stream_recording(recording.url, config, strict=True)) as audio:
                    if audio.status_code != 200:
                        raise PABXError("recording_missing")
                    if int(audio.headers.get("Content-Length", 0)) > remaining:
                        raise PABXError("archive_limit")
                    chunks = iter(audio.response)
                    first = next(chunks, None)
                    if not first:
                        raise PABXError("recording_missing")
                    if len(first) > remaining:
                        raise PABXError("archive_limit")
                    extension = RECORDING_EXTENSIONS[audio.mimetype]
                    filename = f"{recording.filename_base}.{extension}"
                    names[filename] += 1
                    if names[filename] > 1:
                        filename = f"{recording.filename_base}_{names[filename]}.{extension}"
                    with archive.open(filename, "w", force_zip64=True) as entry:
                        for chunk in chain((first,), chunks):
                            if len(chunk) > remaining:
                                raise PABXError("archive_limit")
                            entry.write(chunk)
                            remaining -= len(chunk)
                            yield from output.drain()
            except PABXError as error:
                if error.code == "archive_limit":
                    affected = f"{filename} ficou incompleto." if filename else f"A gravação {index} ficou de fora."
                    warnings.append(f"Limite de tamanho do ZIP atingido. {affected} Os arquivos restantes ficaram de fora.")
                    break
                warnings.append(
                    f"{filename} pode estar incompleto." if filename else f"A gravação {index} não pôde ser incluída."
                )
            yield from output.drain()

        if warnings:
            archive.writestr("resultado.txt", "\n".join(warnings) + "\n")
    yield from output.drain()


def valid_csrf(value):
    return re.fullmatch(r"[a-f0-9]{64}", value) and secrets.compare_digest(value, session.get("pabx_csrf", ""))


def register_pabx(app, login_required, login_user, login_password):
    routes = Blueprint("pabx", __name__)
    media_slots = threading.BoundedSemaphore(2)
    zip_slot = threading.Lock()
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
        return render_template(
            "pabx.html", cdr_enabled=bool(monitor and monitor.client.config.cdr_customer),
            http_mode=profile.url.startswith("http://"), revision=revision,
            csrf=session.setdefault("pabx_csrf", secrets.token_hex(32)),
        )

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
        fixed = service.fixed_http_profile
        if fixed:
            allowed_http = fixed.url
        else:
            allowed_http = service.local_http_origin if request.remote_addr in ("127.0.0.1", "::1") else ""
        display_profile = fixed if fixed and not service.transport_allowed(profile) else profile
        message = None
        if request.method == "POST":
            if request.content_length is None or request.content_length > 16384:
                return jsonify(error="connection_fields", message=ADMIN_MESSAGES["connection_fields"]), 400
            mode = request.form.get("action")
            try:
                submitted_csrf = request.form.get("csrf", "")
                if not valid_csrf(submitted_csrf):
                    raise PABXError("csrf")
                if request.form.get("revision") != str(revision):
                    raise PABXError("config_changed")
                submitted = PABXProfile.from_form(request.form, display_profile, allowed_http, fixed=fixed)
                if mode == "test":
                    service.test(submitted)
                    test_message = (
                        "Conexão validada. Ramais e filas responderam."
                        if submitted.url.startswith("http://")
                        else "Conexão validada. O login e a consulta de status dos ramais responderam."
                    )
                    return jsonify(message=test_message)
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
                    "pabx_settings.html", profile=display_profile, revision=revision, csrf=csrf, message=message,
                    allowed_http=allowed_http, fixed=fixed,
                ), ERROR_STATUS.get(error.code, 502)
        return render_template(
            "pabx_settings.html", profile=display_profile, revision=revision, csrf=csrf, message=message,
            allowed_http=allowed_http, fixed=fixed,
        )

    def status_snapshot():
        profile, revision, monitor = service.current() if service else (PABXProfile(), 0, None)
        if monitor is None:
            state = config_error or ("scope_pending" if profile.enabled else "disabled")
            if service and profile.enabled and not service.transport_allowed(profile):
                state = "http_origin"
            return dict(state=state, last_success=None, collecting=False,
                        stale=False, data=None, poll_seconds=10, company=profile.company, revision=revision)
        return dict(company=profile.company, revision=revision, **monitor.snapshot())

    @routes.get("/api/pabx/status")
    @api_auth
    def status():
        return jsonify(status_snapshot())

    @routes.get("/tv/pabx")
    def tv():
        if not login_required():
            return redirect(url_for("login", tv="pabx"))
        if not authorized():
            return redirect(url_for("dashboard"))
        snapshot = status_snapshot()
        states = {
            "connected": "API conectada.", "starting": "Aguardando a primeira coleta.",
            "disabled": "Monitoramento desativado.", "scope_pending": "Confirme o escopo do monitoramento.",
        }
        data = snapshot["data"] or {}
        return render_template(
            "tv.html", mode="pabx", refresh=15, indicators=data.get("indicators", {}),
            snapshot=snapshot, queues=data.get("queues", []),
            connection=states.get(snapshot["state"]) or ADMIN_MESSAGES.get(snapshot["state"], "Falha ao consultar o PABX."),
            updated=datetime.fromisoformat(snapshot["last_success"]) if snapshot["last_success"] else None,
        )

    @routes.post("/api/pabx/cdr")
    @routes.post("/api/pabx/cdr/export", endpoint="export_history", defaults={"export": True})
    @api_auth
    def history(export=False):
        _, _, monitor = service.current() if service else (None, 0, None)
        if monitor is None:
            raise PABXError(config_error or "cdr_disabled")
        start, end, status, direction, recording_only = filters_from_request()
        rows = monitor.history(start, end)
        if service.current()[2] is not monitor:
            raise PABXError("config_changed")
        selected = [
            row for row in rows
            if (not status or row["status"].casefold() == status)
            and (not direction or row["direction"].casefold() == direction)
            and (not recording_only or row["recording"])
        ]
        if export:
            return cdr_csv(selected)
        return jsonify(
            records=selected,
            statuses=sorted({row["status"] for row in rows if row["status"]}, key=str.casefold),
            directions=sorted({row["direction"] for row in rows if row["direction"]}, key=str.casefold),
        )

    @routes.get("/api/pabx/recordings/<recording_id>")
    @api_auth
    def recording(recording_id):
        _, _, monitor = service.current() if service else (None, 0, None)
        if monitor is None or not re.fullmatch(r"[a-f0-9]{64}", recording_id):
            raise PABXError("recording_missing")
        with ExitStack() as slots:
            if not media_slots.acquire(blocking=False):
                raise PABXError("recording_busy")
            slots.callback(media_slots.release)
            result = stream_recording(
                monitor.recording_url(recording_id), monitor.client.config,
                method=request.method, requested_range=request.headers.get("Range"),
            )
            if service.current()[2] is not monitor or not monitor.accepts_recordings():
                result.close()
                raise PABXError("config_changed")
            if request.args.get("download") == "1":
                extension = RECORDING_EXTENSIONS[result.mimetype]
                result.headers.set("Content-Disposition", "attachment", filename=f"gravacao-pabx.{extension}")
            result.call_on_close(slots.pop_all().close)
        return result

    @routes.post("/api/pabx/recordings/zip")
    @api_auth
    def recordings_zip():
        _, _, monitor = service.current() if service else (None, 0, None)
        if monitor is None or not monitor.client.config.cdr_customer:
            raise PABXError("cdr_disabled")
        if not 0 < (request.content_length or 0) <= MAX_ZIP_REQUEST_BYTES:
            raise PABXError("recording_selection")
        if not valid_csrf(request.form.get("csrf", "")):
            raise PABXError("csrf")
        ids = request.form.getlist("recording_id")
        if not 1 <= len(ids) <= MAX_ZIP_RECORDINGS or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in ids):
            raise PABXError("recording_selection")

        recordings = monitor.recording_entries(dict.fromkeys(ids))
        if service.current()[2] is not monitor:
            raise PABXError("config_changed")
        with ExitStack() as slots:
            if not zip_slot.acquire(blocking=False):
                raise PABXError("recording_busy")
            slots.callback(zip_slot.release)
            if not media_slots.acquire(blocking=False):
                raise PABXError("recording_busy")
            slots.callback(media_slots.release)
            result = Response(
                recording_archive(
                    recordings, monitor.client.config,
                    lambda: service.current()[2] is monitor and monitor.accepts_recordings(),
                ),
                content_type="application/zip",
                headers={"Content-Disposition": 'attachment; filename="gravacoes-pabx.zip"'},
            )
            result.call_on_close(slots.pop_all().close)
        return result

    app.register_blueprint(routes)

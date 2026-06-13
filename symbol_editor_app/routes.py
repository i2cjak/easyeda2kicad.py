from __future__ import annotations

from flask import Blueprint, Response, jsonify, render_template, request, send_file
from io import BytesIO
from pathlib import Path

from .workflow import (
    apply_codex_suggestions,
    cancel_all_codex_jobs,
    clean_payload,
    export_bundle_zip,
    export_symbol_library,
    filename_for_payload,
    get_codex_job,
    get_import_job,
    start_codex_pin_review,
    start_import_job,
    validate_symbol_payload,
)

bp = Blueprint("symbol_editor", __name__)
STATIC_DIR = Path(__file__).parent / "static"


def _asset_version() -> str:
    mtimes = [
        (STATIC_DIR / filename).stat().st_mtime
        for filename in ("app.js", "styles.css")
        if (STATIC_DIR / filename).exists()
    ]
    return str(int(max(mtimes, default=0)))


@bp.get("/")
def index() -> str:
    return render_template("index.html", asset_version=_asset_version())


def _assert_exportable(payload: dict) -> None:
    result = validate_symbol_payload(payload)
    if result.get("status") == "error":
        messages = [
            str(check.get("message") or "")
            for check in result.get("checks", [])
            if check.get("level") == "error"
        ]
        detail = "; ".join(message for message in messages if message)
        raise ValueError(detail or result.get("message") or "Validation failed.")


@bp.post("/api/import")
def import_part() -> Response:
    data = request.get_json(silent=True) or {}
    try:
        job_id = start_import_job(
            str(data.get("lcsc_id") or ""),
            run_codex=bool(data.get("run_codex", True)),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(get_import_job(job_id)), 202


@bp.get("/api/import/<job_id>")
def import_status(job_id: str) -> Response:
    return jsonify(get_import_job(job_id))


@bp.post("/api/cleanup")
def cleanup() -> Response:
    data = request.get_json(silent=True) or {}
    try:
        payload = clean_payload(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(payload)


@bp.get("/api/codex/<job_id>")
def codex_status(job_id: str) -> Response:
    return jsonify(get_codex_job(job_id))


@bp.post("/api/codex/cancel_all")
def codex_cancel_all() -> Response:
    return jsonify(cancel_all_codex_jobs())


@bp.post("/api/codex/start")
def codex_start() -> Response:
    data = request.get_json(silent=True) or {}
    payload = data.get("payload") or data
    if not payload.get("symbol"):
        return jsonify({"error": "No symbol payload to review."}), 400

    try:
        job_id = start_codex_pin_review(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    job = get_codex_job(job_id)
    return jsonify({"job_id": job_id, **job}), 202


@bp.post("/api/codex/apply")
def codex_apply() -> Response:
    data = request.get_json(silent=True) or {}
    try:
        payload = apply_codex_suggestions(
            data.get("payload") or {},
            data.get("suggestions") or {},
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(payload)


@bp.post("/api/validate")
def validate_symbol() -> Response:
    data = request.get_json(silent=True) or {}
    try:
        result = validate_symbol_payload(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@bp.post("/api/export/symbol")
def export_symbol() -> Response:
    payload = request.get_json(silent=True) or {}
    try:
        _assert_exportable(payload)
        content = export_symbol_library(payload)
        filename = filename_for_payload(payload, ".kicad_sym")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return Response(
        content,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/x-kicad-symbols; charset=utf-8",
        },
    )


@bp.post("/api/export/bundle")
def export_bundle() -> Response:
    payload = request.get_json(silent=True) or {}
    try:
        _assert_exportable(payload)
        content = export_bundle_zip(payload)
        filename = filename_for_payload(payload, "_kicad_assets.zip")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return send_file(
        BytesIO(content),
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )

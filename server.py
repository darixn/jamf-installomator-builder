#!/usr/bin/env python3
from __future__ import annotations

"""
Jamf Installomator Builder — local web server
----------------------------------------------
Usage:
  python3 server.py            # normal run, opens browser automatically
  python3 server.py --debug    # dry-run, no Jamf API calls
  python3 server.py --port 8080
"""

import argparse
import json
import os
import queue
import threading
import time
import webbrowser

from flask import Flask, jsonify, render_template, request, Response, stream_with_context

import installomator
from jamf_api import JamfClient, JamfAPIError

app = Flask(__name__)
DEBUG_MODE = False
_build_queues: dict[str, queue.Queue] = {}

# Build queue TTL — abandon stale queues after this many seconds
_BUILD_QUEUE_TTL = 300  # 5 minutes
_build_queue_timestamps: dict[str, float] = {}


def _csrf_check() -> bool:
    """Allow requests only from our own localhost origin."""
    origin = request.headers.get("Origin", "")
    host   = request.headers.get("Host", "")
    # Accept requests with no Origin (curl / direct) or matching localhost origin
    if not origin:
        return True
    return origin in (f"http://localhost:{host.split(':')[-1]}", f"http://127.0.0.1:{host.split(':')[-1]}")


def _reap_stale_queues() -> None:
    """Remove build queues that were never consumed (browser closed mid-build)."""
    now = time.time()
    stale = [bid for bid, ts in _build_queue_timestamps.items() if now - ts > _BUILD_QUEUE_TTL]
    for bid in stale:
        _build_queues.pop(bid, None)
        _build_queue_timestamps.pop(bid, None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", debug=DEBUG_MODE)


@app.route("/api/labels")
def get_labels():
    source = _build_source_from_params(request.args)
    try:
        labels = installomator.fetch_labels(source=source, force_refresh=True)
        desc = installomator.describe_source(source)
        return jsonify({"ok": True, "labels": labels, "source": desc})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


def _build_source_from_params(params) -> dict:
    """Build a source config dict from request params or JSON body."""
    src_type = params.get("source_type", "github")
    if src_type == "fork":
        return {"type": "fork",
                "repo": params.get("source_repo", ""),
                "branch": params.get("source_branch", "main") or "main"}
    if src_type == "local":
        return {"type": "local", "path": params.get("source_path", "")}
    return {"type": "github"}


@app.route("/api/connect", methods=["POST"])
def test_connect():
    if not _csrf_check():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if DEBUG_MODE:
        return jsonify({"ok": True, "message": "Debug mode — skipping real auth"})
    data = request.json or {}
    try:
        jamf = JamfClient(data["jamf_url"], data["client_id"], data["client_secret"])
        jamf.authenticate()
        return jsonify({"ok": True})
    except (JamfAPIError, Exception) as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/preview", methods=["POST"])
def preview_build():
    if not _csrf_check():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    data = request.json or {}
    labels = data.get("labels", [])
    source = _build_source_from_params(data)
    try:
        label_info = installomator.resolve_display_names(labels, source=source)
        preview = []
        for label in labels:
            info = label_info.get(label, {"name": label, "app_name": label, "app_name_confirmed": False})
            app_name = info["name"]
            app_bundle = info["app_name"]
            confirmed = info.get("app_name_confirmed", False)
            preview.append({
                "label": label,
                "smart_group": f"{app_name} Installed",
                "app_bundle": f"{app_bundle}.app",
                "ss_policy": f"Install {app_name}",
                "au_policy": f"Auto-Update {app_name}",
                "confirmed": confirmed,
            })
        return jsonify({"ok": True, "preview": preview})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/build", methods=["POST"])
def start_build():
    if not _csrf_check():
        return jsonify({"error": "Forbidden"}), 403
    import uuid
    _reap_stale_queues()
    config = request.json or {}
    build_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _build_queues[build_id] = q
    _build_queue_timestamps[build_id] = time.time()
    thread = threading.Thread(target=_run_build, args=(config, q), daemon=True)
    thread.start()
    return jsonify({"build_id": build_id})


@app.route("/api/build/stream")
def build_stream():
    build_id = request.args.get("id", "")
    q = _build_queues.get(build_id)
    if not q:
        return "", 404

    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
            except queue.Empty:
                yield "data: {\"type\":\"heartbeat\"}\n\n"
                continue
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("type") == "done":
                break
        _build_queues.pop(build_id, None)
        _build_queue_timestamps.pop(build_id, None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Build worker (runs in background thread)
# ---------------------------------------------------------------------------

def _run_build(config: dict, q: queue.Queue) -> None:
    labels      = config.get("labels", [])
    ss_behavior = config.get("ss_behavior", {})
    au_behavior = config.get("au_behavior", {})
    icons_folder = config.get("icons_folder", "")
    source      = _build_source_from_params(config)
    total   = len(labels) * 3
    done    = 0
    created = 0
    skipped = 0
    failed: list[list[str]] = []
    needs_review: list[str] = []

    def emit(**kwargs):
        q.put(kwargs)

    def tick(label: str, step: str):
        nonlocal done
        done += 1
        pct = int(done / total * 100) if total else 100
        emit(type="progress", label=label, step=step, pct=pct)

    src_desc = installomator.describe_source(source)
    emit(type="log", text=f"Installomator source: {src_desc}")

    # -- Auth / script upload
    if DEBUG_MODE:
        jamf = None
        script_id = 9999
        emit(type="log", text="[DEBUG] Dry-run — no Jamf API calls will be made.")
    else:
        try:
            jamf = JamfClient(config["jamf_url"], config["client_id"], config["client_secret"])
            jamf.authenticate()
        except (JamfAPIError, Exception) as exc:
            emit(type="done", created=0, skipped=0, failed=[["auth", str(exc)]])
            return

        emit(type="log", text="Fetching Installomator script contents…")
        try:
            script_body = installomator.fetch_script_contents(source)
        except Exception as exc:
            emit(type="done", created=0, skipped=0, failed=[["script_fetch", str(exc)]])
            return

        emit(type="log", text="Uploading/checking Installomator script in Jamf…")
        try:
            script_id = jamf.ensure_installomator_script(script_contents=script_body)
        except Exception as exc:
            emit(type="done", created=0, skipped=0, failed=[["script_upload", str(exc)]])
            return

    # -- Resolve label info (display name + correct .app bundle name)
    emit(type="log", text=f"Resolving app names for {len(labels)} labels from {src_desc}…")
    label_info = installomator.resolve_display_names(labels, source=source)

    resolved_count = sum(1 for v in label_info.values() if v.get("app_name_confirmed"))
    emit(type="log", text=f"Resolved {resolved_count}/{len(labels)} labels with confirmed appName")

    # -- Per-label loop
    for idx, label in enumerate(labels):
        info        = label_info.get(label, {"name": label, "app_name": label, "app_name_confirmed": False})
        app_name    = info["name"]      # e.g. "Microsoft Visual Studio"  → used in policy names
        app_bundle  = info["app_name"]  # e.g. "Visual Studio"            → used in Smart Group criterion (.app appended in jamf_api)
        confirmed   = info.get("app_name_confirmed", False)
        fake_id     = 9000 + idx

        if not confirmed:
            needs_review.append(label)
            emit(type="warn", text=f'⚠️  {label}: no appName= found in fragment — Smart Group criterion set to "{app_bundle}.app" — verify in Jamf UI')

        # Smart Group
        try:
            if DEBUG_MODE:
                emit(type="log", text=f'[DEBUG] Smart Group → "{app_name} Installed" (criterion: {app_bundle}.app)')
                group_id, group_created = fake_id, True
            else:
                group_id, group_created = jamf.create_smart_group(app_name, app_bundle)
            created += 1 if group_created else 0
            skipped += 0 if group_created else 1
            if group_created:
                emit(type="created", label=label, object_type="smart_group",
                     id=group_id, name=f"{app_name} Installed")
            tick(label, f"Smart Group: {app_name}")
        except Exception as exc:
            failed.append([f"{label} [Smart Group]", str(exc)])
            emit(type="warn", text=f"Smart Group failed for {label}: {exc}")
            done += 3
            skipped += 2
            continue

        # Self Service Policy
        icon_path = _find_icon(icons_folder, label)
        try:
            if DEBUG_MODE:
                emit(type="log", text=f'[DEBUG] Self Service → "Install {app_name}" ({label}) behavior={ss_behavior}')
                ss_id, ss_created = fake_id + 100, True
            else:
                ss_id, ss_created = jamf.create_self_service_policy(
                    app_name=app_name, label=label,
                    script_id=script_id, behavior=ss_behavior,
                )
                if ss_created and icon_path:
                    icon_id = jamf.upload_icon(icon_path)
                    if icon_id:
                        jamf.attach_icon_to_policy(ss_id, icon_id)
            created += 1 if ss_created else 0
            skipped += 0 if ss_created else 1
            if ss_created:
                emit(type="created", label=label, object_type="ss_policy",
                     id=ss_id, name=f"Install {app_name}")
            tick(label, f"Self Service: {app_name}")
        except Exception as exc:
            failed.append([f"{label} [Self Service]", str(exc)])
            emit(type="warn", text=f"Self Service failed for {label}: {exc}")
            done += 1

        # Auto-Update Policy
        try:
            if DEBUG_MODE:
                emit(type="log", text=f'[DEBUG] Auto-Update → "Auto-Update {app_name}" group_id={group_id} behavior={au_behavior}')
                au_id, au_created = fake_id + 200, True
            else:
                au_id, au_created = jamf.create_autoupdate_policy(
                    app_name=app_name, label=label,
                    script_id=script_id, smart_group_id=group_id,
                    behavior=au_behavior,
                )
                if au_created and icon_path:
                    icon_id = jamf.upload_icon(icon_path)
                    if icon_id:
                        jamf.attach_icon_to_policy(au_id, icon_id)
            created += 1 if au_created else 0
            skipped += 0 if au_created else 1
            if au_created:
                emit(type="created", label=label, object_type="au_policy",
                     id=au_id, name=f"Auto-Update {app_name}")
            tick(label, f"Auto-Update: {app_name}")
        except Exception as exc:
            failed.append([f"{label} [Auto-Update]", str(exc)])
            emit(type="warn", text=f"Auto-Update failed for {label}: {exc}")
            done += 1

    emit(type="done", created=created, skipped=skipped, failed=failed, needs_review=needs_review)


def _find_icon(icons_folder: str, label: str) -> str | None:
    if not icons_folder:
        return None
    p = os.path.join(icons_folder, f"{label}.png")
    return p if os.path.isfile(p) else None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global DEBUG_MODE
    parser = argparse.ArgumentParser(description="Jamf Installomator Builder")
    parser.add_argument("--debug", "-debug", action="store_true",
                        help="Dry-run mode — no Jamf API calls made")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    DEBUG_MODE = args.debug

    url = f"http://localhost:{args.port}"
    print(f"\nJamf Installomator Builder")
    print(f"  → {url}")
    if DEBUG_MODE:
        print("  → DEBUG mode: no Jamf API calls will be made")
    print()

    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

"""Read-only fleet portal plus authenticated, outbound-only health ingestion.

Production sits behind OAuth2 Proxy on a private container network. No commands,
arbitrary requests, remote terminals, alarm endpoints, or update jobs exist here.
"""
import hmac
import json
import os
from pathlib import Path
import re
import time

from flask import Flask, abort, g, jsonify, request, send_from_directory

import protocol
import store

ROOT = Path(__file__).resolve().parent


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(MODE=os.environ.get("ASTRA_MODE", "production"),
                      DATA_DIR=os.environ.get("ASTRA_DATA_DIR", str(ROOT / "var")),
                      OPERATORS_FILE=os.environ.get("ASTRA_OPERATORS_FILE", ""),
                      CLOCK=time.time, MAX_CONTENT_LENGTH=protocol.MAX_BODY)
    if config:
        app.config.update(config)
    mode = app.config["MODE"]
    if mode not in {"preview", "production", "test"}:
        raise RuntimeError("Unknown operating mode.")
    operators = set()
    if mode == "production":
        path = app.config["OPERATORS_FILE"]
        if not path or not Path(path).is_file():
            raise RuntimeError("Production requires an operator allowlist and the authentication proxy.")
        operators = {line.strip().lower() for line in Path(path).read_text().splitlines() if line.strip() and not line.startswith("#")}
        if not operators or any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", x) or "*" in x for x in operators):
            raise RuntimeError("Operator allowlist must contain explicit email addresses.")
    store.initialize(app.config["DATA_DIR"])

    def now():
        return int(app.config["CLOCK"]())

    @app.before_request
    def access():
        # Preview is inaccessible over the LAN even if someone changes its bind.
        if mode == "preview" and (request.remote_addr not in {"127.0.0.1", "::1"} or request.host.split(":")[0] not in {"127.0.0.1", "localhost", "["}):
            abort(403)
        if request.path in {"/healthz", "/api/v1/heartbeat"}:
            return
        if mode == "production":
            email = request.headers.get("X-Forwarded-Email", "").strip().lower()
            if email not in operators:
                abort(403)
            g.operator = email
        else:
            g.operator = "Local preview" if mode == "preview" else "test operator"

    @app.after_request
    def headers(response):
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()"
        })
        return response

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def error(err):
        return jsonify(error=err.name), err.code

    @app.get("/healthz")
    def health():
        with store.connect(app.config["DATA_DIR"]) as db:
            db.execute("SELECT 1 FROM panels LIMIT 1").fetchone()
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return send_from_directory(ROOT / "static", "index.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(ROOT / "static", name)

    @app.get("/api/fleet")
    def fleet():
        with store.connect(app.config["DATA_DIR"]) as db:
            panels = [store.public_panel(row, now()) for row in db.execute("SELECT * FROM panels ORDER BY site, name")]
        return jsonify(panels=panels, server_time=now(), demo=(mode == "preview"), operator=g.operator,
                       capabilities={"health": True, "remote_support": False, "remote_updates": False, "alarm_control": False})

    @app.get("/api/activity")
    def activity():
        with store.connect(app.config["DATA_DIR"]) as db:
            rows = db.execute("SELECT audit.id, panel_id, action, actor, created_at, panels.site FROM audit LEFT JOIN panels ON panels.id=audit.panel_id ORDER BY created_at DESC, audit.id DESC LIMIT 100").fetchall()
        return jsonify(events=[dict(row) for row in rows], demo=(mode == "preview"))

    @app.post("/api/v1/heartbeat")
    def heartbeat():
        # Demo fixtures cannot accidentally become real customer data.
        if mode == "preview":
            abort(403)
        if request.mimetype != "application/json" or request.headers.get("Content-Encoding"):
            abort(400)
        body = request.get_data(cache=False)
        panel_id = request.headers.get("X-Astra-Panel", "")
        stamp = request.headers.get("X-Astra-Time", "")
        nonce = request.headers.get("X-Astra-Nonce", "")
        supplied = request.headers.get("X-Astra-Signature", "")
        if not protocol.DEVICE_ID.fullmatch(panel_id) or not re.fullmatch(r"[0-9]{10,11}", stamp) or not protocol.NONCE.fullmatch(nonce) or not re.fullmatch(r"[a-f0-9]{64}", supplied):
            abort(403)
        timestamp, received_at = int(stamp), now()
        if abs(received_at - timestamp) > 120:
            abort(403)
        with store.connect(app.config["DATA_DIR"]) as db:
            # Serializes acceptance with revocation, sequence and nonce checks.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                abort(403)
            expected = protocol.signature(row["secret"], panel_id, stamp, nonce, body)
            if not hmac.compare_digest(expected, supplied):
                abort(403)
            try:
                payload = protocol.validate(json.loads(body))
            except (ValueError, TypeError, UnicodeDecodeError):
                abort(400)
            if row["last_device_time"] is not None and timestamp < row["last_device_time"]:
                abort(409)
            if db.execute("SELECT 1 FROM nonces WHERE panel_id=? AND nonce=?", (panel_id, nonce)).fetchone():
                abort(409)
            if row["last_seen"] is not None and received_at - row["last_seen"] < 15:
                abort(429)
            db.execute("INSERT INTO nonces VALUES(?,?,?)", (panel_id, nonce, received_at))
            db.execute("DELETE FROM nonces WHERE seen_at < ?", (received_at - 600,))
            db.execute("UPDATE panels SET last_seen=?,last_device_time=?,health=? WHERE id=?",
                       (received_at, timestamp, protocol.encode(payload).decode(), panel_id))
            if row["last_seen"] is None:
                store.event(db, panel_id, "First health check received", "Panel", received_at)
        # No command channel. The agent discards the response body.
        return "", 204

    return app

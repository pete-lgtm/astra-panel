"""Private fleet inventory, most recent health snapshot, and audit history."""
import json
import os
from pathlib import Path
import sqlite3
import time


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(data_dir):
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_mode & 0o077:
        raise RuntimeError("Fleet data directory must have mode 0700.")
    path = directory / "fleet.sqlite3"
    # Restrictive creation before SQLite opens it, including first start.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Fleet database must have mode 0600.")
    db = sqlite3.connect(path, timeout=10, factory=Connection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize(data_dir):
    with connect(data_dir) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS panels (
            id TEXT PRIMARY KEY, site TEXT NOT NULL, name TEXT NOT NULL,
            secret TEXT NOT NULL, enrolled_at INTEGER NOT NULL,
            revoked_at INTEGER, last_seen INTEGER, last_device_time INTEGER,
            health TEXT
        );
        CREATE TABLE IF NOT EXISTS nonces (
            panel_id TEXT NOT NULL REFERENCES panels(id), nonce TEXT NOT NULL,
            seen_at INTEGER NOT NULL, PRIMARY KEY(panel_id, nonce)
        );
        CREATE INDEX IF NOT EXISTS nonce_age ON nonces(seen_at);
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, panel_id TEXT,
            action TEXT NOT NULL, actor TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        """)


def event(db, panel_id, action, actor, now=None):
    db.execute("INSERT INTO audit(panel_id,action,actor,created_at) VALUES(?,?,?,?)",
               (panel_id, action, actor, int(time.time()) if now is None else now))


def public_panel(row, now):
    health = json.loads(row["health"]) if row["health"] else None
    age = max(0, now - row["last_seen"]) if row["last_seen"] is not None else None
    status = "revoked" if row["revoked_at"] else "pending" if age is None else "offline" if age > 180 else "online"
    issues = []
    if status == "offline":
        issues.append("Panel has stopped checking in")
    if health and status == "online":
        if health["panel_service"] != "active":
            issues.append("Panel service needs checking")
        if health["temperature_c"] is not None and health["temperature_c"] >= 80:
            issues.append("Panel is running warm")
        if health["disk_used_pct"] is not None and health["disk_used_pct"] >= 85:
            issues.append("Storage is nearly full")
    return {"id": row["id"], "site": row["site"], "name": row["name"],
            "status": status, "last_seen": row["last_seen"], "health": health,
            "issues": issues, "support": "not_configured", "updates": "not_configured"}

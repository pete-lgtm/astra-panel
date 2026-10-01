"""Loopback-only interactive preview. Fixtures never represent live customer data."""
import os
import tempfile
import time

from app import create_app
import protocol
import store


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="astra-portal-preview-") as directory:
        app = create_app({"MODE": "preview", "DATA_DIR": directory})
        now = int(time.time())
        fixtures = [
            ("demo-studio", "Astra studio", "Bench panel", "0.2.44", 0, 48.6, 28, 17, "active", 5),
            ("demo-oakhouse", "Oak house", "Entry panel", "0.2.44", 0, 52.1, 32, 23, "active", 12),
            ("demo-hillcrest", "Hillcrest", "Kitchen panel", "0.2.44", 0, 81.2, 39, 31, "active", 3),
            ("demo-lakehouse", "Lake house", "Entry panel", "0.2.43", 0, 44.7, 29, 18, "active", 18),
            ("demo-workshop", "The workshop", "Office panel", "0.2.43", 1440, 50.3, 34, 27, "active", 2),
        ]
        with store.connect(directory) as db:
            for panel_id, site, name, version, age, temp, mem, disk, service, days in fixtures:
                health = {"schema": 1, "version": version, "uptime_s": days * 86400 + 1260,
                          "temperature_c": temp, "memory_used_pct": mem, "disk_used_pct": disk,
                          "load_1m": 0.31, "panel_service": service}
                db.execute("INSERT INTO panels VALUES(?,?,?,?,?,?,?,?,?)",
                           (panel_id, site, name, "", now - days * 86400, None, now - age, now - age, protocol.encode(health).decode()))
                store.event(db, panel_id, "First health check received", "Sample panel", now - days * 86400)
                store.event(db, panel_id, "Panel enrolled", "Preview operator", now - days * 86400 - 300)
        # Keep online fixture times fresh; offline fixture remains offline.
        @app.before_request
        def refresh_fixtures():
            with store.connect(directory) as db:
                db.execute("UPDATE panels SET last_seen=? WHERE id!='demo-workshop'", (int(time.time()),))

        app.run(host="127.0.0.1", port=18790, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

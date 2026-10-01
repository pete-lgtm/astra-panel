"""Server-local enrollment and revocation. Never print device credentials."""
import argparse
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

import store


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.environ.get("ASTRA_DATA_DIR", "var"))
    sub = parser.add_subparsers(dest="command", required=True)
    enroll = sub.add_parser("enroll")
    enroll.add_argument("--site", required=True)
    enroll.add_argument("--name", required=True)
    enroll.add_argument("--origin", required=True, help="Your deployed portal HTTPS origin")
    enroll.add_argument("--output", required=True, help="New private enrollment JSON file")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("panel_id")
    sub.add_parser("list")
    sub.add_parser("prune", help="Remove audit history older than 90 days")
    args = parser.parse_args()
    store.initialize(args.data_dir)
    with store.connect(args.data_dir) as db:
        if args.command == "enroll":
            for value in (args.site, args.name):
                if not value.strip() or len(value) > 80 or any(ord(c) < 32 for c in value):
                    parser.error("Site and name must be 1–80 printable characters.")
            origin = urlsplit(args.origin)
            if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment or origin.path not in {"", "/"}:
                parser.error("Origin must be an HTTPS origin without credentials or path.")
            panel_id = "astra-" + secrets.token_hex(8)
            secret = secrets.token_hex(32)
            output = Path(args.output)
            output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            # Exclusive file creation; repeat enrollment cannot overwrite another panel.
            with output.open("x", encoding="utf-8") as file:
                json.dump({"panel_id": panel_id, "secret": secret,
                           "url": args.origin.rstrip("/") + "/api/v1/heartbeat"}, file)
                file.write("\n")
            try:
                db.execute("INSERT INTO panels(id,site,name,secret,enrolled_at) VALUES(?,?,?,?,?)",
                           (panel_id, args.site.strip(), args.name.strip(), secret, int(time.time())))
                store.event(db, panel_id, "Panel enrolled", "Server administrator")
                db.commit()
            except Exception:
                output.unlink(missing_ok=True)
                raise
            print("Enrolled " + panel_id + ". Private enrollment file created; transfer it directly to this panel.")
        elif args.command == "revoke":
            cursor = db.execute("UPDATE panels SET revoked_at=?, secret='' WHERE id=? AND revoked_at IS NULL", (int(time.time()), args.panel_id))
            if cursor.rowcount != 1:
                parser.error("Panel does not exist or is already revoked.")
            store.event(db, args.panel_id, "Device credential revoked", "Server administrator")
            print("Device revoked. Further health check-ins will be rejected.")
        elif args.command == "list":
            for row in db.execute("SELECT id,site,name,revoked_at FROM panels ORDER BY site"):
                print(row["id"], row["site"], row["name"], "revoked" if row["revoked_at"] else "enrolled", sep=" | ")
        elif args.command == "prune":
            db.execute("DELETE FROM audit WHERE created_at < ?", (int(time.time()) - 90 * 86400,))
            db.execute("DELETE FROM nonces WHERE seen_at < ?", (int(time.time()) - 600,))
            print("Expired audit and nonce records removed.")


if __name__ == "__main__":
    main()

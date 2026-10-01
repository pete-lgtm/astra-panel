"""Independent local screen stays available while the keypad service restarts."""
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get('KEYPAD_RECOVERY_PORT', '8764'))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{PORT}':
            self.send_error(403)
            return
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/preview': ('index.html', 'text/html; charset=utf-8'),
                 '/recovery.js': ('recovery.js', 'text/javascript'),
                 '/aurora.js': ('aurora.js', 'text/javascript'),
                 '/recovery.css': ('recovery.css', 'text/css'),
                 }
        if self.path not in files:
            self.send_error(404)
            return
        name, kind = files[self.path]
        body = (ROOT / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src http://127.0.0.1:8765; frame-src http://127.0.0.1:8765; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()

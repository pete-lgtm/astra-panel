"""Short-lived HTTPS-only phone handoff. Never expose the keypad admin server."""
import json
import secrets
import threading
import time
import ssl
import ipaddress
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from qrcodegen import QrCode


def qr_svg(text):
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    n = qr.get_size()
    squares = ' '.join(f'M{x+4},{y+4}h1v1h-1z' for y in range(n) for x in range(n) if qr.get_module(x,y))
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n+8} {n+8}"><path fill="white" d="M0 0h{n+8}v{n+8}H0z"/><path fill="black" d="{squares}"/></svg>'


class Pairing:
    def __init__(self, connect, origin='', bind='', cert='', key='', clock=time.monotonic):
        self.connect, self.origin, self.bind = connect, origin, bind
        self.cert, self.key, self.clock = cert, key, clock
        self.lock = threading.RLock()
        self.current = None
        self.httpd = None

    def available(self):
        parts = urlsplit(self.origin)
        try:
            address = ipaddress.ip_address(self.bind)
            return bool(parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password and
                        not parts.path and not parts.query and not parts.fragment and
                        (address.is_private or address.is_loopback) and not address.is_unspecified and
                        Path(self.cert).is_file() and Path(self.key).is_file())
        except ValueError:
            return False

    def start_listener(self):
        if self.httpd:
            return
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert, self.key)
        manager = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def setup(self):
                super().setup()
                self.connection.settimeout(10)
            def reply(self, status, data, kind='application/json'):
                body = json.dumps(data).encode() if kind == 'application/json' else data
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(body)
            def trusted(self):
                return self.headers.get('Host') == urlsplit(manager.origin).netloc
            def do_GET(self):
                if not self.trusted(): return self.reply(403, {'error':'Invalid address.'})
                if manager.status()['state'] != 'waiting': return self.reply(410, {'error':'Pairing closed. Start again on the keypad.'})
                files = {'/':('phone_setup.html','text/html; charset=utf-8'), '/phone.js':('phone_setup.js','text/javascript'), '/phone.css':('phone_setup.css','text/css')}
                if self.path not in files: return self.reply(404, {})
                name,kind=files[self.path]
                self.reply(200, Path(__file__).with_name(name).read_bytes(),kind)
            def do_POST(self):
                if not self.trusted() or self.path != '/submit' or self.headers.get('Origin') != manager.origin:
                    return self.reply(403, {'error':'Invalid pairing request.'})
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0 < size <= 8192 or self.headers.get('Content-Type') != 'application/json': raise ValueError()
                    data=json.loads(self.rfile.read(size))
                    if not isinstance(data,dict) or set(data)!={'key'}: raise ValueError()
                    manager.submit(self.headers.get('Authorization','').removeprefix('Bearer '), data['key'])
                    self.reply(200, {'saved':True})
                except (ValueError,TypeError):
                    self.reply(400, {'error':'Could not connect. Check the key or create a new QR code on the keypad.'})
                except Exception:
                    self.reply(500, {'error':'Connection could not be saved. Try again from the keypad.'})
        parts=urlsplit(self.origin)
        server=ThreadingHTTPServer((self.bind,parts.port or 443),Handler)
        server.daemon_threads=True
        server.socket=context.wrap_socket(server.socket,server_side=True)
        self.httpd=server
        threading.Thread(target=server.serve_forever,daemon=True).start()

    def begin(self, host):
        if not self.available():
            raise ValueError('Phone setup needs a trusted HTTPS address configured by your installer. You can still paste the key on this keypad.')
        with self.lock:
            self.start_listener()
            token=secrets.token_urlsafe(32)
            self.current={'token':token,'host':host,'expires':self.clock()+300,'state':'waiting'}
            url=self.origin+'/#'+token
            timer=threading.Timer(300,self.expire,args=(token,))
            timer.daemon=True
            timer.start()
            return {'url':url,'qr':qr_svg(url),'expiresIn':300}

    def expire(self, token):
        with self.lock:
            if self.current and self.current['token']==token:
                self.current['token']=''
                if self.current['state']=='waiting': self.current['state']='expired'
                self.close_listener()

    def close_listener(self):
        server,self.httpd=self.httpd,None
        if server:
            def close():
                server.shutdown()
                server.server_close()
            threading.Thread(target=close,daemon=True).start()

    def cancel(self):
        with self.lock:
            self.current=None
            self.close_listener()
        return {'state':'cancelled'}

    def status(self):
        with self.lock:
            if not self.current: return {'state':'idle'}
            if self.current['state']=='waiting' and self.clock()>=self.current['expires']:
                self.current['state']='expired'
                self.current['token']=''
                self.close_listener()
            return {'state':self.current['state']}

    def submit(self, token, key):
        with self.lock:
            if (self.status()['state']!='waiting' or not isinstance(token,str) or
                    not secrets.compare_digest(token,self.current['token'])):
                raise ValueError('Pairing expired or invalid.')
            # A submission consumes this link even if UniFi rejects it.
            self.current['token']=''
            self.current['state']='failed'
            try:
                self.connect(self.current['host'],key)
                self.current['state']='saved'
            finally:
                self.close_listener()

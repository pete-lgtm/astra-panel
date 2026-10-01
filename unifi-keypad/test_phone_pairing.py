import json
import ssl
import socket
import subprocess
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
from phone_pairing import Pairing, qr_svg


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.connect=Mock()
        self.pair=Pairing(self.connect,origin='https://setup.example.test',clock=lambda:self.now)
        self.available=patch.object(self.pair,'available',return_value=True)
        self.listener=patch.object(self.pair,'start_listener')
        self.available.start(); self.listener.start()
        self.addCleanup(self.available.stop); self.addCleanup(self.listener.stop)
        self.addCleanup(self.pair.cancel)

    def begin(self):
        return self.pair.begin('192.168.0.1')['url'].split('#')[1]

    def test_one_use_and_no_key_in_status_or_qr(self):
        token=self.begin()
        self.pair.submit(token,'synthetic-api-key')
        self.connect.assert_called_once_with('192.168.0.1','synthetic-api-key')
        self.assertEqual(self.pair.status(),{'state':'saved'})
        with self.assertRaises(ValueError): self.pair.submit(token,'second-key')
        self.assertEqual(self.connect.call_count,1)

    def test_expiry_cancel_and_replacement(self):
        token=self.begin(); newer=self.begin()
        with self.assertRaises(ValueError): self.pair.submit(token,'key')
        self.pair.cancel()
        with self.assertRaises(ValueError): self.pair.submit(newer,'key')
        token=self.begin(); self.now=301
        with self.assertRaises(ValueError): self.pair.submit(token,'key')
        self.connect.assert_not_called()

    def test_invalid_token_does_not_consume_valid_pairing(self):
        token=self.begin()
        with self.assertRaises(ValueError): self.pair.submit('incorrect','key')
        self.assertEqual(self.pair.status()['state'],'waiting')
        self.pair.submit(token,'key')

    def test_failed_connection_consumes_link(self):
        self.connect.side_effect=ValueError('rejected')
        token=self.begin()
        with self.assertRaises(ValueError): self.pair.submit(token,'key')
        self.assertEqual(self.pair.status()['state'],'failed')
        with self.assertRaises(ValueError): self.pair.submit(token,'key')
        self.assertEqual(self.connect.call_count,1)

    def test_default_transport_is_disabled_and_qr_is_local(self):
        pair=Pairing(self.connect)
        self.assertFalse(pair.available())
        with self.assertRaises(ValueError): pair.begin('192.168.0.1')
        svg=qr_svg('https://example.test/#random-pair-token')
        self.assertIn('<svg',svg)
        self.assertNotIn('random-pair-token',svg)


class HttpsPairingTests(unittest.TestCase):
    def test_real_https_submission_and_origin_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            cert=Path(directory)/'cert.pem';key=Path(directory)/'key.pem'
            conf=Path(directory)/'openssl.conf'
            conf.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\nbasicConstraints=critical,CA:TRUE\n')
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-config',str(conf),'-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            origin=f'https://localhost:{port}'
            connect=Mock()
            pair=Pairing(connect,origin,'127.0.0.1',str(cert),str(key))
            context=ssl.create_default_context(cafile=str(cert))
            try:
                link=pair.begin('192.168.0.1')['url']
                with urllib.request.urlopen(origin+'/',context=context) as response:
                    self.assertIn(b'UniFi API key',response.read())
                def post(allowed):
                    req=urllib.request.Request(origin+'/submit',data=json.dumps({'key':'test-only-api-key'}).encode(),headers={'Content-Type':'application/json','Origin':origin if allowed else 'https://wrong.test','Authorization':'Bearer '+link.split('#')[1]})
                    return urllib.request.urlopen(req,context=context)
                with self.assertRaises(urllib.error.HTTPError): post(False)
                connect.assert_not_called()
                with post(True) as response: self.assertEqual(json.load(response),{'saved':True})
                connect.assert_called_once_with('192.168.0.1','test-only-api-key')
            finally: pair.cancel()


if __name__=='__main__': unittest.main()

import base64
import io
import socket
import unittest
from unittest.mock import Mock, patch

import sonos_artwork as art

PLAYER = {'ip': '192.168.0.27', 'id': 'RINCON_AAAAAAAAAAAA01400'}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')


def response(status=200, data=PNG, **headers):
    r = Mock(status=status)
    headers = {'Content-Type': 'image/png', **headers}
    r.getheader.side_effect = lambda key, default=None: headers.get(key, default)
    r.read.side_effect = io.BytesIO(data).read
    return r


class ArtworkTests(unittest.TestCase):
    def test_only_reporting_player_getaa_or_standard_public_image_urls(self):
        relative = art.source('/getaa?s=1&u=track', PLAYER)
        self.assertTrue(relative['local'])
        self.assertEqual(relative['url'], 'http://192.168.0.27:1400/getaa?s=1&u=track')
        self.assertEqual(relative, art.source(relative['url'], PLAYER))
        self.assertFalse(art.source('https://images.example.org/cover.png', PLAYER)['local'])
        for uri in ('/xml/device_description.xml', '/getaa', '//evil.example/getaa',
                    'http://192.168.0.28:1400/getaa?u=x', 'http://192.168.0.27:1400/reboot',
                    'http://127.0.0.1/cover', 'http://169.254.169.254/latest/meta-data', 'http://224.0.0.1/cover',
                    'http://localhost/cover', 'https://user:password@example.org/image',
                    'https://example.org:8080/image', 'https://example.org/image#fragment',
                    'https://example.org/a\r\nb', 'https://example.org\\@127.0.0.1/image',
                    'data:image/png;base64,AA==', 'file:///etc/passwd', 'x'*4097):
            self.assertIsNone(art.source(uri, PLAYER), uri)

    def test_only_bounded_raster_images(self):
        self.assertTrue(art.image_data(PNG, 'image/png').startswith('data:image/png;base64,'))
        for data, kind in ((b'<svg/>', 'image/svg+xml'), (b'<html/>', 'image/png'), (PNG, 'text/html'),
                           (PNG + b'a'*art.MAX_IMAGE, 'image/png')):
            with self.assertRaises(ValueError):
                art.image_data(data, kind)

    def test_local_fetch_exact_endpoint_without_dns_or_credentials(self):
        with patch.object(art.http.client, 'HTTPConnection') as connection, patch.object(art.socket, 'getaddrinfo') as dns:
            conn = connection.return_value
            conn.getresponse.return_value = response()
            self.assertTrue(art.fetch(art.source('/getaa?u=track', PLAYER)).startswith('data:image/png;'))
            dns.assert_not_called()
            self.assertEqual(conn.request.call_args.args, ('GET', '/getaa?u=track'))
            self.assertNotIn('Cookie', conn.request.call_args.kwargs['headers'])
            conn.close.assert_called_once()

    def test_public_https_pins_dns_but_retains_original_hostname_for_tls(self):
        with patch.object(art.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]), \
                patch.object(art.socket, 'create_connection') as connect, patch.object(art.http.client, 'HTTPSConnection') as connection:
            connection.return_value.getresponse.return_value = response()
            art.fetch(art.source('https://images.example.org/image.png', PLAYER))
            connection.assert_called_once_with('images.example.org', 443, timeout=2)
            connection.return_value._create_connection(('images.example.org', 443), 2)
            connect.assert_called_once_with(('8.8.8.8', 443), 2, None)

    def test_dns_private_mixed_or_multicast_is_not_contacted(self):
        for addresses in (['127.0.0.1'], ['192.168.0.1'], ['169.254.169.254'], ['8.8.8.8', '10.0.0.1'], ['224.0.0.1']):
            result = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443)) for a in addresses]
            with patch.object(art.socket, 'getaddrinfo', return_value=result), patch.object(art.http.client, 'HTTPSConnection') as connection:
                with self.assertRaises(ValueError):
                    art.fetch(art.source('https://images.example.org/art', PLAYER))
                connection.assert_not_called()

    def test_redirect_cannot_access_another_local_service(self):
        with patch.object(art.http.client, 'HTTPConnection') as connection:
            connection.return_value.getresponse.return_value = response(302, **{'Location': 'http://192.168.0.1/private'})
            with self.assertRaises(ValueError):
                art.fetch(art.source('/getaa?u=track', PLAYER))
            self.assertEqual(connection.call_count, 1)

    def test_size_and_content_type_limits_also_apply_without_content_length(self):
        for r in (response(data=PNG+b'x'*art.MAX_IMAGE), response(**{'Content-Length': str(art.MAX_IMAGE+1)}),
                  response(data=b'<svg/>', **{'Content-Type': 'image/svg+xml'})):
            with patch.object(art.http.client, 'HTTPConnection') as connection:
                connection.return_value.getresponse.return_value = r
                with self.assertRaises(ValueError):
                    art.fetch(art.source('/getaa?u=track', PLAYER))
                connection.return_value.close.assert_called_once()

    def test_cache_is_only_for_current_sources_and_caches_failures_briefly(self):
        cache = art.ArtworkCache()
        item = art.source('/getaa?u=track', PLAYER)
        key = art.identifier(item)
        verify = Mock()
        with patch.object(art, 'fetch', return_value='image') as fetch:
            self.assertIsNone(cache.get(key, verify));verify.assert_not_called()
            cache.replace({key: item})
            self.assertEqual(cache.get(key, verify), 'image')
            self.assertEqual(cache.get(key, verify), 'image')
            verify.assert_called_once_with(item);fetch.assert_called_once_with(item)
            cache.replace({});self.assertIsNone(cache.get(key, verify));self.assertFalse(cache.images)
        cache.replace({key: item})
        with patch.object(art, 'fetch', side_effect=OSError('offline')) as fetch:
            self.assertIsNone(cache.get(key, verify));self.assertIsNone(cache.get(key, verify))
            fetch.assert_called_once()

    def test_late_artwork_discarded_after_song_or_network_changes(self):
        cache = art.ArtworkCache()
        item = art.source('/getaa?u=track', PLAYER);key = art.identifier(item)
        cache.replace({key: item})
        def fetch(_):
            cache.replace({})
            return 'old cover'
        with patch.object(art, 'fetch', side_effect=fetch):
            self.assertIsNone(cache.get(key, lambda _: None))
            self.assertFalse(cache.images)

    def test_cover_cache_has_a_memory_bound(self):
        cache = art.ArtworkCache()
        items = [art.source('/getaa?u='+str(i), PLAYER) for i in range(20)]
        cache.replace({art.identifier(item): item for item in items})
        with patch.object(art, 'fetch', return_value='image'):
            for item in items:
                cache.get(art.identifier(item), lambda _: None)
        self.assertEqual(len(cache.images), 12)


if __name__ == '__main__':
    unittest.main()

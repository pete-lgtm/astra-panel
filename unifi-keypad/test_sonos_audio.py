import ipaddress
import unittest
from xml.sax.saxutils import escape
from unittest.mock import patch
from pathlib import Path
import socket
import struct

import sonos_audio as audio

A, B, C = ('RINCON_' + c * 12 + '01400' for c in 'ABC')
NETS = [ipaddress.ip_network('192.168.0.0/24')]


def description(uid=A, model='Sonos Amp'):
    return audio.xml(f'<root><device><deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType>'
                     f'<manufacturer>Sonos, Inc.</manufacturer><UDN>uuid:{uid}</UDN><roomName>Family Room</roomName>'
                     f'<modelName>{model}</modelName></device></root>')


def groups():
    return audio.xml('<root><ZoneGroupState>' +
                     '&lt;ZoneGroups&gt;&lt;ZoneGroup Coordinator="' + A + '"&gt;' +
                     ''.join('&lt;ZoneGroupMember UUID="' + uid + '" ZoneName="' + name +
                             '" Location="http://192.168.0.' + ip + ':1400/xml/device_description.xml"' + invisible + '/&gt;'
                             for uid, name, ip, invisible in [(A, 'Family Room', '27', ''), (B, 'Kitchen', '28', ''),
                                                              (C, 'Sub', '29', ' Invisible="1"')]) +
                     '&lt;/ZoneGroup&gt;&lt;/ZoneGroups&gt;</ZoneGroupState></root>')


class AudioTests(unittest.TestCase):
    def test_interface_read_works_without_netlink_and_skips_unaddressed_nics(self):
        devices = {'eth0': None, 'wlan0': ('192.168.0.21', '255.255.254.0', 1),
                   'lo': ('127.0.0.1', '255.0.0.0', 1), 'down0': ('10.0.0.2', '255.255.255.0', 0),
                   'public0': ('8.8.8.8', '255.255.255.0', 1), 'badmask': ('192.168.0.2', '255.0.255.0', 1)}
        calls = []
        def ioctl(fd, op, request):
            name = request[:16].split(b'\0')[0].decode()
            calls.append((name, op))
            self.assertIn(op, (0x8913, 0x8915, 0x891b), 'Only GET operations are allowed')
            dev = devices[name]
            if dev is None:
                raise OSError('No address assigned')
            result = bytearray(request)
            if op == 0x8913:
                struct.pack_into('H', result, 16, dev[2])
            else:
                struct.pack_into('H', result, 16, socket.AF_INET)
                result[20:24] = socket.inet_aton(dev[0] if op == 0x8915 else dev[1])
            return bytes(result)
        with patch.object(audio.sys, 'platform', 'linux'), \
                patch.object(audio.Path, 'iterdir', return_value=[Path('/sys/class/net') / n for n in devices]), \
                patch.object(audio.socket, 'socket') as make_socket, patch.object(audio.fcntl, 'ioctl', side_effect=ioctl):
            self.assertEqual(list(map(str, audio.interfaces())), ['192.168.0.21/23'])
            make_socket.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        self.assertNotIn(('down0', 0x8915), calls)

    def test_interface_list_absent_or_socket_failure_returns_unavailable(self):
        with patch.object(audio.sys, 'platform', 'linux'), patch.object(audio.Path, 'iterdir', side_effect=OSError('Unavailable')):
            self.assertEqual(audio.interfaces(), [])
        with patch.object(audio.sys, 'platform', 'linux'), patch.object(audio.Path, 'iterdir', return_value=[Path('wlan0')]), \
                patch.object(audio.socket, 'socket', side_effect=OSError('Unavailable')):
            self.assertEqual(audio.interfaces(), [])

    def test_local_targets_only_no_redirect_urls_credentials_or_public_networks(self):
        self.assertEqual(audio.location_ip('http://192.168.0.27:1400/xml/device_description.xml', NETS), '192.168.0.27')
        for uri in ('http://127.0.0.1:1400/xml/device_description.xml', 'http://192.168.1.27:1400/xml/device_description.xml',
                    'http://192.168.0.27:80/xml/device_description.xml', 'http://user:pass@192.168.0.27:1400/xml/device_description.xml',
                    'http://example.com:1400/xml/device_description.xml', 'http://192.168.0.27:1400/private',
                    'http://192.168.0.27:1400/xml/device_description.xml?next=public', 'http://8.8.8.8:1400/xml/device_description.xml'):
            self.assertIsNone(audio.location_ip(uri, NETS), uri)
        with patch.object(audio.http.client, 'HTTPConnection') as connection:
            for action in ('Play', 'SetVolume', 'Reboot', 'JoinGroup'):
                with self.assertRaises(ValueError):
                    audio.request('192.168.0.27', NETS, action)
            connection.assert_not_called()

    def test_rejects_entities_oversize_and_malformed_xml(self):
        for raw in ('<!DOCTYPE test><root/>', '<!ENTITY x "y"><root/>', '<root>', 'x'*262145):
            with self.assertRaises(ValueError):
                audio.xml(raw)

    def test_group_tracks_come_from_coordinator_and_sub_is_not_a_zone(self):
        reader = audio.AudioInventory()
        seen = []
        def request(ip, nets, action=None):
            seen.append((ip, action))
            if action is None:
                return description()
            if action == 'GetZoneGroupState':
                return groups()
            if action == 'GetTransportInfo':
                return audio.xml('<r><CurrentTransportState>PLAYING</CurrentTransportState></r>')
            return audio.xml('<r><TrackURI>x-sonos-htastream:tv</TrackURI></r>')
        with patch.object(audio, 'interfaces', return_value=[ipaddress.ip_interface('192.168.0.21/24')]), \
                patch.object(audio, 'discover', return_value={A: {'id': A, 'ip': '192.168.0.27', 'name': 'Family Room'}}), \
                patch.object(audio, 'request', side_effect=request):
            result = reader.read()
            self.assertEqual([z['name'] for z in result['zones']], ['Family Room', 'Kitchen'])
            for zone in result['zones']:
                self.assertEqual((zone['title'], zone['playback'], zone['source']), ('TV audio', 'playing', 'TV'))
                self.assertEqual(zone['groupRooms'], ['Family Room', 'Kitchen'])
            self.assertFalse(any(ip != '192.168.0.27' for ip, action in seen if action in ('GetPositionInfo', 'GetTransportInfo', 'GetCurrentTransportActions')))
            result['zones'].clear()
            self.assertEqual(len(reader.read()['zones']), 2, 'Returned cache is detached from internal state')

    def test_bad_identity_and_offline_do_not_return_stale_music(self):
        with patch.object(audio, 'request', return_value=description(B)) as request:
            result = audio.playback({'id': A, 'ip': '192.168.0.27'}, NETS)
            self.assertEqual(result['playback'], 'offline')
            self.assertEqual(result['title'], '')
            self.assertEqual(request.call_count, 1)

    def test_track_metadata_plain_text_and_radio(self):
        position = audio.xml('<r><TrackMetaData>&lt;DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"&gt;'
                             '&lt;item&gt;&lt;dc:title&gt;A &amp;amp; B&lt;/dc:title&gt;'
                             '&lt;dc:creator&gt;Artist&lt;/dc:creator&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;</TrackMetaData></r>')
        self.assertEqual(audio.metadata(position)['title'], 'A & B')
        self.assertEqual(audio.metadata(position)['artist'], 'Artist')

    def test_failed_topology_retains_room_as_unavailable(self):
        reader = audio.AudioInventory()
        with patch.object(audio, 'interfaces', return_value=[ipaddress.ip_interface('192.168.0.21/24')]), \
                patch.object(audio, 'discover', return_value={A: {'id': A, 'ip': '192.168.0.27', 'name': 'Family Room'}}), \
                patch.object(audio, 'request', side_effect=ValueError('offline')):
            self.assertEqual(reader.read()['zones'][0]['playback'], 'offline')

    def test_group_artwork_comes_from_coordinator_and_raw_urls_stay_private(self):
        reader = audio.AudioInventory()
        def request(ip, nets, action=None):
            if action is None: return description()
            if action == 'GetZoneGroupState': return groups()
            if action == 'GetTransportInfo': return audio.xml('<r><CurrentTransportState>PLAYING</CurrentTransportState></r>')
            didl = '<DIDL-Lite><item><title>Song</title><albumArtURI>/getaa?u=private-track&amp;v=2</albumArtURI></item></DIDL-Lite>'
            return audio.xml('<r><TrackURI>x-sonos:track</TrackURI><TrackMetaData>'+escape(didl)+'</TrackMetaData></r>')
        with patch.object(audio, 'interfaces', return_value=[ipaddress.ip_interface('192.168.0.21/24')]), \
                patch.object(audio, 'discover', return_value={A: {'id': A, 'ip': '192.168.0.27', 'name': 'Family Room'}}), \
                patch.object(audio, 'request', side_effect=request):
            result = reader.read()
            self.assertEqual(result['zones'][0]['artworkId'], result['zones'][1]['artworkId'])
            self.assertEqual(len(result['zones'][0]['artworkId']), 64)
            self.assertEqual(len(reader.covers.sources), 1)
            self.assertNotIn('private-track', str(result))
            self.assertNotIn('_artwork', str(result))

    def test_tv_clears_music_artwork_returned_in_stale_metadata(self):
        didl = '<DIDL-Lite><item><title>Old song</title><albumArtURI>/getaa?u=old</albumArtURI></item></DIDL-Lite>'
        position = audio.xml('<r><TrackURI>x-sonos-htastream:tv</TrackURI><TrackMetaData>'+escape(didl)+'</TrackMetaData></r>')
        with patch.object(audio, 'request', side_effect=[description(), audio.xml('<r><CurrentTransportState>PLAYING</CurrentTransportState></r>'), position]):
            result = audio.playback({'id': A, 'ip': '192.168.0.27'}, NETS)
            self.assertEqual(result['title'], 'TV audio')
            self.assertIsNone(result['_artwork'])

    def test_airplay_and_spotify_keep_song_metadata_and_artwork(self):
        for suffix, source in (('airplay:', 'AirPlay'), ('spotify:', 'Spotify Connect'), ('other:', 'Music')):
            didl = '<DIDL-Lite><item><title>A song</title><creator>An artist</creator><album>An album</album><albumArtURI>/getaa?u=current</albumArtURI></item></DIDL-Lite>'
            position = audio.xml('<r><TrackURI>x-sonos-vli:device,'+suffix+'</TrackURI><TrackMetaData>'+escape(didl)+'</TrackMetaData></r>')
            with patch.object(audio, 'request', side_effect=[description(), audio.xml('<r><CurrentTransportState>PLAYING</CurrentTransportState></r>'), position,
                                                           audio.xml('<r><Actions>Set, Stop, Pause, Play, Next</Actions></r>')]):
                result = audio.playback({'id': A, 'ip': '192.168.0.27'}, NETS)
                self.assertEqual((result['source'], result['title'], result['artist']), (source, 'A song', 'An artist'))
                self.assertTrue(result['_artwork'])
                self.assertEqual(result['transportActions'], ['pause', 'play', 'stop'])

    def test_volume_and_mute_failures_do_not_hide_playback(self):
        with patch.object(audio, 'request', side_effect=[description(), audio.xml('<r><CurrentVolume>57</CurrentVolume></r>'),
                         audio.xml('<r><CurrentMute>1</CurrentMute></r>'), ValueError('Not supported')]):
            levels = audio.room_levels({'id': A, 'ip': '192.168.0.27'}, NETS)
            self.assertEqual(levels, {'volume': 57, 'muted': True, 'fixedVolume': False})

    def test_controls_route_playback_to_group_and_volume_to_selected_room(self):
        reader = audio.AudioInventory()
        reader.players = {B: {'id': B, 'ip': '192.168.0.28'}}
        authorized = []
        with patch.object(audio, 'interfaces', return_value=[ipaddress.ip_interface('192.168.0.21/24')]), \
                patch.object(audio, 'identity', return_value={'id': B}), patch.object(audio, 'request', return_value=groups()), \
                patch.object(audio, 'playback', return_value={'transportActions': ['play', 'pause', 'stop']}), \
                patch.object(audio, 'room_levels', return_value={'volume': 57, 'muted': False, 'fixedVolume': False}), \
                patch.object(audio, 'control_request') as write:
            for action, extras, target, setting in [('pause', {}, '192.168.0.27', None), ('play', {}, '192.168.0.27', None),
                 ('stop', {}, '192.168.0.27', None), ('volume_down', {}, '192.168.0.28', None),
                 ('volume', {'volume': 52}, '192.168.0.28', 52), ('mute', {'muted': True}, '192.168.0.28', True)]:
                result = reader.control({'roomId': B, 'groupId': A, 'action': action, **extras}, lambda: authorized.append(True))
                self.assertTrue(result['accepted'])
                self.assertEqual(write.call_args.args, (target, NETS, action, setting))
            self.assertEqual(len(authorized), 12)

    def test_control_rejects_unknown_rooms_changed_groups_and_unsupported_source(self):
        reader = audio.AudioInventory()
        reader.players = {B: {'id': B, 'ip': '192.168.0.28'}}
        with patch.object(audio, 'interfaces', return_value=[ipaddress.ip_interface('192.168.0.21/24')]), \
                patch.object(audio, 'identity', return_value={'id': B}), patch.object(audio, 'request', return_value=groups()), \
                patch.object(audio, 'playback', return_value={'transportActions': []}), patch.object(audio, 'control_request') as write:
            for data in ({'roomId': A, 'groupId': A, 'action': 'play'}, {'roomId': B, 'groupId': B, 'action': 'play'},
                         {'roomId': B, 'groupId': A, 'action': 'play'}):
                with self.assertRaises(ValueError): reader.control(data, lambda: None)
            write.assert_not_called()

    def test_control_rechecks_authorization_after_reads_and_never_retries(self):
        reader = audio.AudioInventory();reader.players = {B: {'id': B, 'ip': '192.168.0.28'}}
        with patch.object(audio, 'interfaces', return_value=[ipaddress.ip_interface('192.168.0.21/24')]), \
                patch.object(audio, 'identity', return_value={'id': B}), patch.object(audio, 'request', return_value=groups()), \
                patch.object(audio, 'playback', return_value={'transportActions': ['pause']}), patch.object(audio, 'control_request') as write:
            with self.assertRaisesRegex(ValueError, 'locked'):
                from unittest.mock import Mock
                reader.control({'roomId': B, 'groupId': A, 'action': 'pause'}, Mock(side_effect=[None, ValueError('locked')]))
            write.assert_not_called()
            write.side_effect = ValueError('Unknown result')
            with self.assertRaises(ValueError):
                reader.control({'roomId': B, 'groupId': A, 'action': 'pause'}, lambda: None)
            write.assert_called_once()
            self.assertEqual(reader.last_read, -1000)

    def test_fixed_room_volume_and_malformed_commands_never_write(self):
        reader = audio.AudioInventory();reader.players = {B: {'id': B, 'ip': '192.168.0.28'}}
        with patch.object(audio, 'interfaces', return_value=[ipaddress.ip_interface('192.168.0.21/24')]), \
                patch.object(audio, 'identity', return_value={'id': B}), patch.object(audio, 'request', return_value=groups()), \
                patch.object(audio, 'room_levels', return_value={'volume': 100, 'fixedVolume': True, 'muted': False}), \
                patch.object(audio, 'control_request') as write:
            for extra in ({'action': 'volume_up'}, {'action': 'volume', 'volume': 101}, {'action': 'volume', 'volume': True},
                          {'action': 'volume', 'volume': -1}, {'action': 'mute', 'muted': 'false'}, {'action': 'reboot'},
                          {'action': 'play', 'ip': '192.168.0.1'}, {'action': 'play', 'volume': 50}):
                with self.assertRaises(ValueError):
                    reader.control({'roomId': B, 'groupId': A, **extra}, lambda: None)
            write.assert_not_called()

    def test_wire_commands_use_only_validated_arguments_and_fixed_endpoints(self):
        with patch.object(audio, 'exchange') as exchange:
            for action, setting, soap, args in (
                    ('play', None, 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>'),
                    ('pause', None, 'Pause', '<InstanceID>0</InstanceID>'),
                    ('stop', None, 'Stop', '<InstanceID>0</InstanceID>'),
                    ('volume_up', None, 'SetRelativeVolume', '<InstanceID>0</InstanceID><Channel>Master</Channel><Adjustment>2</Adjustment>'),
                    ('volume', 54, 'SetVolume', '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>54</DesiredVolume>'),
                    ('mute', True, 'SetMute', '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>1</DesiredMute>')):
                audio.control_request('192.168.0.27', NETS, action, setting)
                call = exchange.call_args
                service = 'AVTransport' if action in ('play', 'pause', 'stop') else 'RenderingControl'
                self.assertEqual(call.args, ('192.168.0.27', NETS, soap, (service, '/MediaRenderer/'+service+'/Control', args)))
                self.assertTrue(call.kwargs['writing'])
            before = exchange.call_count
            for action, setting in [('Play', None), ('volume', 101), ('volume', True), ('mute', 1), ('pause', 'malicious')]:
                with self.assertRaises(ValueError): audio.control_request('192.168.0.27', NETS, action, setting)
            self.assertEqual(exchange.call_count, before)

    def test_network_change_drops_previous_home(self):
        reader = audio.AudioInventory()
        reader.players = {A: {'id': A, 'ip': '192.168.0.27', 'name': 'Private room'}}
        reader.networks = ('192.168.0.21/24',)
        with patch.object(audio, 'interfaces', return_value=[]):
            result = reader.read()
            self.assertEqual(result['state'], 'unavailable')
            self.assertEqual(result['zones'], [])

    def test_control_requires_matching_soap_confirmation_and_never_retries_transport(self):
        with patch.object(audio.http.client, 'HTTPConnection') as make_connection:
            connection = make_connection.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'<Envelope><Body><PauseResponse/></Body></Envelope>'
            audio.control_request('192.168.0.27', NETS, 'pause')
            connection.request.assert_called_once()
            connection.close.assert_called_once()
            for body in (b'<Envelope><Body><Fault/></Body></Envelope>', b'<PlayResponse/>', b'<html/>'):
                connection.reset_mock();response.read.return_value = body
                with self.assertRaisesRegex(ValueError, 'did not confirm'):
                    audio.control_request('192.168.0.27', NETS, 'pause')
                connection.request.assert_called_once()
                connection.close.assert_called_once()
            connection.reset_mock();connection.getresponse.side_effect = TimeoutError()
            with self.assertRaisesRegex(ValueError, 'unknown'):
                audio.control_request('192.168.0.27', NETS, 'pause')
            connection.request.assert_called_once()
            connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()

import copy
import ipaddress
import unittest
from unittest.mock import patch, Mock
from xml.sax.saxutils import escape

import sonos_audio as audio

A, B, C, D, E, F, H = ['RINCON_' + c * 12 + '01400' for c in 'ABCDEFH']
NETS = [ipaddress.ip_network('192.168.0.0/24')]
PLAYERS = {uid: {'id': uid, 'name': 'Room ' + str(i), 'ip': '192.168.0.' + str(i + 10)}
           for i, uid in enumerate((A, B, C, D, E, F, H))}


def topology_response(shape):
    groups = '<ZoneGroups>'
    for leader, members in shape.items():
        groups += '<ZoneGroup Coordinator="' + leader + '">'
        for uid in members:
            p = PLAYERS[uid]
            groups += f'<ZoneGroupMember UUID="{uid}" ZoneName="{p["name"]}" Location="http://{p["ip"]}:1400/xml/device_description.xml"/>'
        groups += f'<ZoneGroupMember UUID="{H}" Invisible="1" Location="http://{PLAYERS[H]["ip"]}:1400/xml/device_description.xml"/>'
        groups += '</ZoneGroup>'
    return audio.xml('<r><ZoneGroupState>' + escape(groups + '</ZoneGroups>') + '</ZoneGroupState></r>')


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.shape = {A: [A, B], C: [C], D: [D, E], F: [F]}
        self.reader = audio.AudioInventory()
        self.reader.players = copy.deepcopy(PLAYERS)
        self.writes = []
        self.patches = [patch.object(audio, 'interfaces', return_value=[ipaddress.ip_interface('192.168.0.21/24')]),
                        patch.object(audio, 'identity', side_effect=lambda ip, nets: next(p.copy() for p in PLAYERS.values() if p['ip'] == ip)),
                        patch.object(audio, 'request', side_effect=lambda ip, nets, action: topology_response(self.shape)),
                        patch.object(audio, 'group_request', side_effect=self.write), patch.object(audio.time, 'sleep')]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def write(self, ip, nets, coordinator):
        uid = next(uid for uid, p in PLAYERS.items() if p['ip'] == ip)
        self.writes.append((uid, coordinator))
        for members in self.shape.values():
            if uid in members: members.remove(uid)
        self.shape = {leader: members for leader, members in self.shape.items() if members}
        self.shape.setdefault(coordinator or uid, []).append(uid)
        for members in self.shape.values(): members.sort()
        return audio.xml('<ok/>')

    def data(self, wanted, **extra):
        return {'roomId': A, 'groupId': A, 'revision': audio.group_revision(self.shape), 'roomIds': wanted, **extra}

    def test_links_and_unlinks_preserve_source_and_move_other_group_followers_first(self):
        auth = Mock()
        result = self.reader.group(self.data([A, C, D, E]), auth)
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.shape, {A: [A, C, D, E], B: [B], F: [F]})
        self.assertEqual(self.writes, [(C, A), (E, A), (D, A), (B, None)])
        self.assertNotIn(A, [uid for uid, _ in self.writes], 'Never replace the source room stream')
        self.assertEqual(auth.call_count, 6, 'Authorize before every write and the final response')
        self.assertEqual(self.reader.last_read, -1000)

    def test_edit_from_follower_uses_its_current_audio_source(self):
        self.reader.group(self.data([A, B, C], roomId=B), lambda: None)
        self.assertEqual(self.writes, [(C, A)])
        self.assertEqual(self.shape[A], [A, B, C])

    def test_unchanged_selection_has_no_writes(self):
        result = self.reader.group(self.data([A, B]), lambda: None)
        self.assertTrue(result['confirmed']);self.assertEqual(self.writes, [])

    def test_stale_topology_invalid_members_or_partial_source_move_never_write(self):
        cases = [self.data([A, B], revision='0' * 64), self.data([B]), self.data([A, H]),
                 self.data([A, D]), self.data([A, A]), self.data([A], ip='192.168.0.1'),
                 self.data([A], roomId=F), self.data([A], groupId=F), self.data(['http://host'])]
        for data in cases:
            with self.assertRaises(ValueError): self.reader.group(data, lambda: None)
        self.assertEqual(self.writes, [])

    def test_unknown_room_and_cross_household_target_never_write(self):
        data = self.data([A, B, F]);del self.shape[F]
        data['revision'] = audio.group_revision(self.shape)
        with self.assertRaisesRegex(ValueError, 'no longer available'): self.reader.group(data, lambda: None)
        self.reader.players.pop(A)
        with self.assertRaisesRegex(ValueError, 'select a Sonos room'): self.reader.group(self.data([A]), lambda: None)
        self.assertEqual(self.writes, [])

    def test_only_one_audio_change_can_run_at_once(self):
        self.reader.control_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, 'previous audio change'):
                self.reader.group(self.data([A, B, C]), lambda: None)
        finally: self.reader.control_lock.release()
        self.assertEqual(self.writes, [])

    def test_lock_during_second_action_stops_remaining_changes(self):
        def authorize():
            if self.writes: raise ValueError('Locked')
        with self.assertRaisesRegex(ValueError, 'Some room links may have changed'):
            self.reader.group(self.data([A, C]), authorize)
        self.assertEqual(self.writes, [(C, A)])
        self.assertIn(B, self.shape[A], 'Do not unlink more rooms after locking')

    def test_uncertain_command_is_not_retried_or_followed_by_more_writes(self):
        def uncertain(ip, nets, coordinator):
            self.write(ip, nets, coordinator)
            raise ValueError('Timed out after sending')
        with patch.object(audio, 'group_request', side_effect=uncertain):
            with self.assertRaisesRegex(ValueError, 'Some room links may have changed'):
                self.reader.group(self.data([A, C]), lambda: None)
        self.assertEqual(self.writes, [(C, A)])
        self.assertEqual(self.reader.last_read, -1000)

    def test_confirmation_polls_do_not_repeat_a_write(self):
        with patch.object(audio, 'group_request') as send:
            with self.assertRaisesRegex(ValueError, 'Some room links may have changed'):
                self.reader.group(self.data([A, B, C]), lambda: None)
            send.assert_called_once()

    def test_external_change_between_review_and_first_write_is_rejected(self):
        initial = topology_response(self.shape)
        changed = topology_response({A: [A], B: [B], C: [C], D: [D, E], F: [F]})
        with patch.object(audio, 'request', side_effect=[initial, changed]):
            with self.assertRaisesRegex(ValueError, 'changed elsewhere'):
                self.reader.group(self.data([A, B, C]), lambda: None)
        self.assertEqual(self.writes, [])

    def test_wire_group_commands_cannot_target_arbitrary_uris_or_services(self):
        with patch.object(audio, 'exchange') as exchange:
            # Exercise the real wire builder, not the inventory's mocked writes.
            self.patches[3].stop()
            audio.group_request('192.168.0.12', NETS, A)
            self.assertEqual(exchange.call_args.args[2], 'SetAVTransportURI')
            self.assertEqual(exchange.call_args.args[3], ('AVTransport', '/MediaRenderer/AVTransport/Control',
                f'<InstanceID>0</InstanceID><CurrentURI>x-rincon:{A}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>'))
            self.assertTrue(exchange.call_args.kwargs['writing'])
            audio.group_request('192.168.0.12', NETS)
            self.assertEqual(exchange.call_args.args[2], 'BecomeCoordinatorOfStandaloneGroup')
            self.assertEqual(exchange.call_args.args[3][2], '<InstanceID>0</InstanceID>')
            for bad in ('http://public.example', A + '</CurrentURI>', '', 1):
                with self.assertRaises(ValueError): audio.group_request('192.168.0.12', NETS, bad)
            self.assertEqual(exchange.call_count, 2)


if __name__ == '__main__': unittest.main()

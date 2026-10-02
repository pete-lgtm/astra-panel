import copy
import unittest
from unittest.mock import Mock

from standby_audio import StandbyAudio


def zone(uid='room-a', **values):
    return dict(id=uid, groupId=uid, name='Family Room', source='Music', playback='playing',
                title='A song', artist='An artist', volume=35, fixedVolume=False,
                artworkId='a' * 64, transportActions=['play', 'pause', 'next'], **values)


class StandbyAudioTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.zones = [zone()]
        self.inventory = Mock()
        self.inventory.read.side_effect = lambda: {'zones': copy.deepcopy(self.zones)}
        self.inventory.control.side_effect = lambda command, authorize: (authorize(), {'accepted': True})[1]
        self.player = StandbyAudio(self.inventory, clock=lambda: self.now)

    def grant(self):
        return self.player.read({})['mediaToken']

    def test_only_music_appears_and_metadata_omits_network_fields(self):
        self.zones[0]['ip'] = '192.168.1.123'
        self.zones[0]['private'] = 'not for lock screen'
        result = self.player.read({})
        self.assertNotIn('ip', result['zone'])
        self.assertNotIn('private', result['zone'])
        for source, state in [('TV', 'playing'), ('Line-in', 'playing'), ('Music', 'stopped'), ('Music', 'offline')]:
            self.zones[0].update(source=source, playback=state)
            self.assertEqual(self.player.read({}), {'zone': None})
            with self.assertRaises(ValueError): self.player.authorized(result['mediaToken'])

    def test_preferred_playing_room_stays_selected_then_pauses_for_five_minutes(self):
        self.zones.append(zone('room-b'))
        result = self.player.read({'preferredRoom': 'room-b'})
        self.assertEqual(result['zone']['id'], 'room-b')
        self.zones.reverse()
        self.zones[0]['playback'] = 'paused'
        self.now += 20
        self.assertEqual(self.player.read({})['zone']['id'], 'room-b')
        self.now += 281
        self.assertEqual(self.player.read({})['zone']['id'], 'room-a')

    def test_expired_replaced_and_fabricated_grants_never_control(self):
        token = self.grant()
        self.now += 61
        with self.assertRaises(ValueError): self.player.control({'mediaToken': token, 'action': 'pause'})
        new = self.grant()
        self.assertNotEqual(token, new)
        for invalid in (token, 'PIN-unlock-grant', '', None):
            with self.assertRaises(ValueError): self.player.control({'mediaToken': invalid, 'action': 'pause'})
        self.inventory.control.assert_not_called()

    def test_room_group_artwork_and_actions_cannot_be_injected(self):
        token = self.grant()
        for action in ('stop', 'mute', 'group', 'reboot', 'disarm', 'SetAVTransportURI'):
            with self.assertRaises(ValueError): self.player.control({'mediaToken': token, 'action': action})
        for extra in ({'roomId': 'room-b'}, {'groupId': 'other'}, {'unlock': token}, {'volume': 40}):
            with self.assertRaises(ValueError): self.player.control({'mediaToken': token, 'action': 'pause', **extra})
        with self.assertRaises(ValueError): self.player.artwork({'mediaToken': token, 'artworkId': 'b' * 64})
        self.inventory.control.assert_not_called()
        self.inventory.artwork.assert_not_called()

    def test_only_selected_room_control_and_live_authorization_are_used(self):
        token = self.grant()
        self.player.control({'mediaToken': token, 'action': 'next'})
        self.assertEqual(self.inventory.control.call_args.args[0], {'roomId': 'room-a', 'groupId': 'room-a', 'action': 'next'})
        self.player.control({'mediaToken': token, 'action': 'volume', 'volume': 37})
        self.assertEqual(self.inventory.control.call_args.args[0]['volume'], 37)
        def expired_during_read(command, authorize):
            self.now += 61
            authorize()
        self.inventory.control.side_effect = expired_during_read
        with self.assertRaises(ValueError): self.player.control({'mediaToken': token, 'action': 'pause'})

    def test_source_and_group_changes_revoke_old_controls(self):
        token = self.grant()
        self.zones[0]['groupId'] = 'different'
        self.player.read({})
        with self.assertRaises(ValueError): self.player.control({'mediaToken': token, 'action': 'pause'})
        token = self.grant()
        self.zones[0]['transportActions'] = ['play', 'pause']
        self.player.read({})
        with self.assertRaises(ValueError): self.player.control({'mediaToken': token, 'action': 'next'})
        self.inventory.control.assert_not_called()

    def test_uncertain_writes_are_never_retried(self):
        token = self.grant()
        self.inventory.control.side_effect = ValueError('Unknown result')
        with self.assertRaises(ValueError): self.player.control({'mediaToken': token, 'action': 'next'})
        self.player.read({})
        self.inventory.control.assert_called_once()

    def test_artwork_rechecks_the_current_track_after_fetch(self):
        token = self.grant()
        def change_track(key):
            self.zones[0]['artworkId'] = 'b' * 64
            self.player.read({})
            return 'old image'
        self.inventory.artwork.side_effect = change_track
        with self.assertRaises(ValueError): self.player.artwork({'mediaToken': token, 'artworkId': 'a' * 64})


if __name__ == '__main__':
    unittest.main()

"""Local lock-screen music. These short-lived grants never unlock the panel."""
import copy
import secrets
import threading
import time


class StandbyAudio:
    ACTIONS = {'play', 'pause', 'next', 'volume', 'volume_up', 'volume_down'}
    FIELDS = ('id', 'groupId', 'name', 'title', 'artist', 'album', 'source', 'playback',
              'volume', 'fixedVolume', 'transportActions', 'artworkId', 'groupRooms')

    def __init__(self, inventory, clock=time.monotonic):
        self.inventory, self.clock = inventory, clock
        self.lock = threading.Lock()
        self.grant = None
        self.last_playing = 0

    def read(self, data):
        if set(data) - {'preferredRoom'} or not isinstance(data.get('preferredRoom', ''), str):
            raise ValueError('Invalid music request.')
        # Discovery and speaker I/O never hold the alarm controller's lock.
        snapshot = self.inventory.read()
        zones = [z for z in snapshot.get('zones', [])
                 if z.get('source') in ('Music', 'AirPlay', 'Spotify Connect', 'Radio')
                 and z.get('playback') in ('playing', 'paused') and z.get('groupId')]
        with self.lock:
            now, old = self.clock(), self.grant
            playing = [z for z in zones if z['playback'] == 'playing']
            # Keep the chosen room stable; retain a paused card for five minutes
            # so Pause does not make the Play button disappear.
            current = next((z for z in zones if old and z['id'] == old['zone']['id']
                            and z['groupId'] == old['zone']['groupId']
                            and (z['playback'] == 'playing' or now - self.last_playing < 300)), None)
            preferred = next((z for z in playing if z['id'] == data.get('preferredRoom')), None)
            zone = preferred or current or next(iter(playing), None)
            if not zone:
                self.grant = None
                return {'zone': None}
            if zone['playback'] == 'playing':
                self.last_playing = now
            zone = {key: copy.deepcopy(zone[key]) for key in self.FIELDS if key in zone}
            same = old and all(old['zone'].get(k) == zone.get(k) for k in ('id', 'groupId', 'source'))
            token = old['token'] if same and old['expires'] > now else secrets.token_urlsafe(32)
            self.grant = {'token': token, 'expires': now + 60, 'zone': zone}
            return {'zone': copy.deepcopy(zone), 'mediaToken': token}

    def authorized(self, token):
        with self.lock:
            grant = self.grant
            if (not isinstance(token, str) or not grant or grant['expires'] <= self.clock()
                    or not secrets.compare_digest(token, grant['token'])):
                raise ValueError('Music changed. Wait for the lock screen to refresh.')
            return copy.deepcopy(grant['zone'])

    def artwork(self, data):
        if set(data) != {'mediaToken', 'artworkId'}:
            raise ValueError('Invalid music artwork request.')
        zone = self.authorized(data['mediaToken'])
        if not zone.get('artworkId') or zone['artworkId'] != data['artworkId']:
            raise ValueError('Music artwork changed.')
        image = self.inventory.artwork(data['artworkId'])
        if self.authorized(data['mediaToken']).get('artworkId') != data['artworkId']:
            raise ValueError('Music artwork changed.')
        return {'artworkId': data['artworkId'], 'image': image}

    def control(self, data):
        action = data.get('action')
        if not isinstance(action, str) or action not in self.ACTIONS or set(data) != (
                {'mediaToken', 'action', 'volume'} if action == 'volume' else {'mediaToken', 'action'}):
            raise ValueError('This control requires the unlocked Audio screen.')
        zone = self.authorized(data['mediaToken'])
        if action in ('play', 'pause', 'next') and action not in zone.get('transportActions', []):
            raise ValueError('This source does not support that playback control.')

        def authorize():
            current = self.authorized(data['mediaToken'])
            if any(current.get(k) != zone.get(k) for k in ('id', 'groupId', 'source')):
                raise ValueError('Music changed. Wait for the lock screen to refresh.')

        command = {'roomId': zone['id'], 'groupId': zone['groupId'], 'action': action}
        if action == 'volume':
            command['volume'] = data['volume']
        # The inventory re-verifies the speaker, group and live capabilities,
        # then checks this grant again immediately before its single write.
        result = self.inventory.control(command, authorize)
        authorize()
        return result

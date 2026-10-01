import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import network_actions as actions
import network_inventory as inventory
from local_auth import LocalSession, ConsoleRequestError
from live_entry import EntryObserver
import server

DEVICE = '0123456789abcdef01234567'
HOST = '192.168.1.1'
COOKIE = 'TOKEN=x.' + base64.urlsafe_b64encode(b'{"csrfToken":"test-csrf"}').decode().rstrip('=') + '.x'


class FakeNetwork:
    def __init__(self):
        self.raw = {'_id': DEVICE, 'name': 'Test switch', 'type': 'usw', 'model': 'USW', 'state': 1,
                    'mac': '00:11:22:33:44:55', 'x_ssh_password': 'never-leak',
                    'port_table': [{'port_idx': 1, 'name': 'Test AP', 'poe_caps': 1, 'poe_enable': True, 'is_uplink': False}],
                    'port_overrides': [{'port_idx': 1, 'poe_mode': 'auto', 'native_networkconf_id': 'vlan-10'},
                                       {'port_idx': 2, 'name': 'Unchanged', 'other_setting': ['preserve']}]}
        self.calls = []
        self.write_error = None
        self.readback = True

    def outlets(self, group=None):
        self.raw.update(type='usp', outlet_table=[{'index': 1, 'name': 'Desk', 'has_relay': True, 'relay_state': True},
                                                {'index': 2, 'name': 'Other', 'outlet_caps': 3, 'relay_state': False}],
                        outlet_overrides=[{'index': 1, 'name': 'Desk', 'cycle_enabled': True},
                                          {'index': 2, 'name': 'Other', 'relay_state': False, 'custom': 'keep'}])
        if group is not None:
            self.raw['outlet_table'][0]['relay_group'] = group

    def request(self, host, method, path, payload=None, **kwargs):
        self.calls.append((host, method, path, copy.deepcopy(payload), kwargs))
        if method == 'GET':
            if path.endswith('/self/sites'):
                data = [{'name': 'default', 'desc': 'Test home'}]
            elif path.endswith('/stat/device'):
                data = [copy.deepcopy(self.raw)]
            else:
                raise AssertionError(path)
            return {'meta': {'rc': 'ok'}, 'data': data}, None
        if self.write_error:
            raise self.write_error
        if method == 'PUT' and self.readback:
            self.raw.update(copy.deepcopy(payload))
            if 'outlet_overrides' in payload:
                for item in self.raw['outlet_table']:
                    change = next((o for o in payload['outlet_overrides'] if o['index'] == item['index']), {})
                    if 'relay_state' in change:
                        item['relay_state'] = change['relay_state']
        return {'meta': {'rc': 'ok'}, 'data': []}, None

    def writes(self):
        return [call for call in self.calls if call[1] != 'GET']


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNetwork()
        patcher = patch.object(LocalSession, 'request', side_effect=self.fake.request)
        patcher.start(); self.addCleanup(patcher.stop)

    def prepare(self, action='rename_device', **kwargs):
        return actions.prepare(HOST, COOKIE, {'site': 'default', 'deviceId': DEVICE, 'action': action, **kwargs})

    def test_review_is_read_only_and_rename_is_minimal_and_read_back(self):
        plan = self.prepare(name='New name')
        self.assertEqual(self.fake.writes(), [])
        self.assertNotIn('never-leak', json.dumps({k: v for k, v in plan.items() if k != 'reader'}))
        self.assertEqual(actions.execute(plan)['status'], 'verified')
        call = self.fake.writes()[0]
        self.assertEqual(call[1:4], ('PUT', '/proxy/network/api/s/default/rest/device/' + DEVICE, {'name': 'New name'}))
        self.assertEqual(call[4]['extra_headers'], {'X-CSRF-Token': 'test-csrf'})

    def test_port_rename_preserves_all_other_overrides(self):
        before = copy.deepcopy(self.fake.raw['port_overrides'])
        plan = self.prepare('rename_port', index=1, name='Office AP')
        self.assertEqual(actions.execute(plan)['status'], 'verified')
        sent = self.fake.writes()[0][3]['port_overrides']
        self.assertEqual(sent[1], before[1])
        self.assertEqual(sent[0], {**before[0], 'name': 'Office AP'})

    def test_outlet_switch_preserves_overrides_and_uses_observed_readback(self):
        self.fake.outlets()
        before = copy.deepcopy(self.fake.raw['outlet_overrides'])
        plan = self.prepare('outlet_off', index=1)
        self.assertEqual(actions.execute(plan)['status'], 'verified')
        sent = self.fake.writes()[0][3]['outlet_overrides']
        self.assertEqual(sent[1], before[1])
        self.assertEqual(sent[0], {**before[0], 'relay_state': False})
        self.fake.raw['outlet_table'][0]['relay_state'] = True
        self.fake.readback = False
        self.assertEqual(actions.execute(self.prepare('outlet_off', index=1))['status'], 'submitted')

    def test_only_known_independent_relays_can_switch_and_ups_is_power(self):
        self.fake.outlets(group=1)
        self.fake.raw['type'] = 'usw'
        self.assertEqual(inventory.normalize_device(self.fake.raw)['kind'], 'power')
        for change in ({}, {'outlet_table': [{'index': 1, 'relay_state': True}]},
                       {'outlet_table': [{'index': 1, 'relay_state': True, 'has_relay': False, 'outlet_caps': 3}]}):
            self.fake.raw.update(change)
            with self.assertRaises(actions.ActionError):
                self.prepare('outlet_off', index=1)
        self.assertEqual(self.fake.writes(), [])

    def test_native_cycle_and_soft_restart_do_not_claim_completed(self):
        plan = self.prepare('cycle_poe', index=1)
        self.assertEqual(actions.execute(plan)['status'], 'submitted')
        self.assertEqual(self.fake.writes()[0][3], {'cmd': 'power-cycle', 'mac': self.fake.raw['mac'], 'port_idx': 1})
        self.assertEqual(actions.execute(self.prepare('restart_device'))['status'], 'submitted')
        self.assertEqual(self.fake.writes()[1][3]['reboot_type'], 'soft')
        self.fake.raw['port_table'][0]['is_uplink'] = True
        with self.assertRaises(actions.ActionError): self.prepare('cycle_poe', index=1)
        self.fake.raw['type'] = 'udm'
        with self.assertRaises(actions.ActionError): self.prepare('restart_device')

    def test_unsafe_input_offline_and_changed_site_never_write(self):
        for kwargs in ({'site': '../other'}, {'site': 'missing'}, {'deviceId': '../id'}, {'action': 'factory-reset'},
                       {'action': 'rename_device', 'name': 'bad\nname'}, {'action': 'cycle_poe', 'index': True},
                       {'action': 'rename_device', 'name': 'Okay', 'path': '/arbitrary'}):
            with self.assertRaises(actions.ActionError):
                actions.prepare(HOST, COOKIE, {'site': 'default', 'deviceId': DEVICE, **kwargs})
        self.fake.raw['state'] = 0
        with self.assertRaises(actions.ActionError): self.prepare(name='Name')
        self.assertEqual(self.fake.writes(), [])

    def test_uncertain_write_is_never_retried_or_reported_successful(self):
        plan = self.prepare(name='Name')
        self.fake.write_error = ValueError('secret response')
        with self.assertRaisesRegex(actions.UncertainAction, 'connection ended'):
            actions.execute(plan)
        self.assertEqual(len(self.fake.writes()), 1)
        self.fake.write_error = ConsoleRequestError(403)
        with self.assertRaisesRegex(actions.ActionError, 'rejected'):
            actions.execute(plan)

    def test_malformed_override_array_cannot_be_replaced_with_partial_data(self):
        self.fake.raw['port_overrides'] = [{'port_idx': 2}, {'port_idx': 2}]
        with self.assertRaisesRegex(actions.ActionError, 'incomplete'):
            self.prepare('rename_port', index=1, name='New')


class ActionRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.fake = FakeNetwork()
        session = LocalSession(); session.host, session.cookie = HOST, COOKIE
        observer = EntryObserver(); observer.ingest('disarmed', [], {})
        patches = [patch.object(server, 'PIN_FILE', Path(self.temp.name) / 'pin.json'), patch.object(server, 'PIN', {}),
                   patch.object(server, 'UNLOCKS', {}), patch.object(server, 'NETWORK_PLANS', {}),
                   patch.object(server, 'LOCAL_SESSION', session), patch.object(server, 'ENTRY', observer),
                   patch.object(LocalSession, 'request', side_effect=self.fake.request)]
        for patcher in patches:
            patcher.start(); self.addCleanup(patcher.stop)
        server.set_pin('852741')
        self.actor = server.check_pin('852741')
        server.UNLOCKS['grant'] = {'expires': server.time.monotonic() + 60, 'user': self.actor}

    def review(self, action='restart_device', **kwargs):
        return server.review_network_action({'unlock': 'grant', 'request': {'site': 'default', 'deviceId': DEVICE, 'action': action, **kwargs}})

    def apply(self, plan):
        return server.commit_network_action({'unlock': 'grant', 'planId': plan['planId'], 'confirm': True})

    def test_requires_admin_pin_and_explicit_review(self):
        with self.assertRaises(ValueError): server.review_network_action({'request': {'site': 'default', 'deviceId': DEVICE, 'action': 'restart_device'}})
        with patch.object(server, 'unlocked_user', return_value={'role': 'user'}):
            with self.assertRaisesRegex(ValueError, 'administrator'): self.review()
        with self.assertRaises(ValueError): self.apply({'planId': 'invented'})
        self.assertEqual(self.fake.writes(), [])

    def test_apply_is_once_audited_and_runs_outside_alarm_lock(self):
        plan = self.review()
        self.assertNotIn('never-leak', json.dumps(plan))
        original = actions.execute
        def execute(plan):
            self.assertTrue(server.LOCK.acquire(False)); server.LOCK.release()
            return original(plan)
        with patch.object(actions, 'execute', side_effect=execute):
            self.assertEqual(self.apply(plan)['status'], 'submitted')
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(len(self.fake.writes()), 1)
        store = server.account_store()
        event = store.history()['events'][0]
        self.assertEqual(event['user_name'], 'Owner')
        self.assertEqual(event['status'], 'submitted')
        store.start_command(self.actor, 'arm')
        store.confirm_commands('armed', '2026-09-25T15:00:00Z')
        self.assertEqual(next(e for e in store.history()['events'] if e['id'] == event['id'])['status'], 'submitted')

    def test_lock_expiry_changed_config_or_alarm_blocks_the_reviewed_write(self):
        for change in ('lock', 'expire', 'config', 'armed', 'connection'):
            server.UNLOCKS['grant'] = {'expires': server.time.monotonic() + 60, 'user': self.actor}
            server.LOCAL_SESSION.host, server.LOCAL_SESSION.cookie = HOST, COOKIE
            server.ENTRY.ingest('disarmed', [], {})
            plan = self.review()
            if change == 'lock': server.UNLOCKS.clear()
            if change == 'expire': server.NETWORK_PLANS[plan['planId']]['expires'] = 0
            if change == 'config': self.fake.raw['name'] = 'Changed name'
            if change == 'armed': server.ENTRY.ingest('armed', [], {})
            if change == 'connection': server.LOCAL_SESSION.clear()
            with self.assertRaises(ValueError, msg=change): self.apply(plan)
        self.assertEqual(self.fake.writes(), [])

    def test_lock_during_final_read_blocks_write_and_denial_preserves_alarm_login(self):
        plan = self.review()
        original = actions.prepare
        def prepare(*args):
            result = original(*args); server.UNLOCKS.clear(); return result
        with patch.object(actions, 'prepare', side_effect=prepare):
            with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(self.fake.writes(), [])
        server.UNLOCKS['grant'] = {'expires': server.time.monotonic() + 60, 'user': self.actor}
        plan = self.review(); self.fake.write_error = ConsoleRequestError(403)
        self.assertEqual(self.apply(plan)['status'], 'rejected')
        self.assertEqual(server.LOCAL_SESSION.cookie, COOKIE)

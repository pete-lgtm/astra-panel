import json
import unittest
from unittest.mock import patch

import network_inventory as network
from local_auth import ConsoleRequestError


class NetworkInventoryTests(unittest.TestCase):
    def get_responses(self, devices, sites=None):
        return [({'meta': {'rc': 'ok'}, 'data': sites if sites is not None else [
            {'name': 'default', 'desc': 'Home', 'private': 'secret'}]}, 'rotated-cookie'),
                ({'meta': {'rc': 'ok'}, 'data': devices}, None)]

    def test_discovery_uses_only_two_fixed_gets_and_returns_no_raw_credentials(self):
        device = {'_id': 'switch', 'name': 'Home switch', 'type': 'usw', 'state': 1,
                  'model': 'USW', 'ip': '192.168.1.2', 'x_ssh_password': 'never-return',
                  'port_table': [{'port_idx': 1, 'name': 'Office', 'up': True, 'speed': 1000,
                                  'poe_caps': 7, 'poe_enable': True, 'poe_good': True, 'poe_power': '4.2'}]}
        with patch.object(network.LocalSession, 'request', side_effect=self.get_responses([device])) as request:
            result = network.discover('192.168.1.1', 'private-cookie')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['site'], {'id': 'default', 'name': 'Home'})
        self.assertEqual(result['devices'][0]['ports'][0]['watts'], 4.2)
        self.assertEqual(request.call_args_list[0].args, ('192.168.1.1', 'GET', '/proxy/network/api/self/sites'))
        self.assertEqual(request.call_args_list[1].args, ('192.168.1.1', 'GET', '/proxy/network/api/s/default/stat/device'))
        self.assertEqual(request.call_args_list[1].kwargs, {'cookie': 'rotated-cookie'})
        for secret in ('never-return', 'private-cookie', 'rotated-cookie', 'private'):
            self.assertNotIn(secret, json.dumps(result))

    def test_multiple_sites_require_explicit_selection_and_reject_arbitrary_paths(self):
        sites = [{'name': 'home', 'desc': 'Home'}, {'name': 'office', 'desc': 'Office'}, {'name': '../../evil'}]
        with patch.object(network.LocalSession, 'request', side_effect=self.get_responses([], sites)) as request:
            result = network.discover('192.168.1.1', 'cookie')
        self.assertEqual(result['state'], 'choose_site')
        self.assertEqual(len(result['sites']), 2)
        self.assertEqual(request.call_count, 1)
        with patch.object(network.LocalSession, 'request', side_effect=self.get_responses([], sites)) as request:
            result = network.discover('192.168.1.1', 'cookie', '../../outside')
        self.assertEqual(result['state'], 'choose_site')
        self.assertEqual(request.call_count, 1)
        with patch.object(network.LocalSession, 'request', side_effect=self.get_responses([], sites)) as request:
            self.assertEqual(network.discover('192.168.1.1', 'cookie', 'office')['state'], 'ready')
        self.assertEqual(request.call_args.args[-1], '/proxy/network/api/s/office/stat/device')

    def test_outlet_state_is_observed_not_desired_and_off_is_not_unknown(self):
        raw = {'_id': 'power', 'type': 'usp', 'state': 1,
               'outlet_overrides': [{'index': 0, 'name': 'Modem', 'relay_state': True}],
               'outlet_table': [{'index': 0, 'name': 'Outlet 1', 'relay_state': False, 'outlet_power': '0', 'outlet_caps': 999},
                                {'index': 1}, {'index': 2, 'relay_state': True, 'outlet_power': 'NaN'}]}
        device = network.normalize_device(raw)
        self.assertEqual({k: device['outlets'][0][k] for k in ('index','name','on','watts')},
                         {'index': 0, 'name': 'Modem', 'on': False, 'watts': 0.0})
        self.assertIsNone(device['outlets'][1]['on'])
        self.assertTrue(device['outlets'][2]['on'])
        self.assertIsNone(device['outlets'][2]['watts'])
        raw['state'] = 0
        self.assertTrue(all(p['on'] is None and p['watts'] is None for p in network.normalize_device(raw)['outlets']))

    def test_poe_never_becomes_a_smart_outlet_and_offline_telemetry_is_unknown(self):
        raw = {'_id': 'switch', 'type': 'usw', 'state': 0, 'port_table': [
            {'port_idx': 1, 'up': True, 'poe_caps': 2, 'poe_good': True, 'poe_enable': True, 'poe_power': '8'}]}
        device = network.normalize_device(raw)
        self.assertEqual(device['outlets'], [])
        port = device['ports'][0]
        self.assertTrue(port['poe'])
        for field in ('linkUp', 'watts', 'poePowered', 'poeEnabled'):
            self.assertIsNone(port[field])
        self.assertEqual(network.normalize_device({'_id': 'x', 'state': True})['state'], 'unknown')

    def test_missing_login_denied_unsupported_and_empty_are_distinct(self):
        with patch.object(network.LocalSession, 'request') as request:
            self.assertEqual(network.discover(None, None)['state'], 'needs_login')
            request.assert_not_called()
        for status, expected in ((401, 'needs_login'), (403, 'denied'), (404, 'unavailable'), (503, 'unavailable')):
            with patch.object(network.LocalSession, 'request', side_effect=ConsoleRequestError(status)):
                result = network.discover('192.168.1.1', 'cookie')
            self.assertEqual(result['state'], expected)
            self.assertEqual(result['devices'], [])
        with patch.object(network.LocalSession, 'request', side_effect=self.get_responses([])):
            result = network.discover('192.168.1.1', 'cookie')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['devices'], [])

    def test_invalid_responses_are_not_reported_as_successful_empty_discovery(self):
        for body in ({'meta': {'rc': 'ok'}, 'data': None}, {'meta': {'rc': 'error', 'msg': 'private detail'}, 'data': []}, []):
            with patch.object(network.LocalSession, 'request', return_value=(body, None)):
                result = network.discover('192.168.1.1', 'cookie')
            self.assertNotEqual(result['state'], 'ready')
            self.assertNotIn('private detail', json.dumps(result))
        with patch.object(network.LocalSession, 'request', side_effect=ValueError('cookie or private response')):
            self.assertNotIn('private response', json.dumps(network.discover('192.168.1.1', 'cookie')))

    def test_bounded_device_and_interface_lists_do_not_silently_truncate(self):
        with patch.object(network.LocalSession, 'request', side_effect=self.get_responses([{'_id': str(i)} for i in range(501)])):
            self.assertEqual(network.discover('192.168.1.1', 'cookie')['state'], 'unavailable')
        with self.assertRaises(network.InventoryError):
            network.normalize_device({'_id': 'oversize', 'port_table': [{}] * 257})

    def test_reader_has_no_arbitrary_url_or_write_operation(self):
        with patch.object(network.LocalSession, 'request') as request:
            for path in ('https://another.host/', '/proxy/network/api/s/default/cmd/devmgr', '/api/auth/logout'):
                with self.assertRaises(ValueError):
                    network.NetworkReader('192.168.1.1', 'cookie').get(path)
            request.assert_not_called()

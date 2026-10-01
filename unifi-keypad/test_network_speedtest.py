import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import network_speedtest as speed
import server


STAMP = 1_790_000_000_000


def sample(stamp=STAMP, summary=2, down=2, up=2):
    return {'timestamp': stamp, 'status_summary': summary, 'status_download': down, 'status_upload': up,
            'xput_download': 912.4, 'xput_upload': 41.8, 'latency': 12.7, 'secret': 'never-return'}


class AdapterTests(unittest.TestCase):
    def test_only_completed_finite_values_with_units_and_timestamp(self):
        self.assertEqual(speed.result_from(sample()), {'downloadMbps': 912.4, 'uploadMbps': 41.8,
                         'latencyMs': 12.7, 'testedAt': STAMP})
        self.assertEqual(speed.timestamp(STAMP / 1000), STAMP)
        for change in ({'status_summary': 1}, {'status_upload': 1}, {'status_download': True},
                       {'xput_download': float('nan')}, {'xput_upload': -1}, {'timestamp': 0}):
            with self.subTest(change=change):
                self.assertIsNone(speed.result_from(dict(sample(), **change)))
        self.assertIsNone(speed.result_from(dict(sample(), latency=float('inf')))['latencyMs'])

    def test_fixed_protocol_and_scrubbed_device_identity(self):
        reader = speed.Reader('console', 'cookie', 'default')
        def request(host, method, path, payload=None, **kwargs):
            if path.endswith('/self/sites'):
                data = [{'name': 'default'}]
            elif path.endswith('/stat/device'):
                data = [{'_id': 'a'*24, 'mac': '11:22:33:44:55:66', 'type': 'udm', 'state': 1,
                         'name': 'Gateway', 'x_authkey': 'secret'}]
            elif path.endswith('/stat/health'):
                data = [{'subsystem': 'www', 'speedtest_status': 'Idle'}]
            else:
                self.assertEqual((method, path), ('POST', '/proxy/network/api/s/default/cmd/devmgr'))
                self.assertIn(payload['cmd'], ('speedtest', 'speedtest-status'))
                data = [sample()] if payload['cmd'] == 'speedtest-status' else []
            return {'meta': {'rc': 'ok'}, 'data': data}, None
        with patch.object(reader.session, 'request', side_effect=request) as send, patch.object(speed, 'csrf_from_cookie', return_value='csrf'):
            self.assertEqual(reader.gateway(), {'id': 'a'*24, 'name': 'Gateway'})
            row, running = reader.observation()
            self.assertFalse(running)
            self.assertEqual(speed.result_from(row)['downloadMbps'], 912.4)
            reader.command('speedtest')
            self.assertEqual([c.args[3] for c in send.call_args_list if c.args[1] == 'POST'],
                             [{'cmd': 'speedtest-status'}, {'cmd': 'speedtest'}])
            with self.assertRaises(speed.SpeedTestError):
                reader.command('restart')
        for site in ('../admin', '', None, [], 'default?cmd=restart'):
            with self.assertRaises(speed.SpeedTestError):
                speed.Reader('host', 'cookie', site)

    def test_unknown_site_offline_and_multiple_gateways_are_not_testable(self):
        reader = speed.Reader('host', 'cookie', 'default')
        gateway = {'_id': 'a'*24, 'type': 'udm', 'state': 1, 'mac': '11:22:33:44:55:66'}
        for replies in ([[]], [[{'name': 'default'}], []], [[{'name': 'default'}], [dict(gateway, state=0)]],
                        [[{'name': 'default'}], [gateway, dict(gateway, _id='b'*24)]]):
            with patch.object(reader, 'get', side_effect=replies), self.assertRaises(speed.SpeedTestError):
                reader.gateway()


class JobTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000
        self.manager = speed.SpeedTests()
        self.reader = Mock()
        self.reader.gateway.return_value = {'id': 'gateway-1', 'name': 'Gateway'}
        self.reader.observation.return_value = (sample(), False)
        self.reader.command.return_value = []
        for p in (patch.object(speed, 'Reader', return_value=self.reader),
                  patch.object(speed.time, 'monotonic', side_effect=lambda: self.clock)):
            p.start();self.addCleanup(p.stop)

    def run_test(self, start=False, **kwargs):
        return self.manager.request('console', 'cookie', 'default', start=start, **kwargs)

    def test_open_is_read_only_duplicate_start_and_old_results_never_complete_new_test(self):
        result = self.run_test()
        self.assertEqual(result['state'], 'idle')
        self.reader.command.assert_not_called()
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(self.run_test(True)['state'], 'running')
        self.run_test(True)
        self.reader.command.assert_called_once_with('speedtest')
        self.clock += 30
        self.assertEqual(self.run_test()['state'], 'running')
        self.reader.observation.return_value = (sample(STAMP+30_000, summary=1, up=1), True)
        partial = self.run_test()
        self.assertEqual(partial['phase'], 'Uploading…')
        self.assertEqual(partial['result']['testedAt'], STAMP)
        self.reader.observation.return_value = (sample(STAMP+60_000), False)
        done = self.run_test()
        self.assertEqual(done['state'], 'completed')
        self.assertEqual(done['result']['testedAt'], STAMP+60_000)

    def test_uncertain_start_is_not_replayed_and_times_out_without_fake_success(self):
        self.reader.command.side_effect = OSError('SECRET cookie')
        result = self.run_test(True)
        self.assertIn('could not be confirmed', result['message'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.run_test(True)
        self.reader.command.assert_called_once()
        self.clock += 125
        timeout = self.run_test()
        self.assertEqual(timeout['state'], 'timeout')
        self.assertFalse(timeout['canStart'])
        self.assertIsNone(timeout['pollAfterSeconds'])
        self.clock += 60
        self.assertTrue(self.run_test()['canStart'])

    def test_external_running_test_failure_and_connection_bound_results(self):
        self.reader.observation.return_value = (sample(summary=0, down=0, up=0), True)
        self.assertFalse(self.run_test(True)['canStart'])
        self.reader.command.assert_not_called()
        self.reader.observation.return_value = (sample(), False)
        self.run_test(True)
        self.reader.observation.return_value = (sample(STAMP+1_000, summary=3, up=3), False)
        self.assertEqual(self.run_test()['state'], 'failed')
        self.reader.gateway.return_value = {'id': 'different-gateway', 'name': 'Replacement'}
        self.reader.observation.return_value = (sample(summary=0, down=0, up=0), False)
        self.clock += 31
        self.assertIsNone(self.run_test()['result'])

    def test_expired_authorization_and_concurrent_calls_send_no_start(self):
        with self.assertRaises(speed.SpeedTestError):
            self.run_test(True, authorize=Mock(side_effect=ValueError('Locked')))
        self.reader.command.assert_not_called()
        self.manager.lock.acquire()
        try:
            with self.assertRaises(speed.SpeedTestError):
                self.run_test(True)
        finally:
            self.manager.lock.release()
        self.reader.command.assert_not_called()

    def test_denied_or_malformed_status_suppresses_raw_errors_and_no_start(self):
        for error in (speed.ConsoleRequestError(403), ValueError('SECRET response'), OSError('SECRET cookie')):
            self.reader.observation.side_effect = error
            with self.assertRaises(speed.SpeedTestError) as caught:
                self.run_test(True)
            self.assertNotIn('SECRET', str(caught.exception))
        self.reader.command.assert_not_called()


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.session = server.LocalSession()
        self.session.host, self.session.cookie = 'console', 'private-cookie'
        self.manager = Mock()
        self.manager.request.return_value = {'state': 'idle', 'canStart': True}
        for name, value in [('PIN_FILE', Path(self.temp.name)/'pin.json'), ('PIN', {}), ('UNLOCKS', {}),
                            ('LOCAL_SESSION', self.session), ('NETWORK_SPEEDTESTS', self.manager)]:
            p = patch.object(server, name, value);p.start();self.addCleanup(p.stop)
        server.set_pin('739126');self.actor = server.check_pin('739126')
        server.UNLOCKS['grant'] = {'expires': server.time.monotonic()+60, 'user': self.actor}

    def test_requires_current_admin_grant_and_does_not_extend_it(self):
        data = {'unlock': 'grant', 'site': 'default'}
        with self.assertRaises(ValueError):
            server.gateway_speedtest({'site': 'default'}, True)
        user = server.account_store().save_user(self.actor, {'name': 'User', 'role': 'user', 'pin': '628451', 'confirmation': '628451'})['user']
        server.UNLOCKS['other'] = {'expires': server.time.monotonic()+60, 'user': user}
        with self.assertRaises(ValueError):
            server.gateway_speedtest(dict(data, unlock='other'), True)
        self.manager.request.assert_not_called()
        expires = server.UNLOCKS['grant']['expires']
        server.gateway_speedtest(data)
        self.assertEqual(server.UNLOCKS['grant']['expires'], expires)

    def test_io_outside_alarm_lock_and_login_or_pin_loss_before_write_is_rejected(self):
        def request(*args, **kwargs):
            self.assertTrue(server.LOCK.acquire(blocking=False));server.LOCK.release()
            kwargs['authorize']()
            return {'state': 'running', 'started': True}
        self.manager.request.side_effect = request
        server.gateway_speedtest({'unlock': 'grant', 'site': 'default'}, True)
        events = server.account_store().history()['events']
        self.assertTrue(any(e['action'] == 'internet_speed_test' for e in events))
        self.assertNotIn('private-cookie', json.dumps(events))
        for clear in (lambda: server.UNLOCKS.clear(), self.session.clear):
            server.UNLOCKS['grant'] = {'expires': server.time.monotonic()+60, 'user': self.actor}
            def changed(*args, **kwargs):
                clear();kwargs['authorize']()
            self.manager.request.side_effect = changed
            with self.assertRaises(ValueError):
                server.gateway_speedtest({'unlock': 'grant', 'site': 'default'}, True)

    def test_routes_require_csrf_and_respond_no_store(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = httpd.server_address[1]
        for name, value in [('PORT', port), ('ORIGIN', 'http://127.0.0.1:%s'%port), ('TOKEN', 'test-csrf')]:
            p = patch.object(server, name, value);p.start();self.addCleanup(p.stop)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close);self.addCleanup(httpd.shutdown)
        for route in ('status', 'start'):
            for token, expected in [('wrong', 403), ('test-csrf', 200)]:
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                conn.request('POST', '/hub/network/speedtest/'+route, json.dumps({'unlock':'grant','site':'default'}),
                             {'Origin':server.ORIGIN,'Content-Type':'application/json','X-Keypad-Token':token})
                result = conn.getresponse();result.read()
                self.assertEqual(result.status, expected)
                self.assertEqual(result.getheader('Cache-Control'), 'no-store');conn.close()
        self.assertEqual(self.manager.request.call_count, 2)


if __name__ == '__main__':
    unittest.main()

import base64
import json
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from local_auth import LocalSession
from rule_setup import (MANIFEST, PROFILES, RULES, SENSORS, RuleSetupSession,
                        csrf_from_cookie, inspect_console, run_creation_probe)


def cookie(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'TOKEN=header.' + payload + '.signature'


class RuleSetupTests(unittest.TestCase):
    def test_csrf_is_derived_from_console_session(self):
        self.assertEqual(csrf_from_cookie(cookie({'csrfToken': 'test-csrf'})), 'test-csrf')
        for value in (None, '', 'other=x.y.z', 'TOKEN=broken', cookie({}), cookie({'csrfToken': 'bad\nheader'})):
            with self.assertRaises(ValueError):
                csrf_from_cookie(value)

    def test_request_includes_csrf_but_does_not_return_it(self):
        session = LocalSession()
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"id":"test"}'
        response.getheaders.return_value = []
        with patch('local_auth.http.client.HTTPSConnection', return_value=connection), patch.dict('os.environ', {'UNIFI_CERT_SHA256': ''}):
            result = session.request('192.168.0.1', 'POST', RULES, {'title': 'Test'}, cookie='TOKEN=private', extra_headers={'X-CSRF-Token': 'test-csrf'})
        headers = connection.request.call_args.kwargs['headers']
        self.assertEqual(headers['X-CSRF-Token'], 'test-csrf')
        self.assertEqual(headers['Cookie'], 'TOKEN=private')
        self.assertNotIn('private', str(result))
        self.assertNotIn('test-csrf', str(result))

    def test_mutation_paths_cannot_arm_disarm_or_modify_devices(self):
        session = RuleSetupSession()
        session.host, session.cookie = '192.168.0.1', cookie({'csrfToken': 'test-csrf'})
        with patch.object(session, 'request') as request:
            for method, path in [('POST', PROFILES), ('POST', RULES + '/test'), ('PATCH', SENSORS), ('DELETE', RULES + '/../profiles')]:
                with self.assertRaises(ValueError):
                    session.write(method, path)
            request.assert_not_called()

    def test_read_discovery_does_not_mutate_or_expose_credentials(self):
        session = RuleSetupSession()
        payloads = {RULES: [{'id': 'r', 'title': 'Existing', 'trigger_categories': [],
                            'action_categories': [{'actions': [{'id': 'protect:webhook', 'data': {'url': 'private-url', 'token': 'secret'}}]}]}],
                    PROFILES: [{'id': 'p', 'title': 'Arm', 'state': 'disarmed', 'private': 'hidden'}],
                    SENSORS: [{'id': 's', 'name': 'Front', 'state': 'CONNECTED', 'isOpened': False, 'private': 'hidden'}],
                    MANIFEST: {'schema': 'public'}}
        with patch.object(session, 'read', side_effect=lambda path: payloads[path]):
            result = inspect_console(session)
        self.assertEqual(result['sensors'][0]['id'], 's')
        self.assertNotIn('private-url', str(result))
        self.assertNotIn("'secret'", str(result))
        self.assertNotIn('hidden', str(result))
        self.assertEqual(result['rules'][0]['actions'][0]['parameters']['url'], 'str')


class FakeConsole:
    host = '192.168.0.1'
    cookie = cookie({'csrfToken': 'test-csrf'})

    def __init__(self):
        self.rules = [{'id': 'original-rule', 'title': 'Existing door protection', 'scope': {'doors': ['front']}}]
        self.commands = []
        self.state = 'disarmed'
        self.uncertain_create = False
        self.delete_failure = False
        self.id = '12345678-1234-4567-8123-123456789abc'

    def read(self, path):
        return copy.deepcopy({RULES: self.rules,
                              PROFILES: [{'id': 'profile', 'state': self.state}],
                              MANIFEST: {'trigger_categories': [{'triggers': [{'id': 'protect:webhook.webhook'}]}],
                                         'action_categories': [{'actions': [{'id': 'protect:notify'}]}]}}[path])

    def write(self, method, path, body=None):
        self.commands.append((method, path))
        if method in ('POST', 'PATCH'):
            rule = {'id': self.id, 'title': body['title'], 'stats': {'executions_24h': 0},
                    'trigger_categories': [{'triggers': copy.deepcopy(body['triggers_data'][0])}],
                    'action_categories': [{'actions': copy.deepcopy(body['actions_data'][0])}]}
            if method == 'POST':
                self.rules.append(rule)
                if self.uncertain_create:
                    raise ValueError('Create response timed out.')
            else:
                self.rules[-1] = rule
            return copy.deepcopy(rule)
        if self.delete_failure:
            raise ValueError('Delete could not complete.')
        self.rules = [r for r in self.rules if path != RULES + '/' + r['id']]


class ProbeLifecycleTests(unittest.TestCase):
    def test_full_lifecycle_preserves_existing_rules(self):
        session = FakeConsole()
        before = copy.deepcopy(session.rules)
        with tempfile.TemporaryDirectory() as directory:
            result = run_creation_probe(session, Path(directory) / 'journal.json')
        self.assertTrue(result['passed'])
        self.assertTrue(result['temporaryRuleRemoved'])
        self.assertEqual(session.rules, before)
        self.assertEqual([m for m, p in session.commands], ['POST', 'PATCH', 'DELETE'])
        self.assertTrue(all(p == RULES or p == RULES + '/' + session.id for m, p in session.commands))

    def test_timeout_after_create_does_not_retry_and_cleans_up(self):
        session = FakeConsole()
        session.uncertain_create = True
        with tempfile.TemporaryDirectory() as directory:
            result = run_creation_probe(session, Path(directory) / 'journal.json')
        self.assertFalse(result['passed'])
        self.assertTrue(result['temporaryRuleRemoved'])
        self.assertEqual([m for m, p in session.commands], ['POST', 'DELETE'])
        self.assertEqual(len(session.rules), 1)

    def test_non_disarmed_state_stops_before_writes(self):
        for state in ('armed', 'arming', 'breach', None):
            session = FakeConsole()
            session.state = state
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'Disarm'):
                    run_creation_probe(session, Path(directory) / 'journal.json')
            self.assertEqual(session.commands, [])

    def test_cleanup_failure_is_recorded_and_blocks_duplicate_creation(self):
        session = FakeConsole()
        session.delete_failure = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            with self.assertRaisesRegex(ValueError, 'cleanup needs review'):
                run_creation_probe(session, path)
            journal = json.loads(path.read_text())
            self.assertFalse(journal['cleaned'])
            self.assertEqual(journal['phase'], 'cleanup_required')
            with self.assertRaisesRegex(ValueError, 'previous test needs cleanup'):
                run_creation_probe(session, path)
        self.assertEqual([m for m, p in session.commands].count('POST'), 1)

    def test_missing_request_token_prevents_any_write(self):
        session = FakeConsole()
        session.cookie = 'TOKEN=not-supported'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'request-verification token'):
                run_creation_probe(session, Path(directory) / 'journal.json')
        self.assertEqual(session.commands, [])

    def test_existing_identifier_is_never_deleted(self):
        session = FakeConsole()
        session.id = 'original-rule'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'cleanup needs review'):
                run_creation_probe(session, Path(directory) / 'journal.json')
        self.assertNotIn('DELETE', [m for m, p in session.commands])


if __name__ == '__main__':
    unittest.main()

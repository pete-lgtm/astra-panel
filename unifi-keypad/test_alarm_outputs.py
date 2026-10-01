"""Synthetic Protect provisioning and siren lifecycle checks; no hardware calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import alarm_outputs as outputs
import arm_coverage
import onboarding as ob
import sensor_settings
import siren_runtime
from device_config import load_site, validate_site
from pin_store import create_pin
from test_onboarding import Console
from live_entry import EntryObserver


class SirenConsole(Console):
    def __init__(self):
        super().__init__()
        self.sirens = [{'id': 'siren-1', 'name': 'Hall siren', 'mac': '00112233AABB',
                        'state': 'CONNECTED', 'modelKey': 'siren', 'type': 'UP-Siren-PoE'}]
        self.sources = [{'value': '00112233AABB', 'metadata': {'id': 'siren-1'}}]
        self.manifest['action_categories'][0]['actions'].append({'id': outputs.ACTION, 'options_schema': {
            'required': ['sources', 'duration'], 'properties': {'sources': {'ui:dataurl': outputs.SOURCES},
            'duration': {'type': 'string', 'ui:data': [{'value': str(n)} for n in outputs.DURATIONS]}}}})

    def request(self, host, method, path, payload=None, **kwargs):
        if method == 'GET' and path in (outputs.SIRENS, outputs.SOURCES):
            return copy.deepcopy(self.sirens if path == outputs.SIRENS else self.sources), None
        return super().request(host, method, path, payload, **kwargs)


class SirenSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.console = SirenConsole(); self.state = {}
        self.env = patch.dict('os.environ', {}, clear=True); self.env.start(); self.addCleanup(self.env.stop)
        self.wait = patch.object(ob.time, 'sleep'); self.wait.start(); self.addCleanup(self.wait.stop)
        create_pin(self.root/'.keypad-pin.json', '736291', '736291')
        self.choices = {'name': 'Home', 'profileId': '', 'profileName': 'Home alarm',
                        'sensorIds': ['sensor-1', 'sensor-2'], 'receivers': [self.console.users[0]['value']]}
        self.apply(self.choices)

    def apply(self, choices):
        plan = ob.review(self.console, choices, self.root, self.state)
        ob.apply(self.console, {'planId': plan['planId']}, self.root, self.state)
        return plan

    def add_siren(self):
        plan = outputs.review(self.console, {'sirenIds': ['siren-1'], 'sirenDuration': 30}, self.root, self.state)
        ob.apply(self.console, {'planId': plan['planId']}, self.root, self.state)
        return plan

    def site(self):
        return load_site(self.root/'site.json')

    def test_scan_and_review_are_read_only_new_devices_not_selected(self):
        before = copy.deepcopy(self.console.writes)
        discovered = outputs.discover(self.console, self.root)
        self.assertEqual(discovered['sirenIds'], [])
        self.assertTrue(discovered['sirens'][0]['available'])
        plan = outputs.review(self.console, {'sirenIds': ['siren-1'], 'sirenDuration': 30}, self.root, self.state)
        self.assertEqual(plan['sirens'], ['Hall siren'])
        self.assertEqual(self.console.writes, before)
        self.assertIsNone(outputs.siren_output(self.site()))

    def test_add_only_to_delayed_alert_preserve_rule_identity_and_arm_modes(self):
        before = self.site(); plan = self.add_siren(); after = self.site()
        self.assertEqual(plan['sirenDuration'], 30)
        for key in ('ruleIds', 'webhooks', 'sensors', 'profileId', 'stayProfileId', 'awayProfileId'):
            self.assertEqual(before[key], after[key])
        for kind, identifier in after['ruleIds'].items():
            rule = next(r for r in self.console.rules if r['id'] == identifier)
            sounds = [a for a in ob.entries(rule, 'action') if a['id'] == outputs.ACTION]
            self.assertEqual(len(sounds), int(kind == 'entry_alert'))
            if sounds:
                self.assertEqual(sounds[0]['data'], {'sources': ['00112233AABB'], 'duration': '30'})
                self.assertEqual(set(rule['restriction']['profile_ids']), set(ob.profile_ids_for(after).values()))
        sensor_settings.verify_for_activation(self.console, self.root)
        for mode in ob.ARM_PROFILE_KINDS:
            arm_coverage.prepare(self.console, after, self.root, ['sensor-1'], mode=mode)
        self.assertTrue(all('/play' not in path and '/stop' not in path and '/webhook/' not in path for _, path, _ in self.console.writes))

    def test_sensor_edits_and_legacy_setup_preserve_sirens(self):
        self.add_siren(); before = outputs.siren_output(self.site())
        plan = sensor_settings.review(self.console, {'sensorIds': ['sensor-1']}, self.root, self.state)
        ob.apply(self.console, {'planId': plan['planId']}, self.root, self.state)
        self.assertEqual(outputs.siren_output(self.site()), before)
        choices = ob.default_choices(self.site(), ob.snapshot(self.console))
        choices.pop('sirenIds'); choices.pop('sirenDuration')
        self.apply(choices)
        self.assertEqual(outputs.siren_output(self.site()), before)

    def test_remove_and_repeat_apply_are_idempotent(self):
        self.add_siren(); ids = self.site()['ruleIds']; before = len(self.console.writes)
        plan = outputs.review(self.console, {'sirenIds': [], 'sirenDuration': 30}, self.root, self.state)
        ob.apply(self.console, {'planId': plan['planId']}, self.root, self.state)
        self.assertIsNone(outputs.siren_output(self.site()))
        self.assertEqual(self.site()['ruleIds'], ids)
        writes = len(self.console.writes); self.assertEqual(writes-before, 1)
        ob.apply(self.console, {'planId': plan['planId']}, self.root, self.state)
        self.assertEqual(writes, len(self.console.writes))

    def test_new_unavailable_or_unadvertised_devices_are_rejected(self):
        before = len(self.console.writes)
        for change in ('offline', 'unadvertised', 'wrong_identity'):
            self.console.sirens[0]['state'] = 'DISCONNECTED' if change == 'offline' else 'CONNECTED'
            self.console.sources = [] if change == 'unadvertised' else [{'value': '00112233AABB', 'metadata': {'id': 'wrong' if change == 'wrong_identity' else 'siren-1'}}]
            with self.assertRaises(ValueError):
                outputs.review(self.console, {'sirenIds': ['siren-1'], 'sirenDuration': 30}, self.root, self.state)
        self.assertEqual(before, len(self.console.writes))

    def test_review_invalidated_before_any_write_if_device_changes(self):
        plan = outputs.review(self.console, {'sirenIds': ['siren-1'], 'sirenDuration': 30}, self.root, self.state)
        before = len(self.console.writes)
        self.console.sirens[0]['state'] = 'DISCONNECTED'
        with self.assertRaises(ob.ReviewRequired):
            ob.apply(self.console, {'planId': plan['planId']}, self.root, self.state)
        self.assertEqual(before, len(self.console.writes))
        self.assertEqual(json.loads(ob.store_paths(self.root)[1].read_text())['phase'], 'complete')

    def test_saved_missing_siren_visible_and_removable_without_auto_selecting_new(self):
        self.add_siren(); self.console.sirens.clear(); self.console.sources.clear()
        data = outputs.discover(self.console, self.root)
        self.assertEqual(data['sirens'][0]['state'], 'MISSING')
        self.assertEqual(data['sirenIds'], ['siren-1'])
        plan = outputs.review(self.console, {'sirenIds': [], 'sirenDuration': 30}, self.root, self.state)
        ob.apply(self.console, {'planId': plan['planId']}, self.root, self.state)
        self.assertIsNone(outputs.siren_output(self.site()))

    def test_armed_and_tampered_managed_rules_block_save(self):
        self.add_siren(); before = len(self.console.writes)
        alert = next(r for r in self.console.rules if r['id'] == self.site()['ruleIds']['entry_alert'])
        ob.entries(alert, 'action')[1]['data']['duration'] = '5'
        with self.assertRaises(ValueError):
            outputs.review(self.console, {'sirenIds': [], 'sirenDuration': 30}, self.root, self.state)
        ob.entries(alert, 'action')[1]['data']['duration'] = '30'
        self.console.profiles[0]['state'] = 'armed'
        with self.assertRaisesRegex(ValueError, 'Disarm'):
            outputs.review(self.console, {'sirenIds': [], 'sirenDuration': 30}, self.root, self.state)
        self.assertEqual(before, len(self.console.writes))

    def test_action_order_is_normalized_but_extra_actions_are_rejected(self):
        self.add_siren()
        alert=next(r for r in self.console.rules if r['id']==self.site()['ruleIds']['entry_alert'])
        alert['action_categories'][0]['actions'].reverse()
        sensor_settings.verify_for_activation(self.console,self.root)
        alert['action_categories'][0]['actions'].append(copy.deepcopy(alert['action_categories'][0]['actions'][0]))
        with self.assertRaises(ValueError):sensor_settings.verify_for_activation(self.console,self.root)

    def test_output_validation_and_no_advertised_support(self):
        for seconds in (0, 31, True, '30'):
            with self.assertRaises(ValueError):
                outputs.review(self.console, {'sirenIds': ['siren-1'], 'sirenDuration': seconds}, self.root, self.state)
        self.console.manifest['action_categories'][0]['actions'].pop()
        self.assertEqual(outputs.discover(self.console, self.root)['sirenDurations'], [])
        with self.assertRaises(ValueError):
            outputs.review(self.console, {'sirenIds': ['siren-1'], 'sirenDuration': 30}, self.root, self.state)


class SirenRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.env=patch.dict('os.environ',{},clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.output={'type':'siren','devices':[{'id':'siren-1','name':'Siren','mac':'00112233AABB'}],'durationSeconds':30}
        self.site={'consoleHost':'192.168.0.1','outputs':[self.output]}
        self.calls=[];self.active=True

    def request(self, method, path):
        self.calls.append((method,path))
        if method=='POST': self.active=False;return None
        return {'id':'siren-1','mac':'00112233AABB','sirenStatus':{'isActive':self.active}}

    def test_no_stop_until_confirmed_disarm_and_no_play_commands(self):
        siren_runtime.remember(self.root,self.site,now=100)
        for state in ('armed','arming','unknown','breached'):
            siren_runtime.stop_after_disarm(self.root,self.site,state,self.request,now=105)
        self.assertEqual(self.calls,[])
        siren_runtime.stop_after_disarm(self.root,self.site,'disarmed',self.request,now=105)
        self.assertEqual(self.calls,[('GET','sirens/siren-1'),('POST','sirens/siren-1/stop'),('GET','sirens/siren-1')])
        self.assertFalse(siren_runtime.journal_path(self.root).exists())

    def test_restart_and_uncertain_stop_recheck_without_blind_replay(self):
        siren_runtime.remember(self.root,self.site,now=100)
        def uncertain(method,path):
            result=self.request(method,path)
            if method=='POST':raise ValueError('Response lost')
            return result
        with self.assertRaises(ValueError):siren_runtime.stop_after_disarm(self.root,self.site,'disarmed',uncertain,now=105)
        self.calls=[]
        siren_runtime.stop_after_disarm(self.root,self.site,'disarmed',self.request,now=107)
        self.assertEqual(self.calls,[('GET','sirens/siren-1')])

    def test_expired_or_wrong_device_never_stopped(self):
        siren_runtime.remember(self.root,self.site,now=100)
        with self.assertRaises(ValueError):
            siren_runtime.stop_after_disarm(self.root,self.site,'disarmed',lambda *_:{'id':'other','mac':'00112233AABB'},now=105)
        siren_runtime.stop_after_disarm(self.root,self.site,'disarmed',self.request,now=150)
        self.assertEqual(self.calls,[])

    def test_stop_worker_does_not_block_or_invalidate_alarm_observation(self):
        import server
        observer=EntryObserver();observer.ingest('disarmed',[],{})
        def fake_stop(root,site,state,request):
            self.assertTrue(server.LOCK.acquire(blocking=False))
            server.LOCK.release()
            raise ValueError('Siren offline')
        with patch.object(server,'SITE',self.site),patch.object(server,'CONFIG',{'host':'192.168.0.1','key':'fake'}),patch.object(server,'ENTRY',observer),patch.object(server.siren_runtime,'stop_after_disarm',side_effect=fake_stop):
            server.observe_sirens_once()
            self.assertTrue(observer.snapshot()['fresh'])
            self.assertEqual(observer.snapshot()['state'],'disarmed')
            self.assertIn('not confirmed',server.SIREN_MESSAGE)

    def test_entry_countdown_only_dispatches_once_and_disarm_cancels(self):
        for disarm in (True,False):
            clock=[1.0];observer=EntryObserver(clock=lambda:clock[0],wall=lambda:clock[0]+1000);observer.enabled=True
            observer.ingest('armed',[],{'door':'Front'})
            clock[0]=2;observer.ingest('armed',[{'id':'event','type':'sensorOpened','sensor':'door','start':1002000}],{'door':'Front'})
            sent=[];observer.dispatch(lambda:sent.append('delayed-alarm'));self.assertEqual(sent,[])
            clock[0]=30
            if disarm:observer.ingest('disarmed',[],{'door':'Front'})
            clock[0]=65;observer.dispatch(lambda:sent.append('delayed-alarm'));observer.dispatch(lambda:sent.append('duplicate'))
            self.assertEqual(sent,[] if disarm else ['delayed-alarm'])


if __name__=='__main__':unittest.main()

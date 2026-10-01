import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pin_store import create_pin
from device_config import load_site
from live_entry import EntryObserver
import onboarding as ob
import sensor_settings as ss
from test_onboarding import Console
import server


class SensorSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.session=Console();self.state={}
        self.env=patch.dict('os.environ',{},clear=True);self.env.start();self.addCleanup(self.env.stop)
        pin_path_patch=patch.object(server,'PIN_FILE',self.root/'.keypad-pin.json');pin_path_patch.start();self.addCleanup(pin_path_patch.stop)
        create_pin(self.root/'.keypad-pin.json','736291','736291')
        choices={'name':'Test home','profileId':'','profileName':'Test alarm','sensorIds':['sensor-1','sensor-2'],'receivers':[self.session.users[0]['value']]}
        plan=ob.review(self.session,choices,self.root,self.state)
        ob.apply(self.session,{'planId':plan['planId']},self.root,self.state)

    def test_discovery_is_read_only_and_does_not_select_new_sensors(self):
        before=copy.deepcopy(self.session.writes)
        self.session.sensors.append({'id':'new','name':'Garage','mac':'001122334499','state':'CONNECTED','isOpened':False})
        result=ss.discover(self.session,self.root)
        self.assertEqual(result['selectedIds'],['sensor-1','sensor-2'])
        self.assertFalse(next(s for s in result['sensors'] if s['id']=='sensor-bad')['available'])
        self.assertEqual(self.session.writes,before)

    def test_replace_missing_sensor_by_id_preserves_alerts_and_rule_identity(self):
        before=load_site(self.root/'site.json')
        self.session.sensors=[s for s in self.session.sensors if s['id']!='sensor-1']
        self.session.sensors.append({'id':'replacement','name':'Front','mac':'001122334499','state':'CONNECTED','isOpened':False})
        discovered=ss.discover(self.session,self.root)
        self.assertEqual(next(s for s in discovered['sensors'] if s['id']=='sensor-1')['state'],'MISSING')
        plan=ss.review(self.session,{'sensorIds':['replacement','sensor-2']},self.root,self.state)
        self.assertEqual(plan['added'],['Front']);self.assertEqual(plan['removed'],['Front'])
        ob.apply(self.session,{'planId':plan['planId']},self.root,self.state)
        after=load_site(self.root/'site.json')
        for field in ('outputs','ruleIds','webhooks','profileId'):
            self.assertEqual(before[field],after[field])
        self.assertEqual(after['sensors'][0]['id'],'replacement')
        self.assertEqual(len(self.session.rules),7)  # arm, arm_stay, arm_away, disarm, entry_alert, doors, panic
        ss.verify_for_activation(self.session,self.root)

    def test_active_system_and_unhealthy_selections_cannot_be_saved(self):
        writes=len(self.session.writes)
        with self.assertRaisesRegex(ValueError,'connected'):
            ss.review(self.session,{'sensorIds':['sensor-bad']},self.root,self.state)
        self.session.profiles[0]['state']='armed'
        with self.assertRaisesRegex(ValueError,'Disarm'):
            ss.review(self.session,{'sensorIds':['sensor-1']},self.root,self.state)
        self.assertEqual(len(self.session.writes),writes)

    def test_activation_verifies_rule_output_and_exit_delay(self):
        ss.verify_for_activation(self.session,self.root)
        self.session.profiles[0]['activation_delay']=0
        with self.assertRaisesRegex(ValueError,'profile'):
            ss.verify_for_activation(self.session,self.root)
        self.session.profiles[0]['activation_delay']=60
        next(r for r in self.session.rules if 'Door events' in r['title'])['notification_preferences']['push']=True
        with self.assertRaisesRegex(ValueError,'rules'):
            ss.verify_for_activation(self.session,self.root)

    def test_same_name_replacement_requires_new_sensor_verification(self):
        old=load_site(self.root/'site.json');new=copy.deepcopy(old);new['sensors'][0]['id']='replacement'
        observer=EntryObserver();observer.enabled=True;observer.verified_doors={'Front','Back'}
        with patch.object(server,'SITE',old),patch.object(server,'settings',return_value=({},new)),patch.object(server,'ENTRY',observer),patch.object(server,'WEBHOOKS',old['webhooks']),patch.object(server,'ENTRY_DOORS',set(old['doors'])),patch.object(server,'EXIT_UNTIL',0),patch.dict(server.UNLOCKS,{},clear=True):
            server.apply_saved_site()
            self.assertFalse(server.ENTRY.enabled)
            self.assertEqual(server.ENTRY.verified_doors,{'Back'}, 'An unchanged door keeps its genuine signal check')

    def test_each_door_needs_its_own_recent_event(self):
        observer=EntryObserver(clock=lambda:1,wall=lambda:1000)
        observer.ingest('disarmed',[{'id':'event-1','sensor':'sensor-1','type':'sensorOpened','start':1000000}],{'sensor-1':'Front','sensor-2':'Back'})
        self.assertEqual(observer.snapshot()['verifiedDoors'],['Front'])
        observer.ingest('disarmed',[{'id':'event-2','sensor':'sensor-2','type':'sensorOpened','start':1000000}],{'sensor-1':'Front','sensor-2':'Back'})
        self.assertEqual(observer.snapshot()['verifiedDoors'],['Back','Front'])


if __name__ == '__main__':
    unittest.main()

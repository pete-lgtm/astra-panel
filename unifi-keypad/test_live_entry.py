import unittest
from live_entry import EntryObserver

class LiveEntryTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.observer=EntryObserver(clock=lambda:self.now,wall=lambda:self.now)
        self.observer.ingest('armed',[],{'back'})
    def opened(self, stamp=1001000, identifier='event'):
        return {'id':identifier,'type':'sensorOpened','start':stamp,'sensor':'back'}
    def test_real_event_age_and_duplicate(self):
        self.now=1005
        self.assertEqual(self.observer.ingest('armed',[self.opened()],{'back'})['remaining'],56)
        self.now=1010
        self.assertEqual(self.observer.ingest('armed',[self.opened()],{'back'})['remaining'],51)
        self.observer.ingest('armed',[self.opened(1010000,'second')],{'back'})
        self.assertEqual(self.observer.snapshot()['remaining'],51)
    def test_confirmed_disarm_cancels(self):
        self.now=1005;self.observer.ingest('armed',[self.opened()],{'back'})
        self.now=1020;self.assertIsNone(self.observer.ingest('disarmed',[],{'back'})['remaining'])
    def test_loss_preserves_deadline_and_stale_is_unknown(self):
        self.now=1005;self.observer.ingest('armed',[self.opened()],{'back'})
        self.now=1010;self.assertEqual(self.observer.fail('Disconnected')['remaining'],51)
        self.assertEqual(self.observer.snapshot()['state'],'unknown')
        self.now=1061;self.observer.snapshot();self.assertIn('expired',self.observer.result)
    def test_other_sensor_and_prearm_event_ignored(self):
        self.now=1005;self.observer.ingest('armed',[self.opened(999000),{**self.opened(identifier='other'),'sensor':'front'}],{'back'})
        self.assertIsNone(self.observer.deadline)
    def test_disarmed_event_not_replayed_after_arming(self):
        self.now=1005;self.observer.ingest('disarmed',[self.opened()],{'back'})
        self.now=1010;self.observer.ingest('armed',[self.opened()],{'back'})
        self.assertIsNone(self.observer.deadline)
    def test_no_live_output_claim_and_stale(self):
        self.now=1020
        data=self.observer.snapshot()
        self.assertFalse(data['liveDelayEnabled']);self.assertFalse(data['fresh'])
        self.assertEqual(data['state'],'unknown')


class LiveOutputTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.observer=EntryObserver(clock=lambda:self.now,wall=lambda:self.now)
        self.observer.ingest('disarmed',[],{'back'})
        self.observer.sensor_update({'isOpened':False,'openStatusChangedAt':999000})
        self.now=1001
        self.observer.sensor_update({'isOpened':True,'openStatusChangedAt':1001000})
        self.observer.enable()
        self.now=1010
        self.observer.ingest('armed',[],{'back'})
        self.observer.sensor_update({'isOpened':False,'openStatusChangedAt':1002000})
        self.now=1011
        self.observer.sensor_update({'isOpened':True,'openStatusChangedAt':1011000})
    def test_expiry_sends_once(self):
        sent=[]
        self.now=1070;self.observer.dispatch(lambda:sent.append(1));self.assertEqual(sent,[])
        self.now=1071;self.observer.dispatch(lambda:sent.append(1));self.observer.dispatch(lambda:sent.append(1))
        self.assertEqual(sent,[1])
    def test_confirmed_disarm_prevents_output(self):
        sent=[]
        self.now=1060;self.observer.ingest('disarmed',[],{'back'})
        self.now=1072;self.observer.dispatch(lambda:sent.append(1));self.assertEqual(sent,[])
    def test_uncertain_post_not_retried(self):
        def fail(): raise ValueError('Timed out')
        self.now=1072;self.observer.dispatch(fail)
        self.assertEqual(self.observer.output,'uncertain')
        sent=[];self.observer.dispatch(lambda:sent.append(1));self.assertEqual(sent,[])
    def test_unknown_does_not_cancel_pending_timer(self):
        self.now=1040;self.observer.fail('Connection lost')
        self.now=1072;sent=[];self.observer.dispatch(lambda:sent.append(1));self.assertEqual(sent,[1])
    def test_unverified_enable_rejected(self):
        other=EntryObserver()
        with self.assertRaises(ValueError): other.enable()


class MultiDoorTests(unittest.TestCase):
    def test_exit_event_delivered_after_arming_does_not_start_entry(self):
        now=[1000]
        observer=EntryObserver(clock=lambda:now[0],wall=lambda:now[0])
        observer.ingest('arming',[],{'front'},'1970-01-01T00:16:40Z')
        observer.sensor_update({'name':'Front Door','isOpened':False,'openStatusChangedAt':900000})
        now[0]=1061
        observer.ingest('armed',[],{'front'},'1970-01-01T00:17:40Z')
        observer.sensor_update({'name':'Front Door','isOpened':False,'openStatusChangedAt':1059000})
        self.assertIsNone(observer.deadline)
        now[0]=1062
        observer.sensor_update({'name':'Front Door','isOpened':True,'openStatusChangedAt':1061500})
        self.assertEqual(observer.deadline,1121.5)

    def test_breached_remains_active_and_disarm_clears_old_message(self):
        now=[1000]
        observer=EntryObserver(clock=lambda:now[0],wall=lambda:now[0])
        observer.ingest('armed',[],{'front'})
        observer.sensor_update({'name':'Front Door','isOpened':False,'openStatusChangedAt':900000})
        now[0]=1002
        observer.ingest('breached',[],{'front'})
        observer.sensor_update({'name':'Front Door','isOpened':True,'openStatusChangedAt':1001000})
        self.assertEqual(observer.snapshot()['state'],'breached')
        self.assertEqual(observer.deadline,1061)
        observer.result='Delayed notification request accepted by Protect.'
        observer.deadline=None;observer.output='accepted'
        observer.ingest('disarmed',[],{'front'})
        self.assertEqual(observer.snapshot()['notification'],'not_sent')
        self.assertNotIn('accepted',observer.snapshot()['message'])

    def test_confirmed_exit_countdown_does_not_restart(self):
        now=[1000]
        observer=EntryObserver(clock=lambda:now[0],wall=lambda:now[0])
        observer.ingest('arming',[],{'front'},'1970-01-01T00:16:40Z')
        self.assertEqual(observer.snapshot()['exitRemaining'],60)
        now[0]=1030
        observer.ingest('arming',[],{'front'},'1970-01-01T00:16:40Z')
        self.assertEqual(observer.snapshot()['exitRemaining'],30)
        observer.ingest('armed',[],{'front'})
        self.assertIsNone(observer.snapshot()['exitRemaining'])

    def test_separate_baselines_and_shared_deadline(self):
        now=[1000]
        observer=EntryObserver(clock=lambda:now[0],wall=lambda:now[0])
        observer.ingest('armed',[],{'front':'Front Door','back':'Back Door'})
        def update(name, opened, stamp, state='CONNECTED'):
            observer.sensor_update({'name':name,'isOpened':opened,'openStatusChangedAt':stamp,'state':state})
        update('Front Door',False,900000)
        update('Back Door',False,950000)
        update('Front Door',False,900000)
        self.assertIsNone(observer.deadline)
        now[0]=1001;update('Front Door',True,1001000)
        self.assertEqual(observer.deadline,1061)
        now[0]=1010;update('Back Door',True,1010000)
        self.assertEqual(observer.deadline,1061)
        observer.ingest('disarmed',[],{'front','back'})
        self.assertIsNone(observer.deadline)
    def test_offline_and_unknown_are_not_valid_open_events(self):
        observer=EntryObserver()
        observer.ingest('armed',[],{'master'})
        observer.sensor_update({'name':'Master Bed Door','isOpened':False})
        observer.sensor_update({'name':'Master Bed Door','isOpened':True,'state':'DISCONNECTED'})
        self.assertIsNone(observer.deadline)
        observer.sensor_update({'name':'Master Bed Door','isOpened':None,'state':'CONNECTED'})
        self.assertEqual(observer.snapshot()['doors']['Master Bed Door']['position'],'unknown')
        self.assertFalse(observer.verified_event)

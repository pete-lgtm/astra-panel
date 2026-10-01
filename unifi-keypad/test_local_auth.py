import unittest
from unittest.mock import patch, MagicMock
from local_auth import LocalSession

class LocalLoginTests(unittest.TestCase):
    def test_password_not_retained_and_only_profiles_returned(self):
        client=LocalSession()
        with patch.object(client,'request',side_effect=[({},'TOKEN=private'),([{'title':'Arm','state':'disarmed','secret':'hidden'}],None)]):
            result=client.login('192.168.0.1','local-test','test-only-password')
        self.assertEqual(result['profiles'],[{'id':None,'title':'Arm','state':'disarmed','state_set_at':None}])
        self.assertNotIn('test-only-password',str(client.__dict__))
        self.assertNotIn('TOKEN',str(result))
    def test_no_cookie_is_not_success(self):
        client=LocalSession()
        with patch.object(client,'request',return_value=({},None)):
            with self.assertRaises(ValueError): client.login('192.168.0.1','test','test-password')
        self.assertIsNone(client.cookie)
    def test_mismatch_prevents_sending_password(self):
        client=LocalSession(); connection=MagicMock()
        connection.sock.getpeercert.return_value=b'wrong certificate'
        with patch.dict('os.environ',{'UNIFI_CERT_SHA256':'a'*64}),patch('local_auth.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaisesRegex(ValueError,'Credentials were not sent'):
                client.login('192.168.0.1','test','test-password')
        connection.request.assert_not_called()
    def test_rejection_does_not_expose_response(self):
        client=LocalSession(); connection=MagicMock(); response=connection.getresponse.return_value
        response.status=401;response.read.return_value=b'{"password":"secret"}'
        with patch.dict('os.environ',{'UNIFI_CERT_SHA256':''}),patch('local_auth.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(ValueError) as error: client.login('192.168.0.1','test','secret')
        self.assertNotIn('secret',str(error.exception))
        self.assertIsNone(client.cookie)

class MfaTests(unittest.TestCase):
    def challenge(self, client):
        from local_auth import MfaRequired
        with patch.object(client,'request',side_effect=MfaRequired('mfa=test-only')):
            result=client.login('192.168.0.1','test','private-password')
        self.assertTrue(result['mfaRequired'])
        self.assertNotIn('test-only',str(result))
        self.assertNotIn('private-password',str(client.__dict__))
    def test_challenge_completion_and_cleanup(self):
        client=LocalSession(); self.challenge(client)
        with patch.object(client,'request',side_effect=[({},'TOKEN=test'),([],None)]) as request:
            result=client.login('192.168.0.1','test','private-password','123456')
        self.assertTrue(result['localConnected'])
        self.assertEqual(request.call_args_list[0].kwargs['cookie'],'mfa=test-only')
        self.assertEqual(request.call_args_list[0].args[3]['token'],'123456')
        self.assertIsNone(client.pending)
    def test_expired_or_changed_challenge_sends_nothing(self):
        for changed in ('expired','host','username'):
            client=LocalSession(); self.challenge(client)
            if changed=='expired': client.pending['expires']=0
            with patch.object(client,'request') as request:
                with self.assertRaises(ValueError):
                    client.login('192.168.0.2' if changed=='host' else '192.168.0.1','other' if changed=='username' else 'test','private-password','123456')
            request.assert_not_called()
            self.assertIsNone(client.pending)
    def test_failed_verification_clears_challenge(self):
        client=LocalSession(); self.challenge(client)
        with patch.object(client,'request',side_effect=ValueError('Rejected')):
            with self.assertRaises(ValueError): client.login('192.168.0.1','test','private-password','123456')
        self.assertIsNone(client.pending)
        self.assertIsNone(client.cookie)

class FailureStageTests(unittest.TestCase):
    def test_failure_stages_are_distinct_and_do_not_expose_secrets(self):
        for path,payload,expected in [('/api/auth/login',{'token':'123456'},'verification sign-in'),('/api/auth/login',{'password':'secret'},'username/password'),('/api/v2/alarms/profiles',None,'account permissions')]:
            client=LocalSession(); connection=MagicMock(); response=connection.getresponse.return_value
            response.status=403;response.read.return_value=b'{"private":"secret"}'
            with patch.dict('os.environ',{'UNIFI_CERT_SHA256':''}),patch('local_auth.http.client.HTTPSConnection',return_value=connection):
                with self.assertRaises(ValueError) as error: client.request('192.168.0.1','POST' if payload else 'GET',path,payload)
            self.assertIn(expected,str(error.exception))
            self.assertNotIn('secret',str(error.exception))
            self.assertNotIn('123456',str(error.exception))

class ModernCookieTests(unittest.TestCase):
    def test_modern_attributes_do_not_drop_session(self):
        for name in ('TOKEN','UOS_TOKEN'):
            client=LocalSession(); connection=MagicMock(); response=connection.getresponse.return_value
            response.status=200;response.read.return_value=b'{}'
            response.getheaders.return_value=[('Set-Cookie','unrelated=ignored; Path=/'),('Set-Cookie',name+'=test-session; Path=/; Secure; HttpOnly; SameSite=Strict; Partitioned')]
            with patch.dict('os.environ',{'UNIFI_CERT_SHA256':''}),patch('local_auth.http.client.HTTPSConnection',return_value=connection):
                body,cookie=client.request('192.168.0.1','POST','/api/auth/login',{'username':'test','password':'test'})
            self.assertEqual(cookie,name+'=test-session')
    def test_attribute_is_not_misread_as_session(self):
        client=LocalSession(); connection=MagicMock(); response=connection.getresponse.return_value
        response.status=200;response.read.return_value=b'{}'
        response.getheaders.return_value=[('Set-Cookie','unrelated=ignored; TOKEN=wrong; Partitioned')]
        with patch.dict('os.environ',{'UNIFI_CERT_SHA256':''}),patch('local_auth.http.client.HTTPSConnection',return_value=connection):
            _,cookie=client.request('192.168.0.1','GET','/api/v2/alarms/profiles')
        self.assertIsNone(cookie)

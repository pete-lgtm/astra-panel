"""Gateway internet tests through UniFi Network's compatibility API.

Protocol checked against Network 10.6's speedTest saga (summary/download/upload:
0 pending, 1 running, 2 completed, 3 failed) and vedanta/ui-cli's speedtest command.
Only the two fixed devmgr commands are allowed. Never run a test on this host,
change WAN settings, return raw device records, or replay a start after failure.
"""
from copy import deepcopy
import threading
import time

from local_auth import ConsoleRequestError
from network_inventory import NetworkReader, SITE_NAME, entries, normalize_device, number
from protect_setup_api import csrf_from_cookie


class SpeedTestError(ValueError):
    pass


def timestamp(value):
    value = number(value, maximum=4_000_000_000_000)
    if not value:
        return 0
    return int(value if value > 100_000_000_000 else value * 1000)


def result_from(row):
    if any(type(row.get(k)) is not int or row[k] != 2
           for k in ('status_summary', 'status_download', 'status_upload')):
        return None
    down, up = number(row.get('xput_download')), number(row.get('xput_upload'))
    at = timestamp(row.get('timestamp'))
    if down is None or up is None or not at:
        return None
    return {'downloadMbps': down, 'uploadMbps': up,
            'latencyMs': number(row.get('latency'), maximum=60_000), 'testedAt': at}


class Reader(NetworkReader):
    def __init__(self, host, cookie, site):
        super().__init__(host, cookie)
        if not isinstance(site, str) or not SITE_NAME.fullmatch(site):
            raise SpeedTestError('Choose a Network site first.')
        self.site = site
        self.base = '/proxy/network/api/s/' + site

    def gateway(self):
        sites = entries(self.get('/proxy/network/api/self/sites'), 256)
        if not any(s.get('name') == self.site for s in sites):
            raise SpeedTestError('This Network site is no longer available to your account.')
        devices = [normalize_device(raw) for raw in entries(self.get(self.base + '/stat/device'), 500)]
        gateways = [d for d in devices if d and d['kind'] == 'gateway']
        if len(gateways) != 1 or gateways[0]['state'] != 'online':
            raise SpeedTestError('An online UniFi gateway is needed. Check the gateway in UniFi Network.')
        return {key: gateways[0][key] for key in ('id', 'name')}

    def command(self, command):
        if command not in ('speedtest', 'speedtest-status'):
            raise SpeedTestError('Unsupported speed test request.')
        body, rotated = self.session.request(self.host, 'POST', self.base + '/cmd/devmgr',
            {'cmd': command}, cookie=self.cookie,
            extra_headers={'X-CSRF-Token': csrf_from_cookie(self.cookie)})
        if rotated:
            self.cookie = rotated
        if (not isinstance(body, dict) or not isinstance(body.get('meta'), dict) or
                body['meta'].get('rc') != 'ok' or not isinstance(body.get('data'), list)):
            raise SpeedTestError('UniFi did not confirm this speed test request. Check it in UniFi Network.')
        return entries(body['data'], 16)

    def observation(self):
        rows = self.command('speedtest-status')
        if len(rows) != 1 or any(type(rows[0].get(k)) is not int or rows[0][k] not in (-1, 0, 1, 2, 3)
                for k in ('status_summary', 'status_download', 'status_upload')):
            raise SpeedTestError('This gateway did not provide a supported speed test status. Use UniFi Network.')
        # Pending (0) is also the idle value, so consult the internet health state.
        body, _ = self.session.request(self.host, 'GET', self.base + '/stat/health', cookie=self.cookie)
        if (not isinstance(body, dict) or not isinstance(body.get('meta'), dict) or
                body['meta'].get('rc') != 'ok'):
            raise SpeedTestError('Could not check whether the gateway is already testing.')
        internet = [r for r in entries(body.get('data'), 64) if r.get('subsystem') == 'www']
        if len(internet) != 1:
            raise SpeedTestError('Internet test status is unavailable on this gateway.')
        row = rows[0]
        running = (row['status_summary'] == 1 or
                   str(internet[0].get('speedtest_status', '')).lower() in ('running', 'pending'))
        return row, running


class SpeedTests:
    """Independent lock: slow Network calls never hold the alarm controller lock.

    Jobs outlive browser PIN grants. Closing/locking stops UI polling, not the
    gateway's test. Another authenticated visit can read its eventual result.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}

    def request(self, host, cookie, site, *, start=False, authorize=lambda: None):
        if not self.lock.acquire(blocking=False):
            raise SpeedTestError('The gateway is being checked. Try again in a moment.')
        try:
            return self._request(host, cookie, site, start, authorize)
        except SpeedTestError:
            raise
        except ConsoleRequestError as error:
            if error.status in (401, 403):
                raise SpeedTestError('UniFi denied speed test access. Check this account’s Network permissions.') from None
            raise SpeedTestError('Speed tests are unavailable from this gateway right now.') from None
        except (ValueError, TypeError, KeyError, OSError):
            raise SpeedTestError('Could not read the gateway test. Check the connection and try again.') from None
        finally:
            self.lock.release()

    def _request(self, host, cookie, site, start, authorize):
        reader = Reader(host, cookie, site)
        now = time.monotonic()
        self.jobs = {k: v for k, v in self.jobs.items() if now - v['checkedAt'] < 900}
        key = (host, site)
        job = self.jobs.get(key)
        # Validate site access and gateway identity on opening, periodically, and
        # before every start. Cache only the normalized identity, never secrets.
        if not job or start or now - job['validatedAt'] > 30:
            gateway = reader.gateway()
            if not job or job['gateway']['id'] != gateway['id']:
                if len(self.jobs) >= 16:
                    raise SpeedTestError('Too many pending gateway checks. Try again shortly.')
                job = {'gateway': gateway, 'validatedAt': now, 'checkedAt': now,
                       'startedAt': None, 'baseline': 0, 'lastResult': None, 'uncertain': False}
                self.jobs[key] = job
            job.update(gateway=gateway, validatedAt=now)
        row, native_running = reader.observation()
        now = time.monotonic()
        job['checkedAt'] = now
        result = result_from(row)
        stamp = timestamp(row.get('timestamp'))
        age = now - job['startedAt'] if job['startedAt'] is not None else None
        new = age is not None and stamp > job['baseline']
        if result:
            job['lastResult'] = result
        done = new and result is not None
        failed = new and row['status_summary'] == 3
        waiting = age is not None and age < 120 and not done and not failed
        can_start = not native_running and not waiting and (age is None or age >= 180 or done or failed)
        sent = False
        if start and can_start:
            # Last authorization check occurs after reads, directly before the
            # one write. Record the attempt first: a timeout is never retried.
            authorize()
            job.update(startedAt=now, baseline=stamp, uncertain=False)
            sent = True
            try:
                reader.command('speedtest')
            except (ValueError, TypeError, OSError):
                job['uncertain'] = True
            waiting, done, failed, can_start, age = True, False, False, False, 0
        if waiting or (native_running and (age is None or age < 120)):
            state = 'running'
            phase = ('Uploading…' if row['status_upload'] == 1 else
                     'Downloading…' if row['status_download'] == 1 else 'Testing your internet connection…')
            message = ('Start could not be confirmed. Checking the gateway; no second test was sent.'
                       if job['uncertain'] else 'Usually takes about a minute. You can close this screen while it runs.')
        elif done:
            state, phase, message = 'completed', 'Test complete', 'Measured by your UniFi gateway.'
        elif failed:
            state, phase, message = 'failed', 'Test failed', 'UniFi could not finish this test. Check your internet connection.'
        elif age is not None:
            state, phase = 'timeout', 'Result not confirmed'
            message = 'UniFi has not returned a new completed result. Check UniFi Network before trying again.'
        else:
            state, phase, message = 'idle', 'Ready to test', 'Measures download, upload, and latency on your default internet connection.'
        return {'gateway': deepcopy(job['gateway']), 'state': state, 'phase': phase, 'message': message,
                'canStart': can_start, 'started': sent, 'result': deepcopy(job['lastResult']), 'pollAfterSeconds': 5 if state == 'running' else None}

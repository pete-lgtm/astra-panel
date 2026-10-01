"""Local UniFi Protect proof of concept. Python standard library only."""
import http.client
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from entry_delay import EntryDelay
from local_auth import LocalSession
from live_entry import EntryObserver, configured_alarm_state
from device_config import settings, selected_sensors
import onboarding
import sensor_settings
import alarm_outputs
import siren_runtime
import entry_recovery
import network_inventory
import network_actions
import network_wifi
import network_speedtest
import sonos_audio
import hue_lighting
import protection_state
import door_policy
import arm_coverage
import credential_store
import phone_pairing
import browser_setup
import power_monitor
import fleet_heartbeat
from urllib.parse import urlsplit
from pin_store import create_pin, read_pin
from settings_access import SettingsAccess, SettingsLocked, protected_route
from keypad_store import KeypadStore, ARM_ACTIONS

ROOT = Path(__file__).parent
PORT = int(os.environ.get('KEYPAD_PORT', '8765'))
ORIGIN = f'http://127.0.0.1:{PORT}'
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
CONFIG = {}
INSTALLER_STATE = {}
PIN = {}
STATE_DIR, SITE = settings()
PIN_FILE = STATE_DIR / '.keypad-pin.json'
CREDENTIAL_FILE = STATE_DIR / '.api-connection.json'
LOCAL_SESSION = LocalSession(STATE_DIR / '.unifi-session.json')
VERSION = Path(__file__).with_name('VERSION').read_text().strip()
INSTANCE = secrets.token_hex(16)
KIOSK_ORIGIN = 'http://127.0.0.1:8764' if os.environ.get('KEYPAD_APPLIANCE') == '1' else None
UNLOCKS = {}
NETWORK_PLANS = {}
NETWORK_SPEEDTESTS = network_speedtest.SpeedTests()
SONOS_AUDIO = sonos_audio.AudioInventory()
HUE_LIGHTING = hue_lighting.HueLighting(STATE_DIR / '.lighting-hue.json')
ARM_PLANS = {}
STATUS_SENSORS = []
SIREN_MESSAGE = ''
NETWORK_ACTION_LOCK = threading.Lock()
EXIT_UNTIL = 0
WEBHOOKS = SITE['webhooks']

def set_pin(pin):
    if PIN:
        raise ValueError('PIN already set. Use an authenticated PIN change workflow to replace it.')
    saved = create_pin(PIN_FILE, pin, pin)
    PIN.update(**saved, failures=0, blocked_until=0)


def load_pin():
    saved = read_pin(PIN_FILE)
    if saved is not None:
        PIN.update(**saved, failures=0, blocked_until=0)


def check_pin(pin):
    return account_store().authenticate(pin)

def account_store():
    return KeypadStore(PIN_FILE)

SETTINGS_ACCESS = SettingsAccess(lambda: PIN_FILE, PORT, verifier=check_pin)

def unlocked_user(unlock, consume=False):
    grant = (UNLOCKS.pop(unlock, None) if consume else UNLOCKS.get(unlock)) if isinstance(unlock, str) else None
    if not isinstance(grant, dict) or grant['expires'] <= time.monotonic():
        raise ValueError('Keypad locked. Enter your PIN again.')
    return account_store().current(grant['user'])


def hub_activity(data):
    """Renew an existing grant on interaction, never from status polling."""
    unlock = data.get('unlock')
    unlocked_user(unlock)
    preferences = account_store().panel_settings()
    seconds = preferences['autoLockSeconds']
    UNLOCKS[unlock]['expires'] = time.monotonic() + seconds
    return {**preferences, 'expiresIn': seconds}


def hub_capabilities(data):
    # Discovery must not hold the alarm controller's lock during network I/O.
    with LOCK:
        unlocked_user(data.get('unlock'))
        connection = dict(CONFIG)
    cameras, discovered = [], False
    if connection:
        try:
            response = request_unifi('GET', 'cameras', config=connection)
            if isinstance(response, list):
                cameras = [{'id': camera['id'], 'name': str(camera.get('name') or 'Camera')[:120],
                            'state': camera.get('state') if camera.get('state') in ('CONNECTED', 'CONNECTING', 'DISCONNECTED') else 'UNKNOWN'}
                           for camera in response if isinstance(camera, dict) and isinstance(camera.get('id'), str)]
                discovered = True
        except ValueError:
            pass  # Camera discovery cannot disable the alarm or reveal API errors/secrets.
    with LOCK:
        unlocked_user(data.get('unlock'))
        if CONFIG != connection:
            cameras, discovered = [], False
    return {'cameras': cameras, 'camerasDiscovered': discovered}


def hub_network(data):
    with LOCK:
        actor = unlocked_user(data.get('unlock'))
        if actor['role'] != 'admin':
            return {'state': 'admin_required', 'message': 'Unlock with an administrator PIN to view equipment.',
                    'sites': [], 'site': None, 'devices': [], 'observedAt': None}
        host, cookie, generation = LOCAL_SESSION.host, LOCAL_SESSION.cookie, LOCAL_SESSION.generation
    result = network_inventory.discover(host, cookie, data.get('site'))
    with LOCK:
        actor = unlocked_user(data.get('unlock'))
        if actor['role'] != 'admin' or LOCAL_SESSION.host != host or LOCAL_SESSION.generation != generation:
            # Includes a sign-out, console switch, or new sign-in during the read.
            # Normal cookie rotation by the alarm observer is not a new login.
            raise ValueError('The UniFi connection changed. Refresh the device list.')
    result['actionsAvailable'] = True
    result['wifiPasswordsAvailable'] = True
    result['speedTestAvailable'] = True
    return result


def hub_audio(data, artwork=False):
    with LOCK:
        unlocked_user(data.get('unlock'))
    # Local music reads never hold the alarm lock or extend the PIN grant.
    result = ({'artworkId': data.get('artworkId'), 'image': SONOS_AUDIO.artwork(data.get('artworkId'))}
              if artwork else SONOS_AUDIO.read(rescan=data.get('rescan') is True))
    with LOCK:
        unlocked_user(data.get('unlock'))
    return result


def hub_audio_control(data, grouping=False):
    with LOCK:
        actor = unlocked_user(data.get('unlock'))
    def authorize():
        with LOCK:
            if unlocked_user(data.get('unlock'))['id'] != actor['id']:
                raise ValueError('Unlock with your PIN again.')
    action = SONOS_AUDIO.group if grouping else SONOS_AUDIO.control
    result = action({key: val for key, val in data.items() if key != 'unlock'}, authorize)
    authorize()
    return result


def hub_lighting(data, action='read'):
    manage = action in ('discover', 'prepare', 'pair', 'disconnect')
    with LOCK:
        actor = unlocked_user(data.get('unlock'))
        if manage and actor['role'] != 'admin':
            raise ValueError('Unlock with an administrator PIN to connect lighting.')

    def authorize():
        with LOCK:
            current = unlocked_user(data.get('unlock'))
            if current['id'] != actor['id'] or (manage and current['role'] != 'admin'):
                raise ValueError('Unlock with your PIN again.')

    # Bridge reads and writes never hold the alarm controller lock or renew a PIN.
    if action == 'read':
        result = HUE_LIGHTING.read(authorize)
    elif action == 'discover':
        try:
            result = {'bridges': hue_lighting.discover()}
        except OSError:
            raise ValueError('Could not search the network. Enter the Hue Bridge address instead.') from None
    elif action == 'prepare':
        result = HUE_LIGHTING.prepare(data.get('host'), data.get('unlock'), authorize)
    elif action == 'pair':
        result = HUE_LIGHTING.pair(data.get('pairId'), data.get('unlock'), authorize)
    elif action == 'disconnect':
        if data.get('confirm') is not True:
            raise ValueError('Confirm disconnecting this Hue Bridge.')
        result = HUE_LIGHTING.disconnect(authorize)
    elif action == 'control':
        result = HUE_LIGHTING.control({k: v for k, v in data.items() if k != 'unlock'}, authorize)
    else:
        raise ValueError('Unsupported lighting action.')
    authorize()
    result['canManage'] = actor['role'] == 'admin'
    return result


def gateway_speedtest(data, start=False):
    with LOCK:
        actor = network_actor(data)
        host, cookie, generation = LOCAL_SESSION.host, LOCAL_SESSION.cookie, LOCAL_SESSION.generation

    def authorize():
        with LOCK:
            current = network_actor(data, host, generation)
            if current['id'] != actor['id']:
                raise ValueError('Unlock with your administrator PIN again.')

    # Starting and polling do not hold the alarm lock, renew PIN grants, or
    # change the live console session. All upstream routes are fixed in adapter.
    result = NETWORK_SPEEDTESTS.request(host, cookie, data.get('site'), start=start, authorize=authorize)
    authorize()
    if result.pop('started', False):
        account_store().record(actor, 'internet_speed_test', 'Requested a gateway internet speed test.')
    return result


def reveal_wifi_passwords(data):
    with LOCK:
        actor = unlocked_user(data.get('unlock'))
        if actor['role'] != 'admin':
            raise ValueError('Unlock with an administrator PIN to view Wi-Fi passwords.')
        verified = check_pin(data.pop('pin', None))
        if verified['id'] != actor['id'] or verified['role'] != 'admin':
            raise ValueError('Enter the PIN for the administrator currently using this panel.')
        host, cookie, generation = LOCAL_SESSION.host, LOCAL_SESSION.cookie, LOCAL_SESSION.generation
    # Wi-Fi discovery must not block the alarm observer or disarm commands.
    result = network_wifi.reveal(host, cookie, data.get('site'))
    with LOCK:
        current = unlocked_user(data.get('unlock'))
        if (current['id'] != actor['id'] or current['role'] != 'admin' or
                LOCAL_SESSION.host != host or LOCAL_SESSION.generation != generation):
            raise ValueError('The connection changed. Enter your PIN again to view Wi-Fi passwords.')
        account_store().record(current, 'wifi_passwords_viewed', 'Viewed saved Wi-Fi passwords.')
    return dict(result, hideAfterSeconds=30)


def network_actor(data, host=None, generation=None, disruptive=False):
    """Caller holds LOCK. An equipment action always belongs to an admin PIN."""
    actor = unlocked_user(data.get('unlock'))
    if actor['role'] != 'admin':
        raise ValueError('Unlock with an administrator PIN to control equipment.')
    if not LOCAL_SESSION.cookie or (host is not None and (LOCAL_SESSION.host != host or LOCAL_SESSION.generation != generation)):
        raise ValueError('The UniFi connection changed. Reconnect and review the action again.')
    if disruptive:
        with ENTRY_LOCK:
            observed = ENTRY.snapshot()
        if not observed['fresh'] or observed['state'] != 'disarmed':
            raise ValueError('Disarm the alarm before restarting equipment or switching power.')
    return actor


def review_network_action(data):
    request = network_actions.clean_request(data.get('request'))
    with LOCK:
        actor = network_actor(data, disruptive=request['action'] in network_actions.DISRUPTIVE)
        host, cookie, generation = LOCAL_SESSION.host, LOCAL_SESSION.cookie, LOCAL_SESSION.generation
    plan = network_actions.prepare(host, cookie, request)
    with LOCK:
        network_actor(data, host, generation, request['action'] in network_actions.DISRUPTIVE)
        now = time.monotonic()
        for key, value in list(NETWORK_PLANS.items()):
            if value['expires'] <= now or value['unlock'] == data.get('unlock'):
                del NETWORK_PLANS[key]
        if len(NETWORK_PLANS) >= 64:
            raise ValueError('Too many pending changes. Try again shortly.')
        identifier = secrets.token_urlsafe(24)
        # No raw record, cookie, override array, or request path is stored/sent.
        NETWORK_PLANS[identifier] = {'request': request, 'digest': plan['digest'], 'expires': now + 45,
                                     'actor': actor['id'], 'unlock': data['unlock'], 'host': host, 'generation': generation}
    return {'planId': identifier, 'target': plan['title'], 'effect': plan['effect'], 'expiresIn': 45}


def commit_network_action(data):
    if data.get('confirm') is not True or not isinstance(data.get('planId'), str):
        raise ValueError('Review and confirm this equipment change first.')
    with LOCK:
        actor = network_actor(data)
        saved = NETWORK_PLANS.get(data['planId'])
        if not saved or saved['unlock'] != data.get('unlock') or saved['actor'] != actor['id']:
            raise ValueError('Review this equipment change again.')
        del NETWORK_PLANS[data['planId']]
        if saved['expires'] <= time.monotonic():
            raise ValueError('This review expired. Review the change again.')
        network_actor(data, saved['host'], saved['generation'], saved['request']['action'] in network_actions.DISRUPTIVE)
        cookie = LOCAL_SESSION.cookie
    if not NETWORK_ACTION_LOCK.acquire(blocking=False):
        raise ValueError('Another equipment change is running. Wait, then review again.')
    try:
        plan = network_actions.prepare(saved['host'], cookie, saved['request'])
        with LOCK:
            actor = network_actor(data, saved['host'], saved['generation'], saved['request']['action'] in network_actions.DISRUPTIVE)
            if saved['expires'] <= time.monotonic() or plan['digest'] != saved['digest']:
                raise ValueError('This equipment changed after review. Refresh and review again.')
            action = saved['request']['action']
            store = account_store()
            event = store.start_equipment_command(actor, 'network_' + action, plan['title'])
        # Equipment I/O must never hold the alarm observer's lock.
        try:
            result = network_actions.execute(plan)
        except network_actions.ActionError as error:
            status = 'unverified' if isinstance(error, network_actions.UncertainAction) else 'rejected'
            store.command_result(event, status, plan['title'] + ' · ' + str(error))
            return {'status': status, 'message': str(error)}
        except Exception:
            store.command_result(event, 'unverified', plan['title'] + ' · Result unknown; check UniFi before retrying.')
            return {'status': 'unverified', 'message': 'Result unknown. Check UniFi before trying again.'}
        store.command_result(event, result['status'], plan['title'] + ' · ' + result['message'])
        return result
    finally:
        NETWORK_ACTION_LOCK.release()


def coverage_status():
    with ENTRY_LOCK:
        result = ENTRY.snapshot()
    preferences = door_policy.read(ROOT, SITE)
    result['arming'] = door_policy.assess(SITE, result, preferences)
    result['statusSensors'] = list(STATUS_SENSORS)
    result['sirenMessage'] = SIREN_MESSAGE
    result['apiConfigured'] = bool(CONFIG)
    result['localConnected'] = bool(LOCAL_SESSION.cookie)
    result['recoveryMessage'] = RECOVERY_MESSAGE if not result.get('liveDelayEnabled') else ''
    # Acceptance is not confirmed arming. Keep a bounded estimated countdown
    # while the profile update is in flight, including after a browser reload.
    result['pendingArmSeconds'] = (max(0, EXIT_UNTIL - time.monotonic())
                                   if result['state'] in ('disarmed', 'unknown') else 0)
    return result


def mute_bypass_reminders(data):
    """Dismiss the 'Bypass needed' banner for 1, 7, or 30 days. Any unlocked
    user may snooze it; the banner returns early if a door's issue changes or
    the alarm configuration changes. Alarms are never muted."""
    actor = unlocked_user(data.get('unlock'))
    try:
        days = int(data.get('days', 0))
    except (TypeError, ValueError):
        days = 0
    if days not in door_policy.MUTE_DAY_CHOICES:
        raise ValueError('Choose 1, 7, or 30 days.')
    bypassed = coverage_status()['arming']['bypassed']
    if not bypassed:
        raise ValueError('There is nothing to dismiss right now.')
    door_policy.mute(ROOT, SITE, bypassed, days=days)
    names = sorted(s['name'] for s in bypassed)
    account_store().record(actor, 'bypass_reminders_muted',
                           'Bypass reminders dismissed for %d days: %s.' % (days, ', '.join(names)))
    return {'mutedDays': days, 'muted': names}


def review_arm(data):
    actor = unlocked_user(data.get('unlock'))
    if EXIT_UNTIL > time.monotonic():
        raise ValueError('An arm request is already counting down. Check Protect.')
    mode = str(data.get('mode') or 'arm').replace('-', '_')
    if mode not in ARM_ACTIONS:
        raise ValueError('Unknown arm mode.')
    if mode != 'arm' and mode not in WEBHOOKS:
        raise ValueError('Arm Stay/Away are not set up yet. Run alarm setup first.')
    observed = coverage_status()
    coverage = observed['arming']
    if not coverage['canReview']:
        raise ValueError('Protect must confirm disarmed, with at least one checked, connected, closed alarm door.')
    if not CONFIG or not LOCAL_SESSION.cookie:
        raise ValueError('Reconnect UniFi before arming.')
    ids = [s['id'] for s in coverage['active']]
    plan = arm_coverage.prepare(LOCAL_SESSION, SITE, ROOT, ids, mode=mode)
    ARM_PLANS.clear()
    identifier = secrets.token_urlsafe(24)
    ARM_PLANS[identifier] = {'coverage': coverage, 'baseline': plan['baseline'], 'actor': actor['id'],
                             'unlock': data.get('unlock'), 'expires': time.monotonic() + 45,
                             'mode': mode,
                             'configuration': protection_state.fingerprint(SITE), 'generation': LOCAL_SESSION.generation}
    return {**coverage, 'planId': identifier, 'expiresIn': 45}


def commit_arm(data, actor, action='arm'):
    saved = ARM_PLANS.pop(data.get('planId'), None) if isinstance(data.get('planId'), str) else None
    if (not saved or saved['unlock'] != data.get('unlock') or saved['actor'] != actor['id'] or
            saved['expires'] <= time.monotonic() or saved['generation'] != LOCAL_SESSION.generation or
            saved['configuration'] != protection_state.fingerprint(SITE) or
            saved.get('mode', 'arm') != action):
        raise ValueError('Review the current door coverage before arming.')
    observed = coverage_status()
    coverage = observed['arming']
    if not coverage['canReview'] or door_policy.signature(coverage) != door_policy.signature(saved['coverage']):
        raise ValueError('Door status changed. Review the doors again before arming.')
    if coverage['needsConfirmation'] and data.get('confirmBypass') is not True:
        raise ValueError('Confirm the listed bypassed doors before arming.')
    ids = [s['id'] for s in coverage['active']]
    plan = arm_coverage.prepare(LOCAL_SESSION, SITE, ROOT, ids, mode=action)
    if plan['baseline'] != saved['baseline']:
        raise ValueError('Protect settings changed. Review the doors again before arming.')
    # Verify the latest device readings too; the observer cannot poll while this
    # command holds LOCK. A door opened during review needs a fresh review.
    latest = request_unifi('GET', 'sensors')
    if not isinstance(latest, list):
        raise ValueError('Could not verify the latest door readings. No Arm request was sent.')
    rows = selected_sensors(SITE, latest, allow_missing=True)
    latest_observation = dict(observed, doors={s['name']: {
        'connected': s.get('state') == 'CONNECTED',
        'position': 'open' if s.get('isOpened') is True else 'closed' if s.get('isOpened') is False else 'unknown'} for s in rows})
    latest_coverage = door_policy.assess(SITE, latest_observation, door_policy.read(ROOT, SITE))
    if door_policy.signature(latest_coverage) != door_policy.signature(saved['coverage']):
        raise ValueError('A door or bypass issue changed. Review the doors again before arming.')
    current = {s.get('id'): s for s in latest}
    if any(current.get(key, {}).get('state') != 'CONNECTED' or current[key].get('isOpened') is not False for key in ids):
        raise ValueError('An alarm door opened or disconnected. Review the doors again.')
    record = arm_coverage.apply(LOCAL_SESSION, SITE, ROOT, ids, coverage['bypassed'], plan, mode=action)
    with ENTRY_LOCK:
        ENTRY.active_names = {s['name'] for s in coverage['active']}
        ENTRY.bypassed = coverage['bypassed']
        # Rule read-back and a fresh disarmed read succeeded, and each active
        # sensor already has a real signal check. No bypass gets a fake check.
        ENTRY.enabled = True
    if data.get('muteBypass') is True and data.get('confirmBypass') is True:
        door_policy.mute(ROOT, SITE, coverage['bypassed'])
        coverage['bypassed'] = door_policy.assess(SITE, observed, door_policy.read(ROOT, SITE))['bypassed']
        record['bypassed'] = coverage['bypassed']
        with ENTRY_LOCK:
            ENTRY.bypassed = coverage['bypassed']
    if coverage['bypassed']:
        account_store().record(actor, 'doors_bypassed', '; '.join(s['name'] + ': ' + s['reason'] for s in coverage['bypassed']))
    return record


def duress_flag(data):
    """True when this request's unlock grant was earned with a duress PIN."""
    token = data.get('unlock')
    grant = UNLOCKS.get(token) if isinstance(token, str) else None
    user = grant.get('user') if isinstance(grant, dict) else None
    return bool(isinstance(user, dict) and user.get('duress'))


def fire_best_effort_webhook(target):
    """Fire an installer webhook without ever raising.

    A missing or failing target is silent: the panel must never reveal
    whether a silent alert is configured.
    """
    if not target:
        return
    try:
        request_unifi('POST', 'alarm-manager/webhook/' + target)
    except Exception:
        pass


def fire_panic_webhook():
    """Best-effort silent panic alert after a duress-PIN disarm.

    Never raises and never writes to the visible activity log: anyone watching
    the panel must see an ordinary disarm. The alert goes to the provisioned
    'panic' Protect webhook (a push notification); when none is configured
    there is nothing further to do.
    """
    fire_best_effort_webhook(WEBHOOKS.get('panic'))


def webhook_command(action, data):
    if action not in WEBHOOKS:
        raise ValueError('An installer must configure this site’s alarm rules first.')
    if data.get('confirm') is not True:
        raise ValueError('Confirm the live command first.')
    if action in ARM_ACTIONS and ENTRY_RECOVERY is not None and ENTRY_RECOVERY.pending:
        raise ValueError('Finishing restart checks. Wait for Protect verification before arming.')
    global EXIT_UNTIL
    duress = duress_flag(data)
    actor = unlocked_user(data.get('unlock'), consume=True)
    arm_action = action in ARM_ACTIONS
    if arm_action and EXIT_UNTIL > time.monotonic():
        raise ValueError('An arm request is already counting down. Check Protect.')
    coverage_record = None
    if arm_action and SITE.get('version') == 2:
        coverage_record = commit_arm(data, actor, action)
    elif arm_action:
        with ENTRY_LOCK:
            before = ENTRY.snapshot()
        if not before['fresh'] or before['state'] != 'disarmed':
            raise ValueError('Protect must confirm disarmed before a new Arm request. Check its current state.')
        if not before['liveDelayEnabled'] or set(before['doors']) != ENTRY_DOORS:
            raise ValueError('Finish this site’s entry-delay setup before arming from this keypad.')
        if any(not door['connected'] or door['position'] != 'closed' for door in before['doors'].values()):
            raise ValueError('All configured doors must be connected and closed before arming.')
    started = time.monotonic()
    event = account_store().start_command(actor, action)
    if arm_action:
        EXIT_UNTIL = started + 60
    try:
        if coverage_record:
            # Persist before sending, including an uncertain response/restart.
            arm_coverage.mark_sent(ROOT, coverage_record)
        if arm_action:
            with ENTRY_LOCK:
                ENTRY.checkpoint()
        request_unifi('POST', 'alarm-manager/webhook/' + WEBHOOKS[action])
    except Exception:
        account_store().command_result(event, 'unverified', 'The request may not have completed. Check Protect.')
        raise
    finally:
        # A duress PIN disarms exactly like the normal PIN; the silent alert
        # fires alongside it, and the activity log shows an ordinary disarm.
        if duress and action == 'disarm':
            fire_panic_webhook()
    account_store().command_result(event, 'accepted', 'Request accepted; waiting for Protect to confirm.')
    EXIT_UNTIL = started + 60 if arm_action else 0
    label = {'arm_stay': 'Arm stay', 'arm_away': 'Arm away'}.get(action, action.capitalize())
    return {'exitSeconds': max(0, EXIT_UNTIL - time.monotonic()), 'state': 'Unverified (Global mode)', 'canControl': False,
            'webhookReady': True, 'pinSet': True, 'connected': True,
            'message': label + ' webhook accepted. Actual alarm state is UNVERIFIED. Check Protect; this does not confirm arming or disarming.'}

ENTRY_DOORS = set(SITE['doors'])
ENTRY = EntryObserver()
ENTRY_LOCK = threading.Lock()
RECOVERY_MESSAGE = ''
ENTRY_RECOVERY = None
PERSIST_ENTRY_RUNTIME = False


def protection_path():
    return PIN_FILE.parent / '.protection-state.json'


def enable_saved_protection():
    """Caller holds LOCK and ENTRY_LOCK and has verified the current rules."""
    if not protection_state.checked(SITE, ENTRY.snapshot()):
        raise ValueError('While disarmed, open and close each selected door and leave it closed before enabling protection.')
    previous = ENTRY.enabled
    ENTRY.enable()
    try:
        protection_state.save(protection_path(), SITE, ENTRY.snapshot())
    except (OSError, ValueError):
        ENTRY.enabled = previous
        raise ValueError('Could not save protection. Check storage and try again.')
    return ENTRY.snapshot()


def restore_protection_once():
    global RECOVERY_MESSAGE
    if restore_entry_runtime_once():
        return
    with LOCK:
        with ENTRY_LOCK:
            protection_state.save_progress(protection_path(), SITE, ENTRY.snapshot())
            protection_state.restore_progress(protection_path(), SITE, ENTRY)
            state = ENTRY.snapshot()
        runtime = arm_coverage.saved(ROOT, SITE)
        if runtime and state['fresh'] and CONFIG and LOCAL_SESSION.cookie:
            if state['state'] == 'disarmed' and EXIT_UNTIL <= time.monotonic():
                arm_coverage.restore_scope(LOCAL_SESSION, SITE, ROOT)
                with ENTRY_LOCK:
                    ENTRY.active_names = None
                    ENTRY.bypassed = []
            elif state['state'] in ('arming', 'armed', 'breached') and runtime['phase'] == 'sent' and not ENTRY.enabled:
                arm_coverage.prepare(LOCAL_SESSION, SITE, ROOT, runtime['activeIds'], require_disarmed=False,
                                       mode=runtime.get('mode', 'arm'))
                with ENTRY_LOCK:
                    ENTRY.active_names = {s['name'] for s in SITE['sensors'] if s['id'] in runtime['activeIds']}
                    ENTRY.bypassed = runtime.get('bypassed', [])
                    ENTRY.enabled = True
                RECOVERY_MESSAGE = ''
                return
        if state['liveDelayEnabled']:
            RECOVERY_MESSAGE = ''
            return
        if not protection_state.matches(protection_path(), SITE):
            RECOVERY_MESSAGE = 'Complete the sensor check in Settings to enable protection.'
            return
        if not CONFIG or not LOCAL_SESSION.cookie:
            RECOVERY_MESSAGE = 'Saved protection will resume after UniFi reconnects and verifies the setup.'
            return
        if not state['fresh'] or state['state'] != 'disarmed':
            RECOVERY_MESSAGE = 'Saved protection needs a confirmed disarmed state before it can resume.'
            return
        site = json.loads(json.dumps(SITE))
        generation = LOCAL_SESSION.generation
        session = LocalSession()
        session.host, session.cookie = LOCAL_SESSION.host, LOCAL_SESSION.cookie
        RECOVERY_MESSAGE = 'Checking saved alarm rules before resuming protection…'
    # Rule verification may take several requests. The alarm lock stays free.
    try:
        verified = sensor_settings.verify_for_activation(session, ROOT)
        with LOCK:
            if (LOCAL_SESSION.generation != generation or not LOCAL_SESSION.cookie or
                    protection_state.fingerprint(SITE) != protection_state.fingerprint(site) or
                    verified != SITE or not protection_state.matches(protection_path(), SITE)):
                raise ValueError('The setup changed. Protection will be checked again.')
            with ENTRY_LOCK:
                if not ENTRY.enabled:
                    protection_state.restore(ENTRY, SITE)
                    account_store().record({'name': 'System'}, 'protection_restored', 'Saved sensor checks and Protect rules verified; one-minute entry delay restored.')
            RECOVERY_MESSAGE = ''
    except ValueError as error:
        RECOVERY_MESSAGE = str(error)


def recover_protection():
    global RECOVERY_MESSAGE
    while True:
        try:
            restore_protection_once()
        except Exception:
            RECOVERY_MESSAGE = 'Saved protection could not be verified. Check Sensors in Settings.'
        threading.Event().wait(5)


def restore_entry_runtime_once():
    """Reconcile persisted timing only after a fresh state and rule readback.

Slow configuration reads stay outside LOCK so PIN/disarm requests remain usable
while startup recovery is waiting for a console or checking alarm rules.
"""
    global RECOVERY_MESSAGE
    with LOCK:
        recovery = ENTRY_RECOVERY
        if recovery is None or not recovery.pending:
            return False
        with ENTRY_LOCK:
            state = ENTRY.snapshot()
        if not state['fresh'] or not recovery.profiles or not CONFIG or not LOCAL_SESSION.cookie:
            RECOVERY_MESSAGE = 'Reconnecting to Protect before restoring alarm timing…'
            return True
        if state['state'] == 'disarmed':
            with ENTRY_LOCK:
                profiles = recovery_profiles(recovery)
                recovery.restore(ENTRY, profiles)
                selected = configured_alarm_state(profiles, SITE)
                ENTRY.ingest(selected['state'], [], {}, selected['state_set_at'])
                ENTRY.checkpoint()
            RECOVERY_MESSAGE = ''
            return True
        if recovery.read_error or recovery.record is None:
            RECOVERY_MESSAGE = recovery.read_error or 'No saved alarm cycle is available. Confirm disarmed before resuming protection.'
            return True
        site = json.loads(json.dumps(SITE))
        runtime = arm_coverage.saved(ROOT, site)
        progress = protection_state.read(protection_path(), site)
        if runtime and runtime['phase'] == 'sent':
            ids, bypassed, mode = runtime['activeIds'], runtime.get('bypassed', []), runtime.get('mode', 'arm')
        elif protection_state.matches(protection_path(), site) or recovery.record['observer']['enabled']:
            ids, bypassed, mode = [s['id'] for s in site['sensors']], [], 'arm'
        else:
            RECOVERY_MESSAGE = 'Protection was not enabled before restart. Confirm disarmed to finish setup.'
            return True
        names = {s['name'] for s in site['sensors'] if s['id'] in ids}
        if not progress or not names <= set(progress['verifiedDoors']):
            RECOVERY_MESSAGE = 'Saved sensor checks could not be verified. Confirm disarmed to review protection.'
            return True
        generation = LOCAL_SESSION.generation
        session = LocalSession()
        session.host, session.cookie = LOCAL_SESSION.host, LOCAL_SESSION.cookie
        RECOVERY_MESSAGE = 'Checking saved alarm rules before restoring the countdown…'
    try:
        plan = arm_coverage.prepare(session, site, ROOT, ids, require_disarmed=False, mode=mode)
        if not onboarding.equivalent(plan['current'], plan['payload']):
            raise ValueError('Saved door coverage changed. Confirm disarmed to review protection.')
        with LOCK:
            if ENTRY_RECOVERY is not recovery or LOCAL_SESSION.generation != generation or SITE != site:
                raise ValueError('Connection or setup changed. Alarm recovery will be checked again.')
            with ENTRY_LOCK:
                if not ENTRY.snapshot()['fresh']:
                    raise ValueError('Waiting for a fresh Protect state before restoring alarm timing.')
                profiles = recovery_profiles(recovery)
                recovery.restore(ENTRY, profiles, {'names': names, 'bypassed': bypassed})
                selected = configured_alarm_state(profiles, SITE)
                ENTRY.ingest(selected['state'], [], {}, selected['state_set_at'])
                ENTRY.checkpoint()
            RECOVERY_MESSAGE = ''
            account_store().record({'name': 'System'}, 'protection_restored', 'Restart recovery verified against Protect; original alarm timing and delivery status restored.')
    except (ValueError, OSError) as error:
        RECOVERY_MESSAGE = str(error) if isinstance(error, ValueError) else 'Alarm recovery could not be saved. Check panel storage.'
    return True


def recovery_profiles(recovery):
    """Only marks obtained from the live poller, never persisted profile truth."""
    from datetime import datetime, timezone
    return [{'id': key, 'state': row['state'], 'state_set_at':
             datetime.fromtimestamp(row['changedMs']/1000, timezone.utc).isoformat() if row['changedMs'] is not None else None}
            for key, row in recovery.profiles.items()]

def send_delayed_alarm():
    # Do not let a local journal failure suppress the existing phone alert.
    try:
        siren_runtime.remember(ROOT, SITE)
    except (OSError, ValueError):
        pass
    return request_unifi('POST', 'alarm-manager/webhook/' + WEBHOOKS['entry_alert'])


def observe_sirens_once():
    global SIREN_MESSAGE
    with LOCK:
        with ENTRY_LOCK:
            observed = ENTRY.snapshot()
        site, config = SITE, dict(CONFIG)
    if not observed['fresh'] or observed['state'] != 'disarmed' or config.get('host') != site.get('consoleHost'):
        return
    # A slow/offline siren must never block the alarm observer or keypad.
    try:
        siren_runtime.stop_after_disarm(ROOT, site, 'disarmed', lambda method, path: request_unifi(method, path, config))
        warning = ''
    except Exception:
        warning = 'Disarmed; siren stop not confirmed. Check Protect.'
    with LOCK:
        if SITE == site:
            SIREN_MESSAGE = warning


def observe_sirens():
    while True:
        observe_sirens_once()
        threading.Event().wait(2)


def observe_entry_once():
    global STATUS_SENSORS
    with LOCK:
        try:
            # Keep the account alive even before any alarm doors are chosen.
            LOCAL_SESSION.refresh_if_due()
            if not ENTRY_DOORS:
                raise ValueError('An installer must configure this site’s doors and alarm rules.')
            if not CONFIG or not LOCAL_SESSION.cookie:
                raise ValueError('Sign in to observe real door events. Live entry delay is not enabled.')
            profiles = LOCAL_SESSION.profiles()['profiles']
            selected = configured_alarm_state(profiles, SITE)
            if ENTRY_RECOVERY is not None:
                ENTRY_RECOVERY.set_profiles(profiles)
            account_store().confirm_commands(selected['state'], selected['state_set_at'])
            sensors = request_unifi('GET', 'sensors')
            if not isinstance(sensors, list):
                raise ValueError('The console did not return sensor status.')
            doors = selected_sensors(SITE, sensors, allow_missing=True)
            if len(doors) != len(ENTRY_DOORS) or {x.get('name') for x in doors} != ENTRY_DOORS or any(not x.get('id') for x in doors):
                raise ValueError('Expected all configured entry sensors; coverage is unverified.')
            # Confirm disarm before a potentially unavailable event-history read.
            # It must cancel a pending timer even if that separate endpoint fails.
            with ENTRY_LOCK:
                ENTRY.ingest(selected['state'], [], {x['id']:x['name'] for x in doors}, selected['state_set_at'])
            start = ENTRY_RECOVERY.history_start(ENTRY) if ENTRY_RECOVERY is not None else max(ENTRY.started_ms, (time.time()-120)*1000)
            events = LOCAL_SESSION.door_events(start)
            modes = door_policy.read(ROOT, SITE)['modes']
            selected_ids = {s.get('id') for s in SITE.get('sensors', [])}
            STATUS_SENSORS = [{'id': s['id'], 'name': s.get('name') or 'Sensor',
                               'mode': modes.get(s['id'], {}).get('mode', 'status'),
                               'connected': s.get('state') == 'CONNECTED',
                               'position': 'open' if s.get('isOpened') is True else 'closed' if s.get('isOpened') is False else 'unknown'}
                              for s in sensors if isinstance(s.get('id'), str) and s['id'] not in selected_ids]
            STATUS_SENSORS += [{**s, 'id': key, 'connected': False, 'position': 'unknown'} for key, s in modes.items()
                               if key not in selected_ids and key not in {s['id'] for s in STATUS_SENSORS}]
            with ENTRY_LOCK:
                ENTRY.ingest(selected['state'], events, {x['id']:x['name'] for x in doors}, selected['state_set_at'])
                for door in doors:
                    ENTRY.sensor_update(door)
                if ENTRY_RECOVERY is None or not ENTRY_RECOVERY.pending:
                    ENTRY.checkpoint()
                    if ENTRY_RECOVERY is not None:
                        ENTRY_RECOVERY.history_complete(ENTRY)
                    ENTRY.dispatch(send_delayed_alarm)
        except Exception as error:
            with ENTRY_LOCK:
                ENTRY.fail(str(error) if isinstance(error,ValueError) else 'Live observation failed. Check Protect directly.')
                if CONFIG and (ENTRY_RECOVERY is None or not ENTRY_RECOVERY.pending):
                    try:
                        ENTRY.dispatch(send_delayed_alarm)
                    except ValueError as persistence_error:
                        ENTRY.fail(str(persistence_error))


def observe_entry():
    while True:
        observe_entry_once()
        threading.Event().wait(1)

def apply_saved_site():
    """Load a verified saved setup while holding LOCK; keep an unchanged live test."""
    global SITE, WEBHOOKS, ENTRY_DOORS, ENTRY, EXIT_UNTIL, ENTRY_RECOVERY
    _, saved = settings()
    equivalent = protection_state.fingerprint(saved) == protection_state.fingerprint(SITE)
    if not equivalent:
        with ENTRY_LOCK:
            protection_state.carry_progress(protection_path(), SITE, saved, ENTRY.snapshot())
    SITE, WEBHOOKS, ENTRY_DOORS = saved, saved['webhooks'], set(saved['doors'])
    if not equivalent:
        with ENTRY_LOCK:
            ENTRY = EntryObserver()
            protection_state.restore_progress(protection_path(), SITE, ENTRY)
            if PERSIST_ENTRY_RUNTIME or ENTRY_RECOVERY is not None:
                ENTRY_RECOVERY = new_entry_recovery()
        UNLOCKS.clear()
        EXIT_UNTIL = 0


LAB = EntryDelay()
LAB_LOCK = threading.Lock()

def run_lab_clock():
    while True:
        threading.Event().wait(0.1)
        with LAB_LOCK:
            LAB.tick()
STATES = {'disabled': 'Disarmed', 'arming': 'Arming', 'armed': 'Armed', 'breach': 'Alarm triggered'}

def validate_host(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError('Enter the console’s private IPv4 address, without https or a path.')
    if ip.version != 4 or not any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('Use a private local-network IPv4 address.')
    return str(ip)

def request_unifi(method, path, config=None):
    config = dict(CONFIG) if config is None else config
    if not config:
        raise ValueError('Connect to your console first.')
    pin = os.environ.get('UNIFI_CERT_SHA256', '').replace(':', '').lower()
    if pin and (len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin)):
        raise ValueError('Invalid configured certificate fingerprint.')
    context = ssl.create_default_context(cafile=os.environ.get('UNIFI_CA_FILE') or None)
    if pin:
        # Explicit trust in one exact certificate, checked before sending any key.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(config['host'], timeout=8, context=context)
    try:
        if pin:
            connection.connect()
            actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, pin):
                raise ValueError('Console certificate changed. Connection blocked before sending the API key.')
        connection.request(method, '/proxy/protect/integration/v1/' + path,
                           headers={'X-API-Key': config['key'], 'Accept': 'application/json'})
        response = connection.getresponse()
        body = response.read(2_000_000)
        if response.status >= 300:
            detail = ''
            try:
                failure = json.loads(body)
                if isinstance(failure, dict):
                    detail = str(failure.get('message') or failure.get('error') or '')[:300]
                    detail = detail.replace(config['key'], '[redacted]')
            except (ValueError, TypeError):
                pass
            messages = {401: 'API key rejected.', 403: 'Access denied or this alarm mode is unsupported.',
                        404: 'Endpoint unavailable. Check Protect version and Local Alarm Manager support.'}
            raise ValueError(messages.get(response.status, f'UniFi returned HTTP {response.status} for {path}.') + (' ' + detail if detail else ''))
        return json.loads(body) if body else None
    except ssl.SSLError:
        raise ValueError('Console certificate could not be verified. Configure UNIFI_CA_FILE with a trusted console certificate/CA and restart; verification has not been bypassed.') from None
    except (OSError, http.client.HTTPException):
        raise ValueError('Console connection failed or timed out. Check the local address and network. If a command was sent, its outcome is uncertain; check Protect before retrying.') from None
    except json.JSONDecodeError:
        raise ValueError('Console returned an unexpected response.') from None
    finally:
        connection.close()

def read_status():
    nvr = request_unifi('GET', 'nvrs')
    try:
        profiles = request_unifi('GET', 'arm-profiles')
    except ValueError as error:
        if 'global alarm manager' not in str(error).lower():
            raise
        return {'state': 'Unverified (Global mode)', 'profile': None,
                'canControl': False, 'connected': True, 'webhookReady': True, 'pinSet': bool(PIN), 'exitSeconds': max(0, EXIT_UNTIL - time.monotonic()),
                'panicConfigured': bool(WEBHOOKS.get('panic')),
                'armStayConfigured': bool(WEBHOOKS.get('arm_stay')),
                'armAwayConfigured': bool(WEBHOOKS.get('arm_away')),
                'message': 'Authenticated. Global state cannot be verified here. PIN-protected webhook commands are available for supervised testing; confirm their result in Protect.'}
    if not isinstance(nvr, dict) or not isinstance(profiles, list):
        raise ValueError('Unexpected API format. Controls remain disabled.')
    arm = nvr.get('armMode') or {}
    if not isinstance(arm, dict):
        arm = {}
    state = arm.get('status')
    # Unknown states are never interpreted as disarmed.
    active = next((p.get('name') for p in profiles if isinstance(p, dict) and p.get('id') == arm.get('armProfileId')), None)
    return {'state': STATES.get(state, 'Unknown'), 'rawState': state if state in STATES else None,
            'profile': active, 'profiles': [p.get('name', 'Unnamed') for p in profiles if isinstance(p, dict)],
            'canControl': state in STATES and bool(profiles), 'connected': True,
            'panicConfigured': bool(WEBHOOKS.get('panic')),
            'armStayConfigured': bool(WEBHOOKS.get('arm_stay')),
            'armAwayConfigured': bool(WEBHOOKS.get('arm_away'))}

def connect_api(host, key):
    host = validate_host(str(host or '').strip())
    if not isinstance(key, str):
        raise ValueError('Enter a valid API key.')
    config = credential_store.validate({'host': host, 'key': key.strip()})
    previous = dict(CONFIG)
    CONFIG.clear()
    CONFIG.update(config)
    try:
        result = read_status()
        credential_store.save(CREDENTIAL_FILE, CONFIG)
        return result
    except Exception:
        CONFIG.clear()
        CONFIG.update(previous)
        raise


def phone_connect(host, key):
    with LOCK:
        if CONFIG and CONFIG['host'] != host:
            raise ValueError('Console changed. Create a new pairing link.')
        return connect_api(host, key)

PHONE_PAIR = phone_pairing.Pairing(phone_connect,
    origin=os.environ.get('KEYPAD_PHONE_ORIGIN', ''), bind=os.environ.get('KEYPAD_PHONE_BIND', ''),
    cert=os.environ.get('KEYPAD_PHONE_CERT', ''), key=os.environ.get('KEYPAD_PHONE_TLS_KEY', ''))

def setup_session_info():
    pin_set = SETTINGS_ACCESS.saved_pin() is not None
    return {'pinSet': pin_set, 'installerAvailable': True,
            **account_store().panel_settings(),
            'setupRequired': not SITE.get('doors') or not pin_set,
            'computerSetupAvailable': BROWSER_SETUP.available(),
            'networkSetup': BROWSER_SETUP.connection_info(),
            'apiConfigured': bool(CONFIG), 'consoleHost': CONFIG.get('host') or SITE.get('consoleHost', ''),
            'site': {'name': SITE['name'], 'doors': SITE['doors']}}

class Handler(BaseHTTPRequestHandler):
    @property
    def access(self):
        return SETTINGS_ACCESS

    def log_message(self, *args):
        pass  # Never log setup requests or credentials.

    def reply(self, code, data, content_type='application/json'):
        body = json.dumps(data).encode() if content_type == 'application/json' else data
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if getattr(self, 'settings_cookie', None):
            self.send_header('Set-Cookie', self.settings_cookie)
        self.send_header('X-Content-Type-Options', 'nosniff')
        ancestor = KIOSK_ORIGIN or "'none'"
        self.send_header('Content-Security-Policy', f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors {ancestor}; form-action 'self'")
        if self.path == '/health' and KIOSK_ORIGIN:
            self.send_header('Access-Control-Allow-Origin', KIOSK_ORIGIN)
        self.end_headers()
        self.wfile.write(body)

    def trusted_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{PORT}'

    def trusted_post(self):
        return self.trusted_host() and self.headers.get('Origin') == ORIGIN and self.headers.get('X-Keypad-Token') == TOKEN

    def do_GET(self):
        if not self.trusted_host():
            return self.reply(403, {'error': 'Invalid host.'})
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
        # The durable site/PIN state selects the landing screen. Missing network
        # access or an expired console session must not restart onboarding.
        if self.path == '/' and (not SITE.get('doors') or SETTINGS_ACCESS.saved_pin() is None):
            files['/'] = ('setup_lab.html', 'text/html; charset=utf-8')
        files['/phone-pair.js'] = ('phone_pair_ui.js', 'text/javascript')
        files['/browser-pair.js'] = ('browser_pair_ui.js', 'text/javascript')
        files['/web-setup.css'] = ('web_setup.css', 'text/css')
        files['/management.js'] = ('management.js', 'text/javascript')
        files['/astra.css'] = ('astra.css', 'text/css')
        files['/audio.js'] = ('audio.js', 'text/javascript')
        files['/lighting.js'] = ('lighting.js', 'text/javascript')
        files['/lighting.css'] = ('lighting.css', 'text/css')
        files.update({'/onboarding': ('setup_lab.html', 'text/html; charset=utf-8'), '/setup.js': ('setup_lab.js', 'text/javascript'), '/setup.css': ('setup_lab.css', 'text/css')})
        files.update({'/lab': ('lab.html', 'text/html; charset=utf-8'), '/lab.js': ('lab.js', 'text/javascript')})
        if self.path in files:
            name, kind = files[self.path]
            return self.reply(200, Path(__file__).with_name(name).read_bytes(), kind)
        if self.path == '/session':
            return self.reply(200, {'token': TOKEN, **setup_session_info()})
        if self.path == '/health':
            # Process liveness is not evidence of functioning alarm protection.
            # Restart recovery is still incomplete, so remote activation is blocked.
            return self.reply(200, {'service': 'loxicom-keypad', 'version': VERSION,
                                    'instance': INSTANCE, 'processHealthy': True,
                                    'benchUpdateAllowed': bool(KIOSK_ORIGIN) and not (SITE['doors'] or PIN or CONFIG or LOCAL_SESSION.cookie),
                                    'automaticUpdatesReady': False})
        self.reply(404, {'error': 'Not found.'})

    def do_POST(self):
        self.settings_cookie = None
        if not self.trusted_post():
            return self.reply(403, {'error': 'Reload the local app to continue.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 8192:
                raise ValueError('Request too large.')
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('Invalid request.')
            if self.path.startswith('/browser-setup/'):
                # Remote setup acquires its own lock before the console lock.
                # Never enter it while holding LOCK on the local HTTP thread.
                self.access.require(self.headers)
                if self.path == '/browser-setup/start':
                    result = BROWSER_SETUP.begin()
                elif self.path == '/browser-setup/status':
                    result = BROWSER_SETUP.status()
                elif self.path == '/browser-setup/cancel':
                    result = BROWSER_SETUP.end()
                else:
                    return self.reply(404, {})
                return self.reply(200, result)
            if self.path.startswith('/phone-pair/'):
                # Do not hold LOCK while entering the pairing manager: phone
                # submission acquires the pairing lock before the console lock.
                self.access.require(self.headers)
                if self.path == '/phone-pair/start':
                    host = validate_host(str(data.get('host', '')).strip())
                    if CONFIG and CONFIG['host'] != host:
                        raise ValueError('Use the connected console address.')
                    result = PHONE_PAIR.begin(host)
                elif self.path == '/phone-pair/status':
                    result = PHONE_PAIR.status()
                elif self.path == '/phone-pair/cancel':
                    result = PHONE_PAIR.cancel()
                else:
                    return self.reply(404, {})
                return self.reply(200, result)
            if self.path == '/settings/lock' and not getattr(self, 'remote_setup', False):
                PHONE_PAIR.cancel()
            if self.path == '/entry/status':
                return self.reply(200, coverage_status())
            if self.path == '/hub/capabilities':
                return self.reply(200, hub_capabilities(data))
            if self.path == '/hub/network':
                return self.reply(200, hub_network(data))
            if self.path == '/hub/audio':
                return self.reply(200, hub_audio(data))
            if self.path == '/hub/audio/artwork':
                return self.reply(200, hub_audio(data, artwork=True))
            if self.path == '/hub/audio/control':
                return self.reply(200, hub_audio_control(data))
            if self.path == '/hub/audio/group':
                return self.reply(200, hub_audio_control(data, grouping=True))
            if self.path == '/hub/lighting':
                return self.reply(200, hub_lighting(data))
            if self.path in ('/hub/lighting/control', '/hub/lighting/discover', '/hub/lighting/prepare',
                             '/hub/lighting/pair', '/hub/lighting/disconnect'):
                return self.reply(200, hub_lighting(data, self.path.rsplit('/', 1)[-1]))
            if self.path == '/hub/wifi/reveal':
                return self.reply(200, reveal_wifi_passwords(data))
            if self.path == '/hub/network/speedtest/status':
                return self.reply(200, gateway_speedtest(data))
            if self.path == '/hub/network/speedtest/start':
                return self.reply(200, gateway_speedtest(data, start=True))
            if self.path == '/hub/network/review':
                return self.reply(200, review_network_action(data))
            if self.path == '/hub/network/apply':
                return self.reply(200, commit_network_action(data))
            if self.path == '/lab/event':
                with LAB_LOCK:
                    result = LAB.update(data['event']) if data.get('event') else LAB.snapshot()
                return self.reply(200, result)
            with LOCK:
                if self.path == '/settings/status':
                    return self.reply(200, self.access.status(self.headers))
                if self.path == '/settings/unlock':
                    result, self.settings_cookie = self.access.unlock(data.get('pin'), self.headers)
                    return self.reply(200, result)
                if self.path == '/settings/lock':
                    self.settings_cookie = self.access.lock(self.headers)
                    return self.reply(200, {'settingsUnlocked': False})
                if protected_route(self.path):
                    actor = self.access.require(self.headers)
                if self.path.startswith('/installer/'):
                    if self.path == '/installer/login' and CONFIG and validate_host(str(data.get('host', '')).strip()) != CONFIG['host']:
                        raise ValueError('Use the same console address as the API connection.')
                    result = onboarding.handle(self.path, data, LOCAL_SESSION, ROOT, INSTALLER_STATE)
                    if not PIN and result.get('pinSet'):
                        load_pin()
                    if self.path == '/installer/pin/create':
                        access, self.settings_cookie = self.access.unlock(data.get('pin'), self.headers)
                        result.update(access)
                    if self.path == '/installer/apply' and result.get('saved'):
                        apply_saved_site()
                elif self.path == '/settings/summary':
                    with ENTRY_LOCK:
                        observation = ENTRY.snapshot()
                    result = {'user': actor, 'users': sum(user['enabled'] for user in account_store().users()), 'name': SITE['name'],
                              'managed': SITE.get('version') == 2, 'doors': SITE['doors'],
                              'apiConnected': bool(CONFIG), 'localConnected': bool(LOCAL_SESSION.cookie),
                              'consoleHost': CONFIG.get('host') or SITE.get('consoleHost', ''),
                              **account_store().panel_settings(),
                              'observation': observation}
                elif self.path == '/settings/panel/save':
                    result = account_store().save_panel_settings(actor, data)
                    # Shortening the setting also limits any existing grant.
                    limit = time.monotonic() + result['autoLockSeconds']
                    for grant in UNLOCKS.values():
                        grant['expires'] = min(grant['expires'], limit)
                elif self.path == '/settings/users':
                    account_store().current(actor, admin=True)
                    result = {'users': account_store().users()}
                elif self.path == '/settings/users/save':
                    result = account_store().save_user(actor, data)
                elif self.path == '/settings/activity':
                    account_store().current(actor, admin=True)
                    result = account_store().history(data.get('before'))
                elif self.path == '/settings/sirens':
                    result = alarm_outputs.discover(LOCAL_SESSION, ROOT)
                elif self.path == '/settings/sirens/review':
                    result = alarm_outputs.review(LOCAL_SESSION, data, ROOT, INSTALLER_STATE)
                elif self.path == '/settings/sirens/apply':
                    before = sensor_settings.saved_site(ROOT)
                    result = onboarding.apply(LOCAL_SESSION, data, ROOT, INSTALLER_STATE)
                    apply_saved_site()
                    account_store().record(actor, 'sirens_updated', 'Alarm sirens: ' + ', '.join(d['name'] for d in (alarm_outputs.siren_output(SITE) or {}).get('devices', [])))
                elif self.path == '/settings/sensors':
                    result = sensor_settings.discover(LOCAL_SESSION, ROOT)
                    with ENTRY_LOCK:
                        result['observation'] = ENTRY.snapshot()
                elif self.path == '/settings/sensors/review':
                    result = sensor_settings.review(LOCAL_SESSION, data, ROOT, INSTALLER_STATE)
                elif self.path == '/settings/sensors/apply':
                    before = sensor_settings.saved_site(ROOT)
                    result = sensor_settings.apply(LOCAL_SESSION, data, ROOT, INSTALLER_STATE)
                    apply_saved_site()
                    if SITE != before:
                        account_store().record(actor, 'sensors_updated', 'Protected sensors: ' + ', '.join(SITE['doors']))
                elif self.path == '/settings/sensors/reminders':
                    door_policy.clear_mutes(ROOT, SITE)
                    account_store().record(actor, 'bypass_reminders_reset', 'Bypass reminders turned back on.')
                    result = {'message': 'Bypass reminders are back on.'}
                elif self.path == '/settings/sensors/enable':
                    verified_site = sensor_settings.verify_for_activation(LOCAL_SESSION, ROOT)
                    # The separate installer can also save this file. A replaced
                    # sensor must never inherit the previous device's test.
                    apply_saved_site()
                    if SITE != verified_site:
                        raise ValueError('Sensor setup changed during verification. Review it again.')
                    with ENTRY_LOCK:
                        if not ENTRY_DOORS or not ENTRY_DOORS <= ENTRY.verified_doors or set(ENTRY.doors) != ENTRY_DOORS or any(not x['connected'] or x['position'] != 'closed' for x in ENTRY.doors.values()):
                            raise ValueError('While disarmed, open and close each selected door to verify it before enabling protection.')
                        result = enable_saved_protection()
                    account_store().record(actor, 'protection_enabled', 'One-minute entry delay enabled for: ' + ', '.join(SITE['doors']))
                elif self.path == '/entry/enable':
                    if data.get('immediateRuleAdjusted') is not True:
                        raise ValueError('The immediate notifications for all configured doors must be adjusted before activation.')
                    check_pin(data.get('pin', ''))
                    if SITE.get('version') == 2:
                        verified_site = sensor_settings.verify_for_activation(LOCAL_SESSION, ROOT)
                        apply_saved_site()
                        if SITE != verified_site:
                            raise ValueError('Sensor setup changed. Review it again.')
                    with ENTRY_LOCK:
                        if set(ENTRY.doors) != ENTRY_DOORS or any(not x['connected'] or x['position'] == 'unknown' for x in ENTRY.doors.values()):
                            raise ValueError('All configured doors must be connected and report a known position before enabling the delay.')
                        if SITE.get('version') == 2:
                            result = enable_saved_protection()
                        else:
                            ENTRY.enable()
                            result = ENTRY.snapshot()
                elif self.path == '/local/login':
                    host = validate_host(str(data.get('host', '')).strip())
                    if CONFIG and host != CONFIG['host']:
                        raise ValueError('Use the same console address as the API connection.')
                    result = LOCAL_SESSION.login(host, data.get('username'), data.get('password'), data.get('verificationCode'))
                elif self.path == '/local/status':
                    result = LOCAL_SESSION.profiles()
                elif self.path == '/local/logout':
                    LOCAL_SESSION.clear()
                    result = {'localConnected': False, 'message': 'Local session removed from this server.'}
                elif self.path == '/unlock':
                    user = check_pin(data.get('pin', ''))
                    preferences = account_store().panel_settings()
                    seconds = preferences['autoLockSeconds']
                    UNLOCKS.clear()
                    unlock = secrets.token_urlsafe(32)
                    UNLOCKS[unlock] = {'expires': time.monotonic() + seconds, 'user': user}
                    result = {'unlock': unlock, 'expiresIn': seconds, **preferences}
                elif self.path == '/hub/activity':
                    result = hub_activity(data)
                elif self.path == '/lock':
                    unlock = data.get('unlock')
                    if isinstance(unlock, str):
                        UNLOCKS.pop(unlock, None)
                    result = {'locked': True}
                elif self.path == '/pin/setup':
                    set_pin(data.get('pin'))
                    result = {'pinSet': True}
                elif self.path == '/arm/review':
                    result = review_arm(data)
                elif self.path == '/bypass/mute':
                    result = mute_bypass_reminders(data)
                elif self.path in ('/webhook/arm', '/webhook/disarm', '/webhook/arm-stay', '/webhook/arm-away'):
                    result = webhook_command(self.path.rsplit('/', 1)[1].replace('-', '_'), data)
                elif self.path == '/connect':
                    if getattr(self, 'remote_setup', False):
                        expected = SITE.get('consoleHost') or CONFIG.get('host')
                        if expected and validate_host(str(data.get('host', '')).strip()) != expected:
                            raise ValueError('Use this panel’s saved console address. Set up a different home on its own panel.')
                    result = connect_api(data.get('host'), data.get('key'))
                elif self.path == '/status':
                    result = read_status()
                elif self.path == '/diagnostics':
                    nvr = request_unifi('GET', 'nvrs')
                    sensors = request_unifi('GET', 'sensors')
                    if not isinstance(nvr, dict) or not isinstance(sensors, list):
                        raise ValueError('Unexpected diagnostic response.')
                    arm = nvr.get('armMode')
                    result = {'reportedArmMode': arm if isinstance(arm, dict) else None,
                              'warning': 'Reported alarm data has not been validated against Global Alarm Manager. Do not use it to cancel an entry timer yet.',
                              'sensors': [{k: sensor.get(k) for k in ('name', 'state', 'isOpened', 'openStatusChangedAt', 'batteryStatus')} for sensor in sensors if isinstance(sensor, dict)]}
                elif self.path == '/disconnect':
                    credential_store.forget(CREDENTIAL_FILE)
                    CONFIG.clear()
                    UNLOCKS.clear()
                    result = {'disconnected': True}
                elif self.path in ('/arm', '/disarm'):
                    raise ValueError('Use the keypad controls. Disarm requires a personal PIN and is recorded in Activity.')
                else:
                    return self.reply(404, {'error': 'Not found.'})
            self.reply(200, result)
        except onboarding.ReviewRequired as error:
            self.reply(400, {'error': str(error), 'reviewRequired': True})
        except SettingsLocked as error:
            self.reply(403, {'error': str(error), 'settingsLocked': True})
        except (ValueError, TypeError):
            import sys
            error = sys.exc_info()[1]
            self.reply(400, {'error': str(error), 'state': 'Unknown', 'canControl': False})
        except Exception:
            self.reply(500, {'error': 'Unexpected response. Controls disabled; check Protect directly.', 'state': 'Unknown', 'canControl': False})

_phone_host = urlsplit(os.environ.get('KEYPAD_PHONE_ORIGIN', '')).hostname
BROWSER_SETUP = browser_setup.BrowserSetup(Handler,
    lambda: SettingsAccess(lambda: PIN_FILE, 'browser', verifier=check_pin,
                           lifetime=browser_setup.SESSION_SECONDS, secure=True), setup_session_info,
    origin=os.environ.get('KEYPAD_WEB_ORIGIN', f'https://{_phone_host}:8767' if _phone_host else ''),
    bind=os.environ.get('KEYPAD_WEB_BIND', os.environ.get('KEYPAD_PHONE_BIND', '')),
    cert=os.environ.get('KEYPAD_WEB_CERT', os.environ.get('KEYPAD_PHONE_CERT', '')),
    key=os.environ.get('KEYPAD_WEB_TLS_KEY', os.environ.get('KEYPAD_PHONE_TLS_KEY', '')),
    landing_port=int(os.environ.get('KEYPAD_WEB_LANDING_PORT', '0')))

def monitor_power():
    """Background mains/battery watch. Opt-in via KEYPAD_POWER_MONITOR=1.

    Outages, restorations, and low-battery states are written to the activity
    log; an installer-configured 'power' Protect webhook fires best-effort on
    outage. Network I/O never holds the alarm lock: during an outage the
    network is the least reliable thing in the room.
    """
    if os.environ.get('KEYPAD_POWER_MONITOR') != '1':
        return
    if os.environ.get('KEYPAD_POWER_PROVIDER', 'file') == 'nut':
        provider = power_monitor.NutPowerProvider(os.environ.get('KEYPAD_NUT_UPS', 'ups@localhost'))
    else:
        provider = power_monitor.FilePowerProvider(
            os.environ.get('KEYPAD_POWER_STATE_FILE', str(PIN_FILE.parent / '.power-state.json')))
    power_monitor.PowerMonitor(provider, power_event).run_forever()


def power_event(event, status):
    """Record a power-monitor event; fire the installer power webhook best-effort.

    Module-level so the server integration is testable. Network I/O never
    holds the alarm lock: during an outage the network is the least reliable
    thing in the room.
    """
    system = {'id': 'system', 'name': 'System'}
    webhook_target = None
    try:
        with LOCK:
            if event == 'outage':
                account_store().record(system, 'power_outage',
                                       'Mains power lost; panel is on battery (%s).' % status.get('source'))
                webhook_target = WEBHOOKS.get('power')
            elif event == 'restored':
                account_store().record(system, 'power_restored', 'Mains power restored.')
            elif event == 'low_battery':
                account_store().record(system, 'power_low_battery',
                                       'Battery low while on backup power. Restore mains power.')
    except Exception:
        pass
    fire_best_effort_webhook(webhook_target)
    if event == 'low_battery' and os.environ.get('KEYPAD_UPS_SHUTDOWN') == '1':
        # Last resort, strictly opt-in: shut down cleanly before the
        # battery dies mid-write and corrupts the state files.
        os.system('/sbin/shutdown -h now')


def fleet_heartbeat_loop():
    """Optional heartbeat for a future Loxicom fleet console.

    Disabled unless KEYPAD_FLEET_URL and KEYPAD_FLEET_SECRET are both set.
    Panel side only; the cloud console itself is a separate service.
    """
    url = os.environ.get('KEYPAD_FLEET_URL', '')
    secret = os.environ.get('KEYPAD_FLEET_SECRET', '')
    if not url or not secret:
        return
    try:
        interval = max(60, int(os.environ.get('KEYPAD_FLEET_INTERVAL', '300')))
    except ValueError:
        interval = 300
    while True:
        try:
            with LOCK:
                with ENTRY_LOCK:
                    state = ENTRY.snapshot().get('state', 'unknown')
            fleet_heartbeat.send(url, secret, panel_id=SITE.get('panel_id') or INSTANCE,
                                 version=VERSION, alarm_state=state)
        except Exception:
            pass
        threading.Event().wait(interval)


def _graceful_shutdown(signum, frame):
    try:
        account_store().record({'id': 'system', 'name': 'System'},
                               'controller_shutdown', 'Panel is shutting down.')
    except Exception:
        pass
    raise SystemExit(0)


def new_entry_recovery():
    return entry_recovery.EntryRecovery(PIN_FILE.parent / '.entry-state.json', SITE,
        clock_ready=lambda: os.environ.get('KEYPAD_APPLIANCE') != '1' or
                            Path('/run/systemd/timesync/synchronized').exists())


if __name__ == '__main__':
    PERSIST_ENTRY_RUNTIME = True
    CONFIG.update(credential_store.load(CREDENTIAL_FILE))
    LOCAL_SESSION.restore(CONFIG.get('host') or SITE.get('consoleHost'))
    load_pin()
    account_store().recover()
    if SITE.get('version') == 2:
        ENTRY_RECOVERY = new_entry_recovery()
        protection_state.restore_progress(protection_path(), SITE, ENTRY)
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    threading.Thread(target=run_lab_clock, daemon=True).start()
    threading.Thread(target=observe_entry, daemon=True).start()
    threading.Thread(target=observe_sirens, daemon=True).start()
    threading.Thread(target=recover_protection, daemon=True).start()
    threading.Thread(target=monitor_power, daemon=True).start()
    threading.Thread(target=fleet_heartbeat_loop, daemon=True).start()
    threading.Thread(target=BROWSER_SETUP.watch_landing, daemon=True).start()
    print(f'Local keypad: {ORIGIN}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()

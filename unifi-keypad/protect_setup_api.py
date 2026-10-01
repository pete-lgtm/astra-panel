"""Version-sensitive Protect installer API shapes verified against the console."""
import base64
import json
import re

RULES = '/api/v2/alarms/protect'
MANIFEST = RULES + '/manifest'
PROFILES = '/api/v2/alarms/profiles'
SENSORS = '/proxy/protect/api/sensors'


def csrf_from_cookie(cookie):
    """Use the claim issued by the pinned console, never a browser cookie."""
    try:
        name, token = cookie.split('=', 1)
        if name not in ('TOKEN', 'UOS_TOKEN'):
            raise ValueError()
        part = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        value = claims['csrfToken']
        if not isinstance(value, str) or not re.fullmatch(r'[!-~]{1,8192}', value):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ValueError('This console session does not provide a supported request-verification token. No rule was changed.') from None


def entries(rule, kind):
    return [{'id': item.get('id'), 'data': item.get('data')}
            for category in rule.get(kind + '_categories', [])
            for item in category.get('triggers' if kind == 'trigger' else 'actions', [])]

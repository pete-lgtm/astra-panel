"""Synthetic site fixtures; never read an installation's saved configuration."""


def legacy_site():
    return {
        'name': 'Example Home',
        'profile': 'Arm',
        'doors': ['Front Door', 'Back Door', 'Garage Door'],
        'webhooks': {
            'arm': '00000000-0000-4000-8000-000000000001',
            'disarm': '00000000-0000-4000-8000-000000000002',
            'entry_alert': '00000000-0000-4000-8000-000000000003',
        },
    }

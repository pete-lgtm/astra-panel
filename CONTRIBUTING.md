# Contributing to Astra

Issues and pull requests are welcome. Describe the behavior you want to change,
the hardware/software versions involved, and how you tested it. Share synthetic
examples instead of your home's configuration or device credentials.

## Run the tests

The panel uses Python's standard library. Install Python 3.9 or newer, Node.js
and OpenSSL, then run from the repository root:

```sh
cd unifi-keypad
python3 -m unittest discover -v
for test in test_*.cjs; do node "$test" || exit 1; done
```

The portal needs Python 3.10 or newer and its pinned dependencies:

```sh
cd astra-portal
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m unittest discover -s tests -v
node --check static/portal.js
```

Run these commands in separate terminals from the repository root, or return
to the root before changing between the two directories. CI runs both suites.
Tests use temporary state and mocked equipment; do not replace fixtures with
live account details.

## Development boundaries

- Keep the alarm controller responsive while other integrations are slow or offline.
- Preserve PIN, role, pairing, certificate and command-confirmation checks.
- Treat an unknown equipment or alarm state as unknown. Request acceptance is
  not proof that an action completed.
- Keep private state outside application releases. Never commit a signing key,
  API key, session cookie, PIN database, Wi-Fi credential or enrollment secret.
- Verify touch layouts at **731 × 411 CSS pixels**, the current landscape panel
  viewport at 175% Chromium zoom, as well as desktop computer setup.
- Use a bench system for integration changes. Do not point a second alarm
  controller at an already active site.

Contributions are accepted under the repository's MIT license. Preserve the
license notices of any third-party code you add.

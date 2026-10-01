# Computer-assisted panel setup

A PC or Mac on the same network can complete onboarding through temporarily
paired HTTPS. The main alarm/control UI stays on loopback port 8765.

## Configure the transport

Provision a dedicated hostname resolving to the panel's private LAN address,
plus a certificate trusted by the setup computer. Configure your own DNS and
certificate renewal; these are not provisioned by the application. Do not
forward router ports to the panel or reuse a private certificate key between
customer devices.

Set these in the panel service environment, using your own values:

```text
KEYPAD_WEB_ORIGIN=https://panel.example.com:8767
KEYPAD_WEB_BIND=192.168.1.50
KEYPAD_WEB_CERT=/absolute/path/to/fullchain.pem
KEYPAD_WEB_TLS_KEY=/absolute/path/to/private-key.pem
KEYPAD_WEB_LANDING_PORT=8080
```

The origin has no path or trailing slash. Use an explicit private/loopback IPv4
bind and a key readable only by the appropriate service account. The optional
HTTP landing page provides directions to HTTPS; credentials go only through
the secure setup page. A listener on port 8767 opens when pairing starts.

Restart the panel service after changing its environment. Computers on isolated
guest networks may not reach the panel. Do not bypass browser certificate errors.

## Pair and configure

1. On a fresh panel choose **Set up from a computer**. On a configured panel use
   **Settings → Connection & setup → Set up from a computer**.
2. Open the displayed address on the PC or Mac, continue to HTTPS and enter the
   panel's eight-digit code within ten minutes. The panel retains the code.
3. A configured panel also requires its administrator PIN. Pairing does not
   bypass that check.
4. Enter the Protect API key and sign in to UniFi, completing MFA if requested.
5. Select adopted sensors, recipients and optional sirens, set/reuse the PIN,
   review the proposed changes and save. Adoption remains in Protect.
6. Complete physical door checks and protection activation while disarmed.
   Saving configuration alone does not establish that every sensor works.
7. Choose **Finish setup** or **Close computer access** to revoke browser access.

Access expires after 30 minutes; settings require the administrator PIN again
after ten minutes idle. Switching tabs to copy an API key does not lock setup.
Closing the browser tab alone does not immediately revoke its session.

Host, Origin, session cookie, CSRF and administrator grants are checked by the
backend. Only the setup/commissioning routes are exposed: no remote arm/disarm,
equipment control, Wi-Fi password viewing or access to private state files.

The older `KEYPAD_PHONE_*` key-handoff protocol remains for compatibility. New
installations should use the explicit `KEYPAD_WEB_*` configuration above.

# BYD Vehicle Integration for Home Assistant

Home Assistant custom integration for BYD vehicles, powered by [pyBYD](https://github.com/jkaberg/pyBYD).

> [!NOTE]
> The integration and pyBYD are nearing feature complete. A small number of API values still need final mapping/validation. Follow ongoing mapping work in pyBYD issue #20: https://github.com/jkaberg/pyBYD/issues/20

## Prerequisites

- Home Assistant with access to `custom_components`
- A BYD account dedicated for integration use (recommended)
- Control PIN configured in the BYD app if you want remote commands

## Installation

### Option 1: HACS (Custom Repository)

1. Open HACS → **Integrations**.
2. Open the menu (**⋮**) → **Custom repositories**.
3. Add `https://github.com/jkaberg/hass-byd-vehicle` as **Integration**.
4. Install **BYD Vehicle**.
5. Restart Home Assistant.
6. Add **BYD Vehicle** from **Settings → Devices & Services**.

### Option 2: Manual

1. Open your Home Assistant config directory.
2. Create `custom_components/` if needed.
3. Copy `custom_components/byd_vehicle/` from this repository into your HA config.
4. Restart Home Assistant.
5. Add **BYD Vehicle** from **Settings → Devices & Services**.

## Initial setup

Configuration is UI-only via Home Assistant config flow.

| Field | Required | Default | Description |
|---|---|---|---|
| Username | Yes | — | BYD account username (email/phone). |
| Password | Yes | — | BYD account password. |
| Country | Yes | United Kingdom | Country used for country code and language. |
| Control PIN | No | — | Optional PIN used for remote commands. |
| Climate duration | No | 10 | Climate run time in minutes. |
| Debug dump API responses | No | Off | Writes API request/response traces for troubleshooting. |

Tip: If you get invalid authentication, verify credentials first, then verify selected country.

## After setup

Entity updates are cloud-polled. Poll intervals are exposed as entities and can be tuned in automations.

## Documentation

- Troubleshooting: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- Contributing: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)

## Support and contributions

- API mapping collaboration: https://github.com/jkaberg/pyBYD/issues/20
- Bug/feature templates: [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE)
- Support discussions: https://github.com/jkaberg/hass-byd-vehicle/discussions

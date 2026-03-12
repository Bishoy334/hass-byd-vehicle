# Troubleshooting

## When to use this page
Use this guide when you need diagnostics for authentication failures, missing entities, API parsing issues, or unexpected command behavior.

## Enable debug logging
Add this to your Home Assistant `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.byd_vehicle: debug
    pybyd: debug
```

Restart Home Assistant (or reload logger configuration), then reproduce the issue.

## Where logs are located
- **Home Assistant UI**: **Settings → System → Logs**
- **Log file**: `home-assistant.log` in your Home Assistant config directory

## Debug dump API responses
Enable **Debug dump API responses** in the integration options to store BYD API request/response traces.

- Output path: `.storage/byd_vehicle_debug/`
- Home Assistant example path: `/config/.storage/byd_vehicle_debug/`

> [!CAUTION]
> Debug dump files can contain sensitive data. Enable only while troubleshooting and remove files after use.

## Raw API fetch services
Use these actions to fetch raw endpoint payloads for troubleshooting and API mapping:

- `byd_vehicle.fetch_realtime` (telemetry)
- `byd_vehicle.fetch_gps`
- `byd_vehicle.fetch_hvac`
- `byd_vehicle.fetch_charging`
- `byd_vehicle.fetch_energy`

## How to run raw fetch via Developer Tools -> Actions
1. Go to **Developer Tools → Actions**.
2. Select one of the `byd_vehicle.fetch_*` actions.
3. Choose your BYD device.
4. Run the action.
5. Collect matching logs from **Settings → System → Logs**.

## Redaction checklist
Before sharing logs or debug dump files, remove or mask:

- VIN / vehicle identifiers
- Account email or phone number
- Access tokens, session IDs, cookies
- Control PIN and any credentials
- Precise location data if not required for the report

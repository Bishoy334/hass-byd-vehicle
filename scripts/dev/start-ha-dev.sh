#!/bin/sh
set -eu

PYBYD_SRC_PATH="${PYBYD_SRC_PATH:-/workspaces/pyBYD}"
HASS_ARGS="${HASS_ARGS:---skip-pip-packages pybyd}"

echo "[hass-byd-dev] bootstrapping Home Assistant dev container"

if [ -d "$PYBYD_SRC_PATH" ]; then
  echo "[hass-byd-dev] installing editable pyBYD from: $PYBYD_SRC_PATH"
  python3 -m pip install --disable-pip-version-check --no-cache-dir -e "$PYBYD_SRC_PATH"
else
  echo "[hass-byd-dev] WARNING: pyBYD path not found at $PYBYD_SRC_PATH"
  echo "[hass-byd-dev] continuing without editable pyBYD install"
fi

mkdir -p /config/custom_components

echo "[hass-byd-dev] starting Home Assistant with args: $HASS_ARGS"
# shellcheck disable=SC2086
exec python3 -m homeassistant --config /config $HASS_ARGS

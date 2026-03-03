"""Data coordinators for BYD Vehicle."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from pybyd import (
    BydApiError,
    BydAuthenticationError,
    BydCar,
    BydClient,
    BydControlPasswordError,
    BydDataUnavailableError,
    BydEndpointNotSupportedError,
    BydRateLimitError,
    BydSessionExpiredError,
    BydTransportError,
    CommandAckEvent,
    CommandLifecycleEvent,
    VehicleSnapshot,
)
from pybyd.config import BydConfig, DeviceProfile
from pybyd.models.vehicle import Vehicle

from .const import (
    CONF_BASE_URL,
    CONF_CONTROL_PIN,
    CONF_COUNTRY_CODE,
    CONF_DEBUG_DUMPS,
    CONF_DEVICE_PROFILE,
    CONF_LANGUAGE,
    DEFAULT_DEBUG_DUMPS,
    DEFAULT_LANGUAGE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_HA_EVENT_COMMAND_LIFECYCLE: str = f"{DOMAIN}_command_lifecycle"

_AUTH_ERRORS = (BydAuthenticationError, BydSessionExpiredError)
_RECOVERABLE_ERRORS = (
    BydApiError,
    BydTransportError,
    BydRateLimitError,
    BydEndpointNotSupportedError,
)


class BydApi:
    """Thin wrapper around the pybyd client.

    Manages client lifecycle, exception translation, MQTT callback wiring,
    and debug dump writing.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, session: Any) -> None:
        self._hass = hass
        self._entry = entry
        self._http_session = session
        time_zone = hass.config.time_zone or "UTC"
        device = DeviceProfile(**entry.data[CONF_DEVICE_PROFILE])
        self._config = BydConfig(
            username=entry.data["username"],
            password=entry.data["password"],
            base_url=entry.data[CONF_BASE_URL],
            country_code=entry.data.get(CONF_COUNTRY_CODE, "NL"),
            language=entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            time_zone=time_zone,
            device=device,
            control_pin=entry.data.get(CONF_CONTROL_PIN) or None,
        )
        self._client: BydClient | None = None
        self._debug_dumps_enabled = entry.options.get(
            CONF_DEBUG_DUMPS,
            DEFAULT_DEBUG_DUMPS,
        )
        self._debug_dump_dir = Path(hass.config.path(".storage/byd_vehicle_debug"))
        self._coordinators: dict[str, BydDataUpdateCoordinator] = {}
        _LOGGER.debug(
            "BYD API initialized: entry_id=%s, region=%s, language=%s",
            entry.entry_id,
            entry.data[CONF_BASE_URL],
            entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        )

    # ------------------------------------------------------------------
    # Debug dumps
    # ------------------------------------------------------------------

    def _write_debug_dump(self, category: str, payload: dict[str, Any]) -> None:
        if not self._debug_dumps_enabled:
            return
        try:
            self._debug_dump_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
            file_path = self._debug_dump_dir / f"{timestamp}_{category}.json"
            file_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to write BYD debug dump.", exc_info=True)

    async def _async_write_debug_dump(
        self,
        category: str,
        payload: dict[str, Any],
    ) -> None:
        await self._hass.async_add_executor_job(
            self._write_debug_dump, category, payload
        )

    # ------------------------------------------------------------------
    # pyBYD callbacks
    # ------------------------------------------------------------------

    def _handle_mqtt_event(
        self, event: str, vin: str, respond_data: dict[str, Any]
    ) -> None:
        """Handle generic MQTT events from pyBYD."""
        if self._debug_dumps_enabled:
            dump: dict[str, Any] = {
                "vin": vin,
                "mqtt_event": event,
                "respond_data": respond_data,
            }
            self._hass.async_create_task(
                self._async_write_debug_dump(f"mqtt_{event}", dump)
            )

    def _handle_command_ack(self, ack: CommandAckEvent) -> None:
        """Process a structured command ACK from pyBYD (diagnostics)."""
        _LOGGER.debug(
            "Command ack received: vin=%s serial=%s correlated=%s success=%s result=%s",
            ack.vin[-6:] if ack.vin else "-",
            ack.request_serial,
            ack.is_correlated,
            ack.success,
            ack.result,
        )

    def _handle_command_lifecycle(self, event: CommandLifecycleEvent) -> None:
        """Handle pyBYD-owned command lifecycle events."""
        payload: dict[str, Any] = {
            "vin": event.vin,
            "request_serial": event.request_serial,
            "status": event.status.value,
            "reason": event.reason,
            "command": event.command,
            "timestamp": event.timestamp,
        }
        if event.ack is not None:
            payload["ack_success"] = event.ack.success
            payload["ack_result"] = event.ack.result

        self._hass.bus.async_fire(_HA_EVENT_COMMAND_LIFECYCLE, payload)

        _LOGGER.debug(
            "Command lifecycle event: vin=%s serial=%s status=%s reason=%s",
            event.vin[-6:] if event.vin else "-",
            event.request_serial,
            event.status.value,
            event.reason,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_coordinators(
        self, coordinators: dict[str, BydDataUpdateCoordinator]
    ) -> None:
        """Register telemetry coordinators (used by on_state_changed)."""
        self._coordinators = coordinators

    @property
    def config(self) -> BydConfig:
        return self._config

    @property
    def debug_dumps_enabled(self) -> bool:
        return self._debug_dumps_enabled

    async def async_write_debug_dump(
        self, category: str, payload: dict[str, Any]
    ) -> None:
        await self._async_write_debug_dump(category, payload)

    async def async_shutdown(self) -> None:
        await self._invalidate_client()

    async def _ensure_client(self) -> BydClient:
        if self._client is None:
            _LOGGER.debug(
                "Creating new pyBYD client: entry_id=%s",
                self._entry.entry_id,
            )
            self._client = BydClient(
                self._config,
                session=self._http_session,
                on_mqtt_event=self._handle_mqtt_event,
                on_command_ack=self._handle_command_ack,
                on_command_lifecycle=self._handle_command_lifecycle,
            )
            await self._client.async_start()
        return self._client

    async def _invalidate_client(self) -> None:
        if self._client is not None:
            _LOGGER.debug(
                "Invalidating pyBYD client: entry_id=%s",
                self._entry.entry_id,
            )
            try:
                await self._client.async_close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def async_get_car(self, vin: str, vehicle: Vehicle) -> BydCar:
        """Obtain a ``BydCar`` aggregate for *vin*.

        The ``on_state_changed`` callback triggers coordinator updates
        so that HA entities re-render immediately on any state change
        (including MQTT push and post-command projections).
        """
        client = await self._ensure_client()

        def _on_state_changed(changed_vin: str, snapshot: VehicleSnapshot) -> None:
            coordinator = self._coordinators.get(changed_vin)
            if coordinator is not None:
                coordinator._async_handle_state_push(snapshot)

        return await client.get_car(
            vin,
            vehicle=vehicle,
            on_state_changed=_on_state_changed,
        )

    async def async_call(
        self,
        handler: Any,
        *,
        vin: str | None = None,
        command: str | None = None,
    ) -> Any:
        """Execute a raw pyBYD call with error translation.

        Handles session expiry (re-auth), transport errors, rate limits,
        and authentication failures.  Used during initial setup and by
        the GPS coordinator.
        """
        call_started = perf_counter()
        _LOGGER.debug(
            "BYD API call started: entry_id=%s, vin=%s, command=%s",
            self._entry.entry_id,
            vin[-6:] if vin else "-",
            command or "-",
        )
        try:
            client = await self._ensure_client()
            result = await handler(client)
            _LOGGER.debug(
                "BYD API call succeeded: entry_id=%s, vin=%s, "
                "command=%s, duration_ms=%.1f",
                self._entry.entry_id,
                vin[-6:] if vin else "-",
                command or "-",
                (perf_counter() - call_started) * 1000,
            )
            return result
        except BydSessionExpiredError:
            await self._invalidate_client()
            try:
                client = await self._ensure_client()
                return await handler(client)
            except (
                BydSessionExpiredError,
                BydAuthenticationError,
            ) as retry_exc:
                raise ConfigEntryAuthFailed(str(retry_exc)) from retry_exc
            except (BydApiError, BydTransportError) as retry_exc:
                raise UpdateFailed(str(retry_exc)) from retry_exc
            except Exception as retry_exc:  # noqa: BLE001
                raise UpdateFailed(str(retry_exc)) from retry_exc
        except BydControlPasswordError as exc:
            raise UpdateFailed(
                "Control PIN rejected or cloud control temporarily locked"
            ) from exc
        except BydRateLimitError as exc:
            raise UpdateFailed(
                "Command rate limited by BYD cloud, please retry shortly"
            ) from exc
        except BydEndpointNotSupportedError as exc:
            raise UpdateFailed("Feature not supported for this vehicle/region") from exc
        except BydTransportError as exc:
            await self._invalidate_client()
            raise UpdateFailed(str(exc)) from exc
        except BydAuthenticationError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except BydApiError as exc:
            raise UpdateFailed(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "BYD API call failed: entry_id=%s, vin=%s, command=%s, "
                "duration_ms=%.1f, error=%s",
                self._entry.entry_id,
                vin[-6:] if vin else "-",
                command or "-",
                (perf_counter() - call_started) * 1000,
                type(exc).__name__,
            )
            raise


class BydDataUpdateCoordinator(DataUpdateCoordinator[VehicleSnapshot]):
    """Coordinator for telemetry + HVAC updates for a single VIN.

    Holds a ``BydCar`` reference (set after first refresh).
    ``_async_update_data()`` calls ``car.update_realtime()`` and
    conditionally ``car.update_hvac()``, then returns ``car.state``.
    Receives state-change callbacks from the state engine, which
    trigger ``async_set_updated_data(car.state)``.
    Retains ``_should_fetch_hvac()`` as consumer-side optimisation.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: BydApi,
        vehicle: Vehicle,
        vin: str,
        poll_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_telemetry_{vin[-6:]}",
            update_interval=timedelta(seconds=poll_interval),
        )
        self._api = api
        self._vehicle = vehicle
        self._vin = vin
        self._fixed_interval = timedelta(seconds=poll_interval)
        self._polling_enabled = True
        self._force_next_refresh = False
        self._car: BydCar | None = None
        self._realtime_endpoint_unsupported: bool = False

    # ------------------------------------------------------------------
    # State-engine push (no timer reset)
    # ------------------------------------------------------------------

    @callback
    def _async_handle_state_push(self, snapshot: VehicleSnapshot) -> None:
        """Update data from a state-engine push without resetting the refresh timer.

        Entities re-render immediately, but the next scheduled HTTP poll fires
        at its originally planned time rather than being pushed out.  This
        prevents GPS updates, command projections, and frequent MQTT pushes
        from starving the telemetry polling cycle.
        """
        self.data = snapshot
        self.last_update_success = True
        self.async_update_listeners()

    @property
    def car(self) -> BydCar | None:
        """Return the ``BydCar`` instance if available."""
        return self._car

    @property
    def vehicle(self) -> Vehicle:
        return self._vehicle

    @property
    def vin(self) -> str:
        return self._vin

    @staticmethod
    def _is_vehicle_on_from_snapshot(
        snapshot: VehicleSnapshot | None,
    ) -> bool | None:
        if snapshot is None or snapshot.realtime is None:
            return None
        return snapshot.realtime.is_vehicle_on

    @property
    def is_vehicle_on(self) -> bool:
        return self._is_vehicle_on_from_snapshot(self.data) is True

    def _should_fetch_hvac(
        self,
        snapshot: VehicleSnapshot | None,
        *,
        force: bool = False,
    ) -> bool:
        """Decide whether HVAC data should be fetched."""
        if force:
            return True
        if snapshot is not None and snapshot.hvac is None:
            return True
        return self._is_vehicle_on_from_snapshot(snapshot) is True

    async def _async_update_data(self) -> VehicleSnapshot:
        """Fetch telemetry + conditional HVAC and return car.state."""
        _LOGGER.debug("Telemetry refresh started: vin=%s", self._vin[-6:])
        force = self._force_next_refresh
        self._force_next_refresh = False

        if not self._polling_enabled and not force:
            if self.data is not None:
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        if self._car is None:
            self._car = await self._api.async_get_car(self._vin, self._vehicle)

        car = self._car

        # --- Realtime ---
        try:
            await car.update_realtime()
        except _AUTH_ERRORS:
            raise
        except BydEndpointNotSupportedError:
            if not self._realtime_endpoint_unsupported:
                _LOGGER.warning(
                    "Realtime HTTP endpoint not supported for vin=%s — "
                    "will rely on MQTT push (logged once only)",
                    self._vin,
                )
                self._realtime_endpoint_unsupported = True
        except _RECOVERABLE_ERRORS as exc:
            _LOGGER.warning(
                "Realtime fetch failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

        # --- HVAC (conditional) ---
        if self._should_fetch_hvac(car.state, force=force):
            try:
                await car.update_hvac()
            except _AUTH_ERRORS:
                raise
            except _RECOVERABLE_ERRORS as exc:
                _LOGGER.warning(
                    "HVAC fetch failed: vin=%s, error=%s",
                    self._vin,
                    exc,
                )
        else:
            _LOGGER.debug(
                "HVAC fetch skipped: vin=%s, reason=vehicle_not_on",
                self._vin[-6:],
            )

        snapshot = car.state

        # Bail if we still have no realtime data at all
        if snapshot.realtime is None and not self._realtime_endpoint_unsupported:
            raise UpdateFailed(
                f"Realtime state unavailable for {self._vin}; no data returned from API"
            )

        # Debug dump
        if self._api.debug_dumps_enabled:
            dump: dict[str, Any] = {"vin": self._vin, "sections": {}}
            if snapshot.realtime is not None:
                dump["sections"]["realtime"] = snapshot.realtime.model_dump(mode="json")
            if snapshot.hvac is not None:
                dump["sections"]["hvac"] = snapshot.hvac.model_dump(mode="json")
            self.hass.async_create_task(
                self._api.async_write_debug_dump("telemetry", dump)
            )

        _LOGGER.debug(
            "Telemetry refresh succeeded: vin=%s, realtime=%s, hvac=%s",
            self._vin[-6:],
            snapshot.realtime is not None,
            snapshot.hvac is not None,
        )
        return snapshot

    # ------------------------------------------------------------------
    # Polling control
    # ------------------------------------------------------------------

    @property
    def polling_enabled(self) -> bool:
        return self._polling_enabled

    def set_polling_enabled(self, enabled: bool) -> None:
        self._polling_enabled = bool(enabled)
        self.update_interval = self._fixed_interval if self._polling_enabled else None

    async def async_force_refresh(self) -> None:
        self._force_next_refresh = True
        await self.async_request_refresh()

    # ------------------------------------------------------------------
    # Service helpers — direct BydCar calls
    # ------------------------------------------------------------------

    async def async_fetch_realtime(self) -> None:
        """Service handler: fetch fresh realtime via BydCar."""
        if self._car is None:
            return
        try:
            result = await self._car.update_realtime()
            _LOGGER.info(
                "fetch_realtime result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_realtime failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    async def async_fetch_hvac(self) -> None:
        """Service handler: fetch fresh HVAC via BydCar."""
        if self._car is None:
            return
        try:
            result = await self._car.update_hvac()
            _LOGGER.info(
                "fetch_hvac result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_hvac failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    async def async_fetch_charging(self) -> None:
        """Service handler: fetch charging status and log the raw response."""
        if self._car is None:
            return
        try:
            result = await self._car.update_charging()
            _LOGGER.info(
                "fetch_charging result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_charging failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    async def async_fetch_energy(self) -> None:
        """Service handler: fetch energy consumption and log the raw response."""
        if self._car is None:
            return
        try:
            result = await self._car.update_energy()
            _LOGGER.info(
                "fetch_energy result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_energy failed: vin=%s, error=%s",
                self._vin,
                exc,
            )


class BydGpsUpdateCoordinator(DataUpdateCoordinator[VehicleSnapshot]):
    """Coordinator for GPS updates for a single VIN.

    Uses the ``BydCar`` from the telemetry coordinator so GPS data flows
    through the same state engine and benefits from the value-quality
    validators (Null Island rejection).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: BydApi,
        vehicle: Vehicle,
        vin: str,
        poll_interval: int,
        *,
        telemetry_coordinator: BydDataUpdateCoordinator | None = None,
        smart_polling: bool = False,
        active_interval: int = 30,
        inactive_interval: int = 600,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_gps_{vin[-6:]}",
            update_interval=timedelta(seconds=poll_interval),
        )
        self._api = api
        self._vehicle = vehicle
        self._vin = vin
        self._telemetry_coordinator = telemetry_coordinator
        self._smart_polling = bool(smart_polling)
        self._fixed_interval = timedelta(seconds=poll_interval)
        self._active_interval = timedelta(seconds=active_interval)
        self._inactive_interval = timedelta(seconds=inactive_interval)
        self._current_interval = self._fixed_interval
        self._polling_enabled = True
        self._force_next_refresh = False

    @property
    def polling_enabled(self) -> bool:
        return self._polling_enabled

    def set_polling_enabled(self, enabled: bool) -> None:
        self._polling_enabled = bool(enabled)
        self.update_interval = self._current_interval if self._polling_enabled else None

    async def async_force_refresh(self) -> None:
        self._force_next_refresh = True
        await self.async_request_refresh()

    async def async_fetch_gps(self) -> None:
        """Service handler: fetch fresh GPS via BydCar."""
        car = self._get_car()
        if car is None:
            return
        try:
            result = await car.update_gps()
            _LOGGER.info(
                "fetch_gps result: vin=%s, payload=%s",
                self._vin[-6:],
                result,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Service fetch_gps failed: vin=%s, error=%s",
                self._vin,
                exc,
            )

    def _get_car(self) -> BydCar | None:
        """Return BydCar from telemetry coordinator."""
        if self._telemetry_coordinator is not None:
            return self._telemetry_coordinator.car
        return None

    def _adjust_interval(self) -> None:
        if not self._smart_polling:
            self._current_interval = self._fixed_interval
        else:
            self._current_interval = (
                self._active_interval
                if self._telemetry_coordinator is not None
                and self._telemetry_coordinator.is_vehicle_on
                else self._inactive_interval
            )
        if self._polling_enabled:
            self.update_interval = self._current_interval

    async def _async_update_data(self) -> VehicleSnapshot:
        """Fetch GPS data and return the current car state snapshot."""
        _LOGGER.debug("GPS refresh started: vin=%s", self._vin[-6:])
        force = self._force_next_refresh
        self._force_next_refresh = False

        if not self._polling_enabled and not force:
            if self.data is not None:
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        car = self._get_car()
        if car is None:
            if self.data is not None:
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        try:
            await car.update_gps()
        except _AUTH_ERRORS:
            raise
        except BydDataUnavailableError:
            _LOGGER.debug(
                "GPS data unavailable (vehicle may lack signal): vin=%s",
                self._vin,
            )
        except _RECOVERABLE_ERRORS as exc:
            _LOGGER.warning("GPS fetch failed: vin=%s, error=%s", self._vin, exc)

        snapshot = car.state
        if snapshot.gps is None:
            if self.data is not None:
                _LOGGER.debug(
                    "GPS unavailable, preserving last known position: vin=%s",
                    self._vin,
                )
                return self.data
            return VehicleSnapshot(vehicle=self._vehicle)

        if self._api.debug_dumps_enabled and snapshot.gps is not None:
            dump: dict[str, Any] = {
                "vin": self._vin,
                "sections": {"gps": snapshot.gps.model_dump(mode="json")},
            }
            self.hass.async_create_task(self._api.async_write_debug_dump("gps", dump))

        self._adjust_interval()
        _LOGGER.debug(
            "GPS refresh succeeded: vin=%s, gps=%s",
            self._vin[-6:],
            snapshot.gps is not None,
        )
        return snapshot


def get_vehicle_display(vehicle: Vehicle) -> str:
    return vehicle.model_name or vehicle.vin

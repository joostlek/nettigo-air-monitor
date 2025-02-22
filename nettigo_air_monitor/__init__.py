"""Python wrapper for getting air quality data from Nettigo Air Monitor devices."""
from __future__ import annotations

import asyncio
import logging
import re
from http import HTTPStatus
from typing import Any

from aiohttp import ClientConnectorError, ClientResponseError, ClientSession
from aqipy import caqi_eu
from dacite import from_dict

from .const import (
    ATTR_CONFIG,
    ATTR_DATA,
    ATTR_OTA,
    ATTR_RESTART,
    ATTR_UPTIME,
    ATTR_VALUES,
    DEFAULT_TIMEOUT,
    ENDPOINTS,
    MAC_PATTERN,
    RENAME_KEY_MAP,
    RESPONSES_FROM_CACHE,
)
from .exceptions import (
    ApiError,
    AuthFailedError,
    CannotGetMacError,
    InvalidSensorDataError,
    NotRespondingError,
)
from .model import ConnectionOptions, NAMSensors

_LOGGER = logging.getLogger(__name__)


class NettigoAirMonitor:
    """Main class to perform Nettigo Air Monitor requests."""

    def __init__(self, session: ClientSession, options: ConnectionOptions) -> None:
        """Initialize."""
        self.host = options.host
        self._last_data: dict[str, Any] = {}
        self._options = options
        self._session = session
        self._software_version: str
        self._update_errors: int = 0
        self._auth_enabled: bool = False

    @classmethod
    async def create(
        cls, session: ClientSession, options: ConnectionOptions
    ) -> NettigoAirMonitor:
        """Create a new device instance."""
        instance = cls(session, options)
        await instance.initialize()
        return instance

    async def initialize(self) -> None:
        """Initialize."""
        _LOGGER.debug("Initializing device %s", self.host)

        try:
            config = await self.async_check_credentials()
        except AuthFailedError:
            self._auth_enabled = True
        else:
            self._auth_enabled = config["www_basicauth_enabled"]
            self._software_version = config.get("SOFTWARE_VERSION", "")

    @staticmethod
    def _construct_url(arg: str, **kwargs: str) -> str:
        """Construct Nettigo Air Monitor URL."""
        return ENDPOINTS[arg].format(**kwargs)

    @staticmethod
    def _parse_sensor_data(data: dict[Any, Any]) -> dict[str, int | float]:
        """Parse sensor data dict."""
        result = {
            item["value_type"].lower(): round(float(item["value"]), 1) for item in data
        }

        for key, value in result.items():
            if "pressure" in key and value is not None:
                result[key] = value / 100

        for old_key, new_key in RENAME_KEY_MAP:
            if result.get(old_key) is not None:
                result[new_key] = result.pop(old_key)

        return result

    async def _async_http_request(self, method: str, url: str) -> Any:
        """Retrieve data from the device."""
        try:
            _LOGGER.debug("Requesting %s, method: %s", url, method)
            resp = await self._session.request(
                method,
                url,
                raise_for_status=True,
                auth=self._options.auth,
                timeout=DEFAULT_TIMEOUT,
            )
        except ClientResponseError as error:
            if error.status == HTTPStatus.UNAUTHORIZED.value:
                raise AuthFailedError("Authorization has failed") from error
            raise ApiError(
                f"Invalid response from device {self.host}: {error.status}"
            ) from error
        except (ClientConnectorError, asyncio.TimeoutError) as error:
            _LOGGER.info("Invalid response from device: %s", self.host)
            raise NotRespondingError(
                f"The device {self.host} is not responding"
            ) from error

        _LOGGER.debug("Data retrieved from %s, status: %s", self.host, resp.status)
        if resp.status != HTTPStatus.OK.value:
            raise ApiError(f"Invalid response from device {self.host}: {resp.status}")

        return resp

    async def async_update(self) -> NAMSensors:
        """Retrieve data from the device."""
        url = self._construct_url(ATTR_DATA, host=self.host)

        try:
            resp = await self._async_http_request("get", url)
        except NotRespondingError as error:
            if self._update_errors <= RESPONSES_FROM_CACHE and self._last_data:
                _LOGGER.info(
                    "Using the cached data because the device %s is not responding",
                    self.host,
                )
                data = self._last_data
                self._update_errors += 1
            else:
                raise ApiError(error.status) from error
        else:
            data = self._last_data = await resp.json()
            self._update_errors = 0

        self._software_version = data["software_version"]

        try:
            sensors = self._parse_sensor_data(data["sensordatavalues"])
        except (TypeError, KeyError) as error:
            raise InvalidSensorDataError("Invalid sensor data") from error

        if ATTR_UPTIME in data:
            sensors[ATTR_UPTIME] = int(data[ATTR_UPTIME])

        for sensor in ("pms", "sds011", "sps30"):
            value, data = caqi_eu.get_caqi(
                pm10_1h=sensors.get(f"{sensor}_p1"),
                pm25_1h=sensors.get(f"{sensor}_p2"),
                with_level=True,
            )
            if value is not None and value > -1:
                sensors[f"{sensor}_caqi"] = value
                sensors[f"{sensor}_caqi_level"] = data["level"].replace(" ", "_")

        result: NAMSensors = from_dict(data_class=NAMSensors, data=sensors)

        return result

    async def async_get_mac_address(self) -> str:
        """Retrieve the device MAC address."""
        url = self._construct_url(ATTR_VALUES, host=self.host)

        try:
            resp = await self._async_http_request("get", url)
        except NotRespondingError as error:
            raise ApiError(error.status) from error

        data = await resp.text()

        if not (mac := re.search(MAC_PATTERN, data)):
            raise CannotGetMacError("Cannot get MAC address from device")

        return mac[0]

    async def async_check_credentials(self) -> Any:
        """Request config.json to check credentials."""
        url = self._construct_url(ATTR_CONFIG, host=self.host)

        try:
            resp = await self._async_http_request("get", url)
        except NotRespondingError as error:
            raise ApiError(error.status) from error

        return await resp.json()

    @property
    def software_version(self) -> str:
        """Return software version."""
        return self._software_version

    @property
    def auth_enabled(self) -> bool:
        """Return True if basic auth is enabled."""
        return self._auth_enabled

    async def async_restart(self) -> None:
        """Restart the device."""
        url = self._construct_url(ATTR_RESTART, host=self.host)

        try:
            await self._async_http_request("post", url)
        except NotRespondingError as error:
            raise ApiError(error.status) from error

    async def async_ota_update(self) -> None:
        """Trigger OTA update."""
        url = self._construct_url(ATTR_OTA, host=self.host)

        try:
            await self._async_http_request("post", url)
        except NotRespondingError as error:
            raise ApiError(error.status) from error

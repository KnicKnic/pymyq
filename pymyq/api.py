"""Define the MyQ API."""
import asyncio
from datetime import datetime, timedelta
import logging
from typing import Dict, Optional

from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientError

from .const import DEVICES_API_VERSION
from .device import MyQDevice
from .errors import InvalidCredentialsError, RequestError

_LOGGER = logging.getLogger(__name__)

BASE_API_VERSION = 5
API_BASE = "https://api.myqdevice.com/api/v{0}"

DEFAULT_APP_ID = "JVM/G9Nwih5BwKgNCjLxiFUQxQijAebyyg8QUHr7JOrP+tuPb8iHfRHKwTmDzHOu"
DEFAULT_USER_AGENT = "okhttp/3.10.0"
DEFAULT_BRAND_ID = 2
DEFAULT_REQUEST_RETRIES = 5
DEFAULT_CULTURE = "en"
MYQ_HEADERS = {
    "Content-Type": "application/json",
    "MyQApplicationId": DEFAULT_APP_ID,
    "User-Agent": DEFAULT_USER_AGENT,
    "ApiVersion": str(DEVICES_API_VERSION),
    "BrandId": str(DEFAULT_BRAND_ID),
    "Culture": DEFAULT_CULTURE
}
DEFAULT_STATE_UPDATE_INTERVAL = timedelta(seconds=5)
NON_COVER_DEVICE_FAMILIES = "gateway"


class API:  # pylint: disable=too-many-instance-attributes
    """Define a class for interacting with the MyQ iOS App API."""

    def __init__(self, websession: ClientSession = None) -> None:
        """Initialize."""
        self._account_info = {}
        self._credentials = {}
        self._last_state_update = None  # type: Optional[datetime]
        self._lock = asyncio.Lock()
        self._security_token = None  # type: Optional[str]
        self._websession = websession
        self.devices = {}  # type: Dict[str, MyQDevice]

    @property
    def account_id(self) -> str:
        """Return the account ID."""
        return self._account_info["Account"]["Id"]

    @property
    def covers(self) -> Dict[str, MyQDevice]:
        """Return only those devices that are covers."""
        return {
            device_id: device
            for device_id, device in self.devices.items()
            if device.device_json["device_family"] not in NON_COVER_DEVICE_FAMILIES
        }

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        headers: dict = None,
        params: dict = None,
        json: dict = None,
        login_request: bool = False,
        api_version: str = BASE_API_VERSION,
        **kwargs
    ) -> dict:
        """Make a request."""
        api_base = API_BASE.format(api_version)
        url = "{0}/{1}".format(api_base, endpoint)

        if not headers:
            headers = {}
        if not login_request:
            headers["SecurityToken"] = self._security_token
        headers.update(MYQ_HEADERS)

        # The MyQ API can time out if multiple concurrent requests are made, so
        # ensure that only one gets through at a time:
        async with self._lock:
            for attempt in range(DEFAULT_REQUEST_RETRIES):
                data = {}
                try:
                    _LOGGER.debug(f"Sending myq api request {url}")
                    async with self._websession.request(
                        method, url, headers=headers, params=params, json=json, **kwargs
                    ) as resp:
                        data = await resp.json(content_type=None)
                        resp.raise_for_status()
                        return data
                except ClientError as err:
                    _LOGGER.debug(f"Attempt {attempt} request failed with exception: {str(err)}")
                    if hasattr(err, "status") and err.status == 401:
                        if login_request:
                            self._credentials = {}
                            raise InvalidCredentialsError("Invalid username/password")

                        _LOGGER.debug(f"Security token has expired, re-authenticating to MyQ")
                        try:
                            await api.authenticate(self._credentials.get("username"), self._credentials.get("password"))
                        except RequestError as auth_err:
                            message = f"Error trying to re-authenticate to myQ service: {str(auth_err)}"
                            _LOGGER.error(message)
                            raise RequestError(message)

                    if attempt == DEFAULT_REQUEST_RETRIES - 1:
                        message = f"Error requesting data from {url}: {data.get('description', str(err))}"
                        _LOGGER.error(message)
                        raise RequestError(message)

                    wait_for = min(2 ** attempt, 5)

                    _LOGGER.warning(f"Device update failed; trying again in {wait_for} seconds")

                    await asyncio.sleep(wait_for)

    async def authenticate(self, username: str, password: str) -> None:
        """Authenticate and get a security token."""
        if username is None or password is None:
            _LOGGER.debug("No username/password, most likely due to previous failed authentication.")
            return

        self._credentials = {"username": username, "password": password}
        # Retrieve and store the initial security token:
        _LOGGER.debug("Sending authentication request to MyQ")
        auth_resp = await self.request(
            "post",
            "Login",
            json={"Username": username, "Password": password},
            login_request=True,
        )
        self._security_token = auth_resp.get("SecurityToken")
        if self._security_token is None:
            raise RequestError(
                "Authentication response did not contain a security token yet one is expected."
            )

        # Retrieve and store account info:
        _LOGGER.debug("Retrieving account information")
        self._account_info = await self.request(
            "get", "My", params={"expand": "account"}
        )

        # Retrieve and store initial set of devices:
        _LOGGER.debug("Retrieving MyQ information")
        await self.update_device_info()

    async def update_device_info(self) -> dict:
        """Get up-to-date device info."""
        # The MyQ API can time out if state updates are too frequent; therefore,
        # if back-to-back requests occur within a threshold, respond to only the first:
        call_dt = datetime.utcnow()
        if not self._last_state_update:
            self._last_state_update = call_dt - DEFAULT_STATE_UPDATE_INTERVAL
        next_available_call_dt = self._last_state_update + DEFAULT_STATE_UPDATE_INTERVAL

        if call_dt < next_available_call_dt:
            _LOGGER.debug("Ignoring subsequent request within throttle window")
            return

        devices = []
        _LOGGER.debug("Retrieving accounts")
        accounts_resp = await self.request(
            "get", "Accounts"
        )
        if accounts_resp is not None and accounts_resp.get("Items") is not None:
            for account in accounts_resp["Items"]:
                account_id = account["Id"]
                _LOGGER.debug(f"Retrieving devices for account {account_id}")
                devices_resp = await self.request(
                    "get", "Accounts/{0}/Devices".format(account_id), api_version=DEVICES_API_VERSION
                )
                if devices_resp is not None and devices_resp.get("items") is not None:
                    devices += devices_resp["items"]

        if not devices:
            _LOGGER.debug("Retrieving device information")
            devices_resp = await self.request(
                "get", "Accounts/{0}/Devices".format(self.account_id), api_version=DEVICES_API_VERSION
            )

            if devices_resp is not None and devices_resp.get("items") is not None:
                devices += devices_resp["items"]

        if not devices:
            _LOGGER.debug("Response did not contain any devices, no updates.")
            return

        for device_json in devices:
            serial_number = device_json.get("serial_number")
            if serial_number is None:
                _LOGGER.debug("No serial number for device with name {name}.".format(name=device_json.get("name")))
                continue

            if serial_number in self.devices:
                device = self.devices[serial_number]
                device.device_json = device_json
            else:
                self.devices[device_json["serial_number"]] = MyQDevice(
                    self, device_json
                )

        self._last_state_update = datetime.utcnow()


async def login(username: str, password: str, websession: ClientSession = None) -> API:
    """Log in to the API."""
    api = API(websession)
    await api.authenticate(username, password)
    return api

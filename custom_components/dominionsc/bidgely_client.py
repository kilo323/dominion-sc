from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone, date, time as dt_time
from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING
from urllib.parse import urlparse

import requests

if TYPE_CHECKING:
    # Avoid a runtime import cycle: the dominion_sc_client module imports
    # BidgelyClient. Importing DominionSCClient only for type checking avoids
    # the circular import at runtime while preserving type hints.
    from .dominion_sc_client import DominionSCClient


JsonDict = Dict[str, Any]


@dataclass
class WcSessionInfo:
    request_id: Optional[str]
    pilot_id: Optional[int]
    client_id: Optional[str]
    access_token: Optional[str]
    access_token_expiry_ms: Optional[int]
    user_id: Optional[str]
    partner_user_id: Optional[str]
    raw: JsonDict


class BidgelyClientError(Exception):
    """Raised when the Bidgely client encounters an HTTP or API error."""


class BidgelyClient:
    """
    Thin client for the Bidgely endpoints seen in the HAR.

    Notes:
    - This client is intentionally flexible because some UI features appear to be
      driven by the same endpoint family with different query params.
    - The website HAR did not show a normal Authorization header on the GET calls,
      so this client preserves a requests.Session and bootstraps with wc-session.
    """
    BASE_URL = "https://desc-prodapi.bidgely.com"
    API_BASE = ""

    _WC_SESSION = "/v2.0/web/wc-session"
    _USAGE_CHART = "/v2.0/dashboard/users/{user_id}/usage-chart-data"
    _USAGE_DETAILS = "/v2.0/dashboard/users/{user_id}/usage-chart-details"
    _ACTIVITY_MAP = "/v2.0/dashboard/users/{user_id}/activity-map-data"
    _GB_DOWNLOAD = "/v2.0/dashboard/users/{user_id}/gb-download"
    _GB_DOWNLOAD_GAPS = "/v2.0/dashboard/users/{user_id}/gb-download-has-gaps"
    _MONTHLY_SUMMARY = "/v2.0/dashboard/users/{user_id}/monthly-summary-widget-data"
    _SHC_DATA = "/v2.0/dashboard/users/{user_id}/shc-appliance-graph-data"
    _BILL_COMPARISON = "/v2.0/dashboard/users/{user_id}/bill-comparison"
    _RECO_FEED = "/v2.0/dashboard/users/{user_id}/reco-feed-data"

    def __init__(
        self,
        dominion_client: DominionSCClient = None,
        base_url: str = BASE_URL,
        api_base: str = API_BASE,
        origin: str = "https://account.dominionenergysc.com",
        timeout: int = 30,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
    ) -> None:
        self._dominion_client = dominion_client
        self.base_url = base_url.rstrip("/")
        self.origin = origin
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = self._dominion_client.session.verify
        self.session.cookies.update(self._dominion_client.session.cookies)
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": self.origin,
                "Referer": self.origin + "/",
                "User-Agent": user_agent,
            }
        )

        # Populated after create_wc_session()
        self.wc_session_info: Optional[WcSessionInfo] = None
        self.user_id: Optional[str] = None
        self.pilot_id: Optional[int] = None

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _bool_str(value: bool) -> str:
        return "true" if value else "false"

    @staticmethod
    def _epoch_seconds(value: Optional[Union[int, float, datetime, date]]) -> Optional[int]:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return int(value.timestamp())
        if isinstance(value, date):
            dt = datetime.combine(value, dt_time.min, tzinfo=timezone.utc)
            return int(dt.timestamp())
        raise TypeError(f"Unsupported datetime value: {type(value)!r}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        expected_status: int = 200,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"

        headers = {}
        if extra_headers:
            headers.update(extra_headers)

        if json_body is not None:
            headers.setdefault("Content-Type", "application/json;charset=UTF-8")

        response = self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=self.timeout,
        )

        if response.status_code != expected_status:
            raise BidgelyClientError(
                f"{method.upper()} {url} failed: "
                f"HTTP {response.status_code} - {response.text[:500]}"
            )

        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type.lower():
            try:
                return response.json()
            except ValueError as exc:
                raise BidgelyClientError(
                    f"{method.upper()} {url} returned invalid JSON"
                ) from exc

        return response.text

    def _require_user_id(self, user_id: Optional[str] = None) -> str:
        resolved = user_id or self.user_id
        if not resolved:
            raise BidgelyClientError(
                "No user_id provided and no wc-session has been created yet."
            )
        return resolved

    # -------------------------------------------------------------------------
    # Bootstrap / setup
    # -------------------------------------------------------------------------

    def create_wc_session(
        self,
        client_id: str = "prod_desc_widget",
    ) -> WcSessionInfo:
        """
        Bootstrap a Bidgely widget session.

        This is the first call the site appears to make. It returns the userId and
        other session metadata used by downstream API calls.
        """

        """ Initialize client """
        ret = self._dominion_client.get_account_listing()  # Ensure we're logged in and have cookies
        ret = self._dominion_client.get_account_summary()  # Extra call to further ensure session is fully established

        """ Call SDKInit endpoint to get payload for wc-session """
        url = self._dominion_client._get_api_url(base_url=self.BASE_URL, api_base=self.API_BASE, endpoint_key="sdkinit")
        sdk_init = self._dominion_client._api_request(
            endpoint_key="sdkinit",
            method="GET",
            params={
                "service": "E",
                "serviceAccountType": "R",
            },
            require_auth=True,
            check_response_type=True,
        )

        sdk_data = sdk_init.get("data") if isinstance(sdk_init, dict) else sdk_init
        client_id = sdk_data.get("client_id")
        api_url = sdk_data.get("api_url")
        payload = sdk_data.get("payload")

        parsed_api = urlparse(api_url)
        #wc_url = f"{parsed_api.scheme}://{parsed_api.netloc}{self._WC_SESSION}"
        wc_path = self._WC_SESSION
        wc_body = {
            "clientId": client_id,
            "encryptedData": payload,
        }

        data = self._request(
            "POST",
            wc_path,
            json_body=wc_body,
        )

        wrapper_payload = data.get("payload", {}) if isinstance(data, dict) else {}
        token_details = wrapper_payload.get("tokenDetails", {}) or {}
        profile = wrapper_payload.get("userProfileDetails", {}) or {}

        info = WcSessionInfo(
            request_id=data.get("requestId") if isinstance(data, dict) else None,
            pilot_id=wrapper_payload.get("pilotId"),
            client_id=wrapper_payload.get("clientId"),
            access_token=token_details.get("accessToken"),
            access_token_expiry_ms=token_details.get("expiryTimeInMillis"),
            user_id=profile.get("userId"),
            partner_user_id=profile.get("partnerUserId"),
            raw=data,
        )

        self.wc_session_info = info
        self.user_id = info.user_id
        self.pilot_id = info.pilot_id

        if info.access_token:
            self.session.headers["Authorization"] = f"Bearer {info.access_token}"

        # Set required headers for API requests
        self.session.headers["x-bidgely-client-type"] = "WIDGETS"
        if info.pilot_id:
            self.session.headers["x-bidgely-pilot-id"] = str(info.pilot_id)

        return info

    def get_ui_configs(
        self,
        *,
        user_id: Optional[str] = None,
        pilot_id: Optional[int] = None,
        scoped: bool = False,
        context: str = "WEB_DASHBOARD",
        color_palette_theme_type: str = "color_palette_web",
        config_tag_type: str = "frontend_configs",
        string_tag_type: str = "ui",
        fuel_type: str = "ELECTRIC",
        user_segment: str = "RESIDENTIAL",
    ) -> JsonDict:
        """
        Optional helper for pulling UI configuration metadata.
        """
        resolved_user_id = self._require_user_id(user_id)
        resolved_pilot_id = pilot_id if pilot_id is not None else self.pilot_id
        if resolved_pilot_id is None:
            raise BidgelyClientError(
                "No pilot_id provided and no wc-session has been created yet."
            )

        params = {
            "pilotId": resolved_pilot_id,
            "userId": resolved_user_id,
            "scoped": self._bool_str(scoped),
            "context": context,
            "colorPaletteThemeType": color_palette_theme_type,
            "configTagType": config_tag_type,
            "stringTagType": string_tag_type,
            "fuelType": fuel_type,
        }

        # userSegment was seen on some calls but not all; keep it optional
        if user_segment:
            params["userSegment"] = user_segment

        return self._request(
            "POST",
            "/v2.0/web/uiConfigs",
            params=params,
            json_body=[],
        )

    # -------------------------------------------------------------------------
    # Verified data endpoints
    # -------------------------------------------------------------------------

    def get_bill_projection(
        self,
        *,
        user_id: Optional[str] = None,
        home_id: int = 1,
        measurement_type: str = "ELECTRIC",
        date_format: str = "MONTH_IN_WORDS_SPACE_DAY_COMMA_YEAR",
        locale: str = "en_US",
        round_values: bool = True,
        convert_to_kwh: bool = True,
        skip_if_completed_cycle: bool = True,
        compute_last_year: bool = False,
    ) -> JsonDict:
        """
        Get the current billing-cycle projection / estimated bill.
        """
        resolved_user_id = self._require_user_id(user_id)

        params = {
            "date-format": date_format,
            "locale": locale,
            "round": self._bool_str(round_values),
            "convert-to-kwh": self._bool_str(convert_to_kwh),
            "skip-if-completed-cycle": self._bool_str(skip_if_completed_cycle),
            "compute-last-year": self._bool_str(compute_last_year),
            "measurementType": measurement_type,
        }

        return self._request(
            "GET",
            f"/2.1/users/{resolved_user_id}/homes/{home_id}/billprojections",
            params=params,
        )

    def get_usage_chart_details(
        self,
        *,
        user_id: Optional[str] = None,
        measurement_type: str = "ELECTRIC",
        mode: str = "month",
        start: Optional[Union[int, float, datetime]] = None,
        end: Optional[Union[int, float, datetime]] = None,
        date_format: str = "DATE_TIME",
        locale: str = "en_US",
        next_bill_cycle: bool = False,
        show_at_granularity: bool = False,
        skip_ongoing_cycle: bool = True,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        """
        Generic usage/cost/itemization endpoint family.

        The HAR strongly suggests this endpoint is used for chart detail data,
        including per-interval cost and consumption, and it exposes
        'itemizationDetailsList' on each interval.
        """
        resolved_user_id = self._require_user_id(user_id)

        if start is None:
            start = int(time.time()) - 365 * 24 * 3600  # 1 year ago

        params: Dict[str, Any] = {
            "measurement-type": measurement_type,
            "mode": mode,
            "start": self._epoch_seconds(start) if start is not None else None,
            "end": self._epoch_seconds(end) if end is not None else int(time.time()),
            "date-format": date_format,
            "locale": locale,
            "next-bill-cycle": self._bool_str(next_bill_cycle),
            "show-at-granularity": self._bool_str(show_at_granularity),
            "skip-ongoing-cycle": self._bool_str(skip_ongoing_cycle),
        }

        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}

        if extra_params:
            params.update(extra_params)

        return self._request(
            "GET",
            f"/v2.0/dashboard/users/{resolved_user_id}/usage-chart-details",
            params=params,
        )

    def get_monthly_summary_widget_data(
        self,
        *,
        user_id: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        """
        Monthly summary widget endpoint.

        The HAR showed two calls to this path. Since the exact distinguishing query
        parameters can vary by current-vs-previous billing cycle, this method keeps
        the query flexible via extra_params.
        """
        resolved_user_id = self._require_user_id(user_id)

        params = extra_params or {}

        return self._request(
            "GET",
            f"/v2.0/dashboard/users/{resolved_user_id}/monthly-summary-widget-data",
            params=params,
        )

    # -------------------------------------------------------------------------
    # Feature-oriented helpers
    # -------------------------------------------------------------------------

    def get_usage_by_month(
        self,
        *,
        user_id: Optional[str] = None,
        measurement_type: str = "ELECTRIC",
        current_cycle: bool = True,
        end: Optional[Union[int, float, datetime]] = None,
        start: Optional[Union[int, float, datetime]] = None,
        locale: str = "en_US",
    ) -> JsonDict:
        """
        Convenience wrapper for month-mode usage.

        current_cycle=True:
            Uses skip_ongoing_cycle=False so the active cycle is included.
        current_cycle=False:
            Uses skip_ongoing_cycle=True to prefer the completed prior cycle.

        If your HAR shows a different current/previous-cycle distinction,
        override via get_usage_chart_details() directly.
        """
        if start is None:
            # Default to start of current month
            now = datetime.now(timezone.utc)
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        return self.get_usage_chart_details(
            user_id=user_id,
            measurement_type=measurement_type,
            mode="month",
            start=start,
            end=end if end is not None else int(time.time()),
            locale=locale,
            next_bill_cycle=False,
            show_at_granularity=False,
            skip_ongoing_cycle=not current_cycle,
        )

    def get_usage_by_day(
        self,
        *,
        user_id: Optional[str] = None,
        measurement_type: str = "ELECTRIC",
        day_start: Union[int, float, datetime],
        day_end: Union[int, float, datetime],
        locale: str = "en_US",
    ) -> JsonDict:
        """
        Convenience wrapper for day-granularity usage.

        Pass a specific day's start/end bounds (epoch seconds or datetime).
        """
        return self.get_usage_chart_details(
            user_id=user_id,
            measurement_type=measurement_type,
            mode="day",
            start=day_start,
            end=day_end,
            locale=locale,
            next_bill_cycle=False,
            show_at_granularity=True,
            skip_ongoing_cycle=False,
        )

    def get_cost_by_month(
        self,
        *,
        user_id: Optional[str] = None,
        current_cycle: bool = True,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        """
        Convenience wrapper for the monthly-summary endpoint.

        Because the HAR showed two monthly-summary calls but not a single universal
        query template for 'current' vs 'previous', this method lets you pass
        extra_params for exact control if needed.
        """
        params = dict(extra_params or {})
        params.setdefault("currentCycle", self._bool_str(current_cycle))
        return self.get_monthly_summary_widget_data(
            user_id=user_id,
            extra_params=params,
        )

    def get_bill_itemization(
        self,
        *,
        user_id: Optional[str] = None,
        mode: str = "month",
        start: Optional[Union[int, float, datetime]] = 0,
        end: Optional[Union[int, float, datetime]] = None,
        include_empty: bool = False,
    ) -> List[JsonDict]:
        """
        Best-effort itemization helper.

        The HAR did not clearly expose a separate dedicated '/itemization' endpoint
        in the narrowed endpoint set, but the usage-chart-details response includes
        'itemizationDetailsList' per interval.

        This helper calls usage-chart-details and flattens any itemization records.
        """
        data = self.get_usage_chart_details(
            user_id=user_id,
            mode=mode,
            start=start,
            end=end if end is not None else int(time.time()),
            next_bill_cycle=False,
            show_at_granularity=False,
            skip_ongoing_cycle=False,
        )

        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        intervals = payload.get("usageChartDataList", []) or []

        flattened: List[JsonDict] = []
        for interval in intervals:
            items = interval.get("itemizationDetailsList", []) or []
            if not items and include_empty:
                flattened.append(
                    {
                        "intervalStart": interval.get("intervalStart"),
                        "intervalEnd": interval.get("intervalEnd"),
                        "intervalStartDate": interval.get("intervalStartDate"),
                        "intervalEndDate": interval.get("intervalEndDate"),
                        "itemizationDetailsList": [],
                    }
                )
                continue

            for item in items:
                flattened.append(
                    {
                        "intervalStart": interval.get("intervalStart"),
                        "intervalEnd": interval.get("intervalEnd"),
                        "intervalStartDate": interval.get("intervalStartDate"),
                        "intervalEndDate": interval.get("intervalEndDate"),
                        "cost": interval.get("cost"),
                        "consumption": interval.get("consumption"),
                        "item": item,
                    }
                )

        return flattened
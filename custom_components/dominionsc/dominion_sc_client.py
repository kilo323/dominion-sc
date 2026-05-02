"""
Dominion Energy SC Home Assistant integration client.

This class encapsulates authentication and data retrieval logic for the Dominion Energy SC portal.
"""

import datetime
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import logging

from .bidgely_client import BidgelyClient, BidgelyClientError, JsonDict

_LOGGER = logging.getLogger(__name__)

class DominionSCClient:
    _bidgely_client: Optional[BidgelyClient] = None
    BASE_URL = "https://account.dominionenergysc.com"
    API_BASE = "/fusionapi"
    LOGIN_URL = "https://account.dominionenergysc.com/Access/"
    ENDPOINTS = {
        # Core
        "globals": "/fusionCoreApi/GetFusionGlobals",
        "messages": "/MessagesApi/GetMessages/",
        
        # Login/Auth (from login.js and login2.js)
        "authenticate": "/LoginWebApi/Authenticate",
        "check_login": "/LoginWebApi/CheckLogin",
        "get_aft": "/LoginWebApi/GetAFT",
        "init_auth": "/LoginWebApi/InitAuthentication",
        "send_pin": "/LoginWebApi/SendPINCode",
        "verify_pin": "/LoginWebApi/VerifyPIN",
        "verify_2fa_token": "/LoginWebApi/Verify2FAToken",
        
        # Account
        "account_listing": "/AccountManagementWebApi/GetAccountListing/",
        "account_summary": "/AccountSummaryWebApi/InitAccount/",
        "account_menu": "/CommonWebApi/GetAccountMenuLinks/",
        
        # Billing
        "daily_usage": "/CommonBillingWebApi/GetAccountDailyUsageDetails/",
        "ami_meter": "/CommonBillingWebApi/GetAccountAMIMeter/",
        "energy_analyzer_flag": "/BillingWebApi/GetAcctEnergyAnalyzerFlag/",

        # Bidgely API
        "sdkinit": "/BidgelyWebApi/GetBidgelySDKInit/",
        "wc_session": "/v2.0/web/sdkInit"
    }

    # get account listing
    def get_account_listing(self):
        """
        Get list of accounts associated with the user.
        Returns:
            List of dicts with account info
        """
        response = self._api_request(endpoint_key="account_listing")
        data = response.get("data", {})
        accounts = []
        if data.get("singleAccount"):
            return accounts
        for acct in data.get("accounts", []):
            accounts.append({
                "account_number": acct.get("accountNumber", ""),
                "service_address": acct.get("serviceAddress", ""),
                "account_status": acct.get("accountStatus", ""),
                "nickname": acct.get("nickname", ""),
                "is_closed": acct.get("accountClosed", False),
                "has_ami_meter": acct.get("hasAMIMeter", False),
                "sbu": acct.get("accountSbu", "")
            })
        return accounts

    def get_account_summary(self, account_number: str = None):
        """
        Get detailed account summary.
        Args:
            account_number: Optional account number for multi-account users
        Returns:
            Dict with detailed billing information
        """
        params = {}
        if account_number:
            params["accountNumber"] = account_number
        
        # Ensure account listing has been called to initialize server-side account context
        # (some Fusion endpoints expect a prior call to GetAccountListing/InitAccount)
        if not account_number:
            try:
                self.get_account_listing()
            except Exception:
                pass

        # Some environments return intermittent 500 for InitAccount; retry once
        last_exc = None
        for attempt in range(2):
            try:
                response = self._api_request(endpoint_key="account_summary", params=params)
                data = response.get("data") or {}
                break
            except Exception as e:
                # If server error, wait briefly and retry once
                last_exc = e
                if getattr(e, 'status_code', None) == 500 or '500' in str(e):
                    time.sleep(0.25)
                    continue
                raise
        else:
            raise Exception(f"Failed to get account summary after retries: {last_exc}")
        account = data.get("account", {})
        return {
            "customer_name": data.get("customerName", ""),
            "account_number": account.get("accountNumber", ""),
            "service_address": account.get("serviceAddressAndAccountNo", ""),
            "last_payment_amount": account.get("lastPaymentAmount", ""),
            "last_payment_date": account.get("lastPaymentDate", ""),
            "account_balance": account.get("accountBalance", ""),
            "date_billed": account.get("dateBilled", ""),
            "due_date": account.get("dueDate", ""),
            "is_paperless": data.get("isPaperless", False),
            "has_ami_meter": account.get("hasAMIMeter", False),
            "electric_utility": account.get("electricUtility", "") == "Y",
            "budget_billing_enrolled": account.get("budgetBillingEnrolled", False),
            "payment_scheduled": account.get("paymentScheduled", ""),
            "payment_scheduled_date": account.get("paymentScheduledDate", ""),
            "payment_scheduled_amount": account.get("paymentScheduledAmount", ""),
            "raw_data": data
        }

    def get_current_daily_usage(self, usage_type="BOTH", start_date=None, end_date=None, account_number=None, revenue_month=0):
        """
        Get daily energy usage data for the current billing period.
        Args:
            usage_type: 'ELECTRIC', 'GAS', or 'BOTH'
            start_date: Optional start date (YYYY-MM-DD or date)
            end_date: Optional end date (YYYY-MM-DD or date)
            account_number: Optional account number for multi-account users
        Returns:
            Dict with daily usage records and bill summary

        "/fusionapi/CommonBillingWebApi/GetAccountDailyUsageDetails/
        ?startDate=1900-01-01
        &endDate=1900-01-01
        &electricStartDate=1900-01-01
        &electricEndDate=1900-01-01
        &gasStartDate=1900-01-01
        &gasEndDate=1900-01-01
        &revenueMonth=0
        &callType=L
        &_=1774967946578"
        """

        default_date = "1900-01-01"
        def _to_iso(d):
            if d is None:
                return default_date
            if hasattr(d, 'isoformat'):
                return d.isoformat()
            return str(d)

        params = {
            "startDate": _to_iso(start_date),
            "endDate": _to_iso(end_date),
            "electricStartDate": _to_iso(start_date),
            "electricEndDate": _to_iso(end_date),
            "gasStartDate": _to_iso(start_date),
            "gasEndDate": _to_iso(end_date),
            "revenueMonth": revenue_month,
            "callType": "L"
        }

        if account_number:
            params["accountNumber"] = account_number
        response = self._api_request(endpoint_key="daily_usage", params=params)
        data = response.get("data") or {}
        bill_detail = data.get("totalBillDetail") or {}
        bill_summary = {
            "electric_usage_charges": bill_detail.get("electricUsageCharges", 0.0),
            "electric_other_charges": bill_detail.get("electricOtherCharges", 0.0),
            "electric_total": bill_detail.get("electricTotalAmount", 0.0),
            "gas_usage_charges": bill_detail.get("gasUsageCharges", 0.0),
            "gas_other_charges": bill_detail.get("gasOtherCharges", 0.0),
            "gas_total": bill_detail.get("gasTotalAmount", 0.0),
            "total_usage_charges": bill_detail.get("totalUsageCharges", 0.0),
            "total_other_charges": bill_detail.get("totalOtherCharges", 0.0),
            "total_bill_amount": bill_detail.get("totalBillAmount", 0.0)
        }
        daily_records = self._parse_daily_records(data, usage_type)
        return {
            "bill_summary": bill_summary,
            "daily_records": daily_records,
            "is_electric_only": data.get("isElectricOnly", False),
            "is_gas_only": data.get("isGasOnly", False),
            "disclaimer": data.get("disclaimerMessage", ""),
            "raw_data": data
        }

    def _parse_daily_records(self, data, usage_type):
        records = []
        labels = data.get("xAxisLabelForTotal", [])
        total_costs = data.get("barChartTotalCost", [])
        electric_costs = data.get("barChartElectricCost", [])
        gas_costs = data.get("barChartGasCost", [])
        electric_usage = data.get("barChartElectricUsage", [])
        gas_usage = data.get("barChartGasUsage", [])
        high_temps = data.get("lineChartHighTempDataForTotal", [])
        low_temps = data.get("lineChartLowTempDataForTotal", [])
        total_combined = data.get("isTotalCombinedRead", [])
        for i in range(len(labels)):
            label = labels[i] if i < len(labels) else ["", ""]
            record = {
                "date_label": label[0] if len(label) > 0 else "",
                "weekday": label[1] if len(label) > 1 else "",
                "total_cost": total_costs[i] if i < len(total_costs) else 0.0,
                "electric_cost": electric_costs[i] if i < len(electric_costs) else 0.0,
                "gas_cost": gas_costs[i] if i < len(gas_costs) else 0.0,
                "electric_usage": electric_usage[i] if i < len(electric_usage) else 0.0,
                "gas_usage": gas_usage[i] if i < len(gas_usage) else 0.0,
                "high_temp": high_temps[i] if i < len(high_temps) else 0,
                "low_temp": low_temps[i] if i < len(low_temps) else 0,
                "is_combined_read": total_combined[i] if i < len(total_combined) else False
            }
            if usage_type == "ELECTRIC" and record["electric_usage"] == 0 and record["electric_cost"] == 0:
                continue
            elif usage_type == "GAS" and record["gas_usage"] == 0 and record["gas_cost"] == 0:
                continue
            records.append(record)
        return records

    def get_ami_meter(self, account_number: str = None):
        """
        Get AMI meter info for the account.
        Args:
            account_number: Optional account number
        Returns:
            Raw AMI meter API response
        """
        params = {}
        if account_number:
            params["accountNumber"] = account_number
        response = self._api_request(endpoint_key="ami_meter", params=params)
        return response.get("data")

    def get_energy_analyzer_flag(self, account_number: str = None):
        """
        Get energy analyzer flag for the account.
        Args:
            account_number: Optional account number
        Returns:
            Raw energy analyzer flag API response
        """
        params = {}
        if account_number:
            params["accountNumber"] = account_number
        response = self._api_request(endpoint_key="energy_analyzer_flag", params=params)
        return response.get("data")

    def __init__(self, timeout: int = 30, verify_ssl: bool = True, log_requests: bool = False):
        self.timeout = timeout
        self.session = requests.Session()
        if not verify_ssl:
            self.session.verify = False
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._verification_token: Optional[str] = None
        self._log_requests = bool(log_requests)
        self._logged_in = False
        self._wc_session = None
        self._pending_2fa = False
        self._selected_2fa_option = None
        self._2fa_token: Optional[str] = None
        self._username = None
        self._password = None
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.BASE_URL + "/",
        })

    def _get_api_url(self, base_url: str = None, api_base: str = None, endpoint_key: str = "") -> str:
        if base_url is None:
            base_url = self.BASE_URL
        if api_base is None:
            api_base = self.API_BASE
        endpoint = self.ENDPOINTS.get(endpoint_key, endpoint_key)
        return urljoin(base_url, f"{api_base}{endpoint}")

    def _get_timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _ensure_verification_token(self) -> None:
        if self._verification_token:
            return
        resp = self.session.get(self.BASE_URL, timeout=self.timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        token_input = soup.find("input", {"name": "__RequestVerificationToken"})
        if token_input:
            self._verification_token = token_input.get("value")
        else:
            scripts = soup.find_all("script")
            for script in scripts:
                if script.string and "__RequestVerificationToken" in script.string:
                    match = re.search(r'__RequestVerificationToken["\']?\s*[:=]\s*["\']([^"\']+)["\']', script.string)
                    if match:
                        self._verification_token = match.group(1)
                        break
        if not self._verification_token:
            self._verification_token = ""

    def _api_request(self, url: str = None, endpoint_key: str = None, method: str = "GET", params: dict = None, data: dict = None, require_auth: bool = True, check_response_type: bool = True) -> dict:
        if require_auth and not self._logged_in:
            raise Exception("Not logged in. Please call login() first.")
        self._ensure_verification_token()
        if not url:
            url = self._get_api_url(endpoint_key=endpoint_key)
        if params is None:
            params = {}
        params["_"] = self._get_timestamp()
        headers = {
            "isajax": "true",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json",
        }
        if self._verification_token:
            headers["__RequestVerificationToken"] = self._verification_token
        if endpoint_key in ("authenticate", "check_login", "init_auth", "send_pin", "verify_pin", "verify_2fa_token", "get_aft"):
            headers["Referer"] = self.LOGIN_URL
            headers["Origin"] = self.BASE_URL
        if endpoint_key in ("sdkinit"):
            headers["Referer"] = self.LOGIN_URL
            headers["Origin"] = self.BASE_URL
        if method.upper() == "POST":
            resp = self.session.post(url, params=params, json=data, headers=headers, timeout=self.timeout)
        else:
            resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        if resp.status_code in (401, 403):
            self._logged_in = False
            raise Exception("Session expired")
        resp.raise_for_status()
        result = resp.json()
        if check_response_type and isinstance(result, dict) and "responseType" in result:
            if result.get("responseType") != 0:
                error_msg = result.get("pageMessage") or "API request failed"
                if any(kw in error_msg.lower() for kw in ("session has expired", "session expired", "please log in again", "please sign in")):
                    self._logged_in = False
                    raise Exception(error_msg)
                raise Exception(error_msg)
        return result

    def login(self, username: str, password: str) -> bool:
        self._log_requests and _LOGGER.debug("Loading login page for CSRF token...")
        login_page = self.session.get(self.LOGIN_URL, timeout=self.timeout)
        login_page.raise_for_status()
        soup = BeautifulSoup(login_page.text, "html.parser")
        token_input = soup.find("input", {"name": "__RequestVerificationToken"})
        if token_input:
            self._verification_token = token_input.get("value", "")
        self._username = username
        self._password = password
        auth_data = {"userName": username, "password": password, "token": None}
        response = self._api_request(endpoint_key="authenticate", method="POST", data=auth_data, require_auth=False, check_response_type=False)
        status = ""
        if response is True:
            status = "success"
        elif isinstance(response, dict):
            status = response.get("status", "")

        if self._2fa_token:
            try:
                status = "success" if self.verify_2fa_token(self._2fa_token) else "failed"
            except Exception:
                self._2fa_token = None
                check_response = self._api_request(endpoint_key="check_login", require_auth=False, check_response_type=False)
                status = check_response.get("status", "") if isinstance(check_response, dict) else ""
        if status == "success":
            return self._finalize_authenticated_session()
        elif status == "twoFA":
            if self._2fa_token:
                try:
                    return self.verify_2fa_token(self._2fa_token)
                except Exception:
                    self._2fa_token = None
            self._pending_2fa = True
            return self._init_2fa()
        elif status == "failed":
            raise Exception("Login failed")
        elif status == "locked":
            raise Exception("Account is locked")
        elif status == "recaptchaFailed":
            raise Exception("reCAPTCHA verification failed")
        else:
            self._pending_2fa = True
            return self._init_2fa()

    def _finalize_authenticated_session(self) -> bool:
        """Mark authenticated and bootstrap Bidgely session for downstream API calls."""
        self._logged_in = True
        self._pending_2fa = False
        try:
            self._wc_session = self._init_bidgely()
        except Exception as err:  # pylint: disable=broad-except
            # Authentication is already successful at this point. Keep session valid
            # and allow Bidgely bootstrap to be retried lazily by data calls.
            _LOGGER.warning("Bidgely bootstrap failed after auth, will retry lazily: %s", err)
            self._logged_in = True
            self._pending_2fa = False
            self._wc_session = None
        return True

    def _init_2fa(self) -> bool:
        response = self._api_request(endpoint_key="init_auth", require_auth=False, check_response_type=False)
        data = response.get("data", {})
        if data.get("status") is False:
            raise Exception("Authentication session expired")
        phone_numbers = data.get("userInfo", {}).get("phoneNumbers", [])
        email_addresses = data.get("userInfo", {}).get("emailAddresses", [])
        options = []
        for phone in phone_numbers:
            options.append({"method": "SMS", "display_value": phone, "option_id": phone})
        for email in email_addresses:
            options.append({"method": "EMAIL", "display_value": email, "option_id": email})
        if not options:
            raise Exception("No 2FA options available")
        self._2fa_options = options
        raise Exception({"2fa_required": options})

    def select_2fa_method(self, option) -> bool:
        if not self._pending_2fa:
            raise Exception("No pending 2FA. Call login() first.")
        self._selected_2fa_option = option
        send_data = {"sendMethod": option["option_id"]}
        response = self._api_request(endpoint_key="send_pin", method="POST", data=send_data, require_auth=False, check_response_type=False)
        if response is True or (isinstance(response, dict) and response.get("data") is True):
            return True
        else:
            raise Exception("Failed to send verification code.")

    def verify_2fa_code(self, code: str, remember_device: bool = False) -> bool:
        if not self._pending_2fa:
            raise Exception("No pending 2FA. Call login() first.")
        verify_data = {"PINcode": code, "registerDevice": remember_device}
        response = self._api_request(endpoint_key="verify_pin", method="POST", data=verify_data, require_auth=False, check_response_type=False)
        is_verified = False
        token = None
        if response is True:
            is_verified = True
        elif isinstance(response, dict):
            is_verified = response.get("isVerified", False) or (response.get("data") or {}).get("isVerified", False)
            token = response.get("token") or (response.get("data") or {}).get("token")
        if is_verified:
            if token and remember_device:
                self._2fa_token = token
            return self._finalize_authenticated_session()
        else:
            raise Exception("Invalid verification code")

    @property
    def tfa_token(self) -> Optional[str]:
        return self._2fa_token

    @tfa_token.setter
    def tfa_token(self, value: Optional[str]) -> None:
        self._2fa_token = value

    def verify_2fa_token(self, token: Optional[str] = None) -> bool:
        token = token or self._2fa_token
        if not token:
            raise Exception("No 2FA token available")
        self._verification_token = None
        self._ensure_verification_token()
        response = self._api_request(endpoint_key="verify_2fa_token", method="GET", params={"token": token}, require_auth=False, check_response_type=False)
        is_valid = response is True
        if isinstance(response, dict):
            is_valid = response.get("data") is True or response is True
        if not is_valid:
            self._2fa_token = None
            raise Exception("2FA token expired or invalid")
        return self._finalize_authenticated_session()

    def logout(self) -> None:
        if self._logged_in:
            try:
                logout_url = urljoin(self.BASE_URL, "/Account/Logout")
                self.session.get(logout_url, timeout=self.timeout)
            except Exception:
                pass
        self._logged_in = False
        self._pending_2fa = False
        self._verification_token = None
        self.session.cookies.clear()

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    def save_cookies(self, filepath: Union[str, Path]) -> None:
        filepath = Path(filepath).expanduser()
        cookies_data = {
            "cookies": [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires,
                    "secure": cookie.secure,
                }
                for cookie in self.session.cookies
            ],
            "verification_token": self._verification_token,
            "logged_in": self._logged_in,
            "tfa_token": self._2fa_token,
            "username": self._username,
            "password": self._password,
        }
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(cookies_data, f, indent=2)

    def load_cookies(self, filepath: Union[str, Path]) -> bool:
        filepath = Path(filepath).expanduser()
        if not filepath.exists():
            return False
        try:
            with open(filepath, "r") as f:
                cookies_data = json.load(f)
            self.session.cookies.clear()
            for cookie in cookies_data.get("cookies", []):
                try:
                    ck = requests.cookies.create_cookie(
                        name=cookie.get("name"),
                        value=cookie.get("value"),
                        domain=cookie.get("domain") or None,
                        path=cookie.get("path") or "/",
                        expires=cookie.get("expires"),
                        secure=cookie.get("secure", False),
                    )
                    self.session.cookies.set_cookie(ck)
                except Exception:
                    try:
                        self.session.cookies.set(
                            cookie["name"],
                            cookie["value"],
                            domain=cookie.get("domain", ""),
                            path=cookie.get("path", "/"),
                        )
                    except Exception:
                        pass
            self._verification_token = cookies_data.get("verification_token")
            if cookies_data.get("tfa_token"):
                self._2fa_token = cookies_data["tfa_token"]
            # Restore username/password if present
            self._username = cookies_data.get("username")
            self._password = cookies_data.get("password")
            try:
                response = self._api_request(endpoint_key="account_listing", require_auth=False, check_response_type=True)
                if isinstance(response, dict) and response.get("responseType") == 0:
                    return self._finalize_authenticated_session()
            except Exception:
                try:
                    response = self._api_request(endpoint_key="globals", require_auth=False, check_response_type=False)
                    if isinstance(response, dict) and response.get("responseType") == 0:
                        return self._finalize_authenticated_session()
                except Exception:
                    pass
            self.session.cookies.clear()
            self._verification_token = None
            return False
        except (json.JSONDecodeError, KeyError, IOError):
            return False

    def delete_cookies(self, filepath: Union[str, Path]) -> bool:
        filepath = Path(filepath).expanduser()
        if filepath.exists():
            filepath.unlink()
            return True
        return False
    
    """
        Bidgely API Data
    """
    def _init_bidgely(self):
        """Retrieve daily usage via the Bidgely client.

        This lazily instantiates a BidgelyClient bound to this
        DominionSCClient instance and returns the widget session info.
        """
        # Lazily create a BidgelyClient bound to this DominionSCClient
        if self._bidgely_client is None:
            # BidgelyClient expects a dominion_client keyword or positional arg
            self._bidgely_client = BidgelyClient(dominion_client=self)

        wc_session = self._bidgely_client.create_wc_session()
        return wc_session

    def _ensure_bidgely_ready(self) -> BidgelyClient:
        """Ensure Bidgely client exists and has an active wc-session user_id."""
        if self._bidgely_client is None:
            self._bidgely_client = BidgelyClient(dominion_client=self)

        if not getattr(self._bidgely_client, "user_id", None):
            try:
                self._wc_session = self._bidgely_client.create_wc_session()
            except Exception as err:  # pylint: disable=broad-except
                if (
                    "session expired" in str(err).lower()
                    and self._username
                    and self._password
                ):
                    _LOGGER.info("Bidgely bootstrap retrying after re-authentication")
                    self.login(self._username, self._password)
                    self._wc_session = self._bidgely_client.create_wc_session()
                else:
                    raise

        return self._bidgely_client
    
    def get_daily_usage(
        self,
        *,
        user_id: Optional[str] = None,
        measurement_type: str = "ELECTRIC",
        current_cycle: bool = True,
        end: Optional[Union[int, float, datetime.datetime]] = None,
        start: Optional[Union[int, float, datetime.datetime]] = None,
        locale: str = "en_US",
    ) -> JsonDict:
        bidgely = self._ensure_bidgely_ready()
        return bidgely.get_usage_by_month(
            user_id=user_id,
            measurement_type=measurement_type,
            current_cycle=current_cycle,
            end=end,
            start=start,
            locale=locale,
        )

    def get_hourly_usage(
        self,
        *,
        user_id: Optional[str] = None,
        measurement_type: str = "ELECTRIC",
        day_start: Optional[Union[int, float, datetime.datetime]] = None,
        day_end: Optional[Union[int, float, datetime.datetime]] = None,
        locale: str = "en_US",
    ) -> JsonDict:
        now = datetime.datetime.now()
        resolved_end = day_end if day_end is not None else now
        resolved_start = day_start if day_start is not None else resolved_end - datetime.timedelta(days=1)
        bidgely = self._ensure_bidgely_ready()
        return bidgely.get_usage_by_day(
            user_id=user_id,
            measurement_type=measurement_type,
            day_start=resolved_start,
            day_end=resolved_end,
            locale=locale,
        )
    
    def get_bill_itemization(
        self,
        *,
        user_id: Optional[str] = None,
        mode: str = "month",
        start: Optional[Union[int, float, datetime.datetime]] = 0,
        end: Optional[Union[int, float, datetime.datetime]] = None,
        include_empty: bool = False,
    ) -> List[JsonDict]:
        bidgely = self._ensure_bidgely_ready()
        return bidgely.get_bill_itemization(
            user_id=user_id,
            mode=mode,
            start=start,
            end=end,
            include_empty=include_empty,
        )

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
        bidgely = self._ensure_bidgely_ready()
        return bidgely.get_bill_projection(
            user_id=user_id,
            home_id=home_id,
            measurement_type=measurement_type,
            date_format=date_format,
            locale=locale,
            round_values=round_values,
            convert_to_kwh=convert_to_kwh,
            skip_if_completed_cycle=skip_if_completed_cycle,
            compute_last_year=compute_last_year,
        )

    def get_monthly_summary_widget_data(
        self,
        *,
        user_id: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        params = dict(extra_params or {})
        params.setdefault("currentCycle", "true")
        bidgely = self._ensure_bidgely_ready()
        return bidgely.get_monthly_summary_widget_data(
            user_id=user_id,
            extra_params=params,
        )

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
        bidgely = self._ensure_bidgely_ready()
        return bidgely.get_ui_configs(
            user_id=user_id,
            pilot_id=pilot_id,
            scoped=scoped,
            context=context,
            color_palette_theme_type=color_palette_theme_type,
            config_tag_type=config_tag_type,
            string_tag_type=string_tag_type,
            fuel_type=fuel_type,
            user_segment=user_segment,
        )

"""Resilient Roadway API client: timeouts, bounded retries with jittered backoff,
idempotent writes and a typed error.

Run directly to verify your .env is set up and the API is reachable:
    python src/roadway_client.py
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import requests

from config import Settings, load_settings


class RoadwayAPIError(Exception):
    """A failed API call: an HTTP error response, or a transport failure."""

    def __init__(
        self,
        status: int | None,
        code: str,
        message: str = "",
        details: dict | None = None,
        retryable: bool = False,
    ):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable


def _noop(_: dict) -> None:
    return None


def _error_from_response(resp) -> RoadwayAPIError:
    status = resp.status_code
    code, message, details = f"HTTP_{status}", "", {}
    try:
        body = resp.json()
    except ValueError:
        body = None
        message = (getattr(resp, "text", "") or "")[:200]
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        code = err.get("code") or code
        message = err.get("message") or ""
        details = err.get("details") or {}
    return RoadwayAPIError(status, code, message, details, retryable=status >= 500)


class RoadwayClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        settings: Settings,
        http: requests.Session | None = None,
        on_attempt: Callable[[dict], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.settings = settings
        self.http = http or requests.Session()
        self.on_attempt = on_attempt or _noop
        self.sleep = sleep
        self.rng = rng or random.Random()

    @classmethod
    def from_settings(cls, settings: Settings, **kw) -> RoadwayClient:
        return cls(settings.roadway_api_url, settings.roadway_api_key, settings, **kw)

    # -- endpoints -----------------------------------------------------------

    def get_reservation(self, reservation_id: str) -> dict:
        return self._request("GET", f"/reservations/{reservation_id}")

    def get_availability(
        self, location: str, vehicle_type: str, start_date: str, end_date: str
    ) -> dict:
        return self._request(
            "GET",
            "/availability",
            params={
                "location": location,
                "vehicle_type": vehicle_type,
                "start_date": start_date,
                "end_date": end_date,
            },
            read_timeout=self.settings.http_availability_read_timeout_s,
        )

    def quote_extension(self, reservation_id: str, new_return_datetime: str) -> dict:
        # /quote has no side effects, so it is retried like a read.
        return self._request(
            "POST",
            f"/reservations/{reservation_id}/quote",
            json={"change_type": "extend", "new_return_datetime": new_return_datetime},
            safe=True,
        )

    def extend(
        self,
        reservation_id: str,
        *,
        email: str,
        new_return_datetime: str,
        payment: dict | None,
        idempotency_key: str | None,
    ) -> dict:
        body: dict = {"new_return_datetime": new_return_datetime, "email": email}
        if payment is not None:
            body["payment"] = payment
        return self._request(
            "POST",
            f"/reservations/{reservation_id}/extend",
            json=body,
            idempotency_key=idempotency_key,
        )

    # -- transport -----------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        s = self.settings
        delay = min(s.http_backoff_cap_s, s.http_backoff_base_s * 2 ** (attempt - 1))
        return min(s.http_backoff_cap_s, delay * self.rng.uniform(0.8, 1.2))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        idempotency_key: str | None = None,
        safe: bool = False,
        read_timeout: float | None = None,
    ) -> dict:
        s = self.settings
        headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        timeout = (s.http_connect_timeout_s, read_timeout or s.http_read_timeout_s)
        retry_allowed = method == "GET" or safe or bool(idempotency_key)
        max_attempts = s.http_max_attempts if retry_allowed else 1
        url = f"{self.base_url}{path}"

        for attempt in range(1, max_attempts + 1):
            started = time.monotonic()
            error: RoadwayAPIError | None = None
            status: int | None = None
            result: dict = {}
            try:
                resp = self.http.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                # Any transport-level failure (timeout, connection, broken or
                # undecodable body): the outcome is unknown, so retry like a
                # timeout, reusing the same idempotency key.
                error = RoadwayAPIError(
                    None, "TRANSPORT_ERROR", type(exc).__name__, retryable=True
                )
            else:
                status = resp.status_code
                if 200 <= status < 300:
                    try:
                        result = resp.json()
                    except ValueError:
                        error = RoadwayAPIError(
                            status, f"HTTP_{status}", "non-JSON success body"
                        )
                else:
                    error = _error_from_response(resp)

            try:
                self.on_attempt(
                    {
                        "method": method,
                        "path": path,
                        "status": status,
                        "error_code": error.code if error else None,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "attempt": attempt,
                        "idempotency_key": idempotency_key,
                    }
                )
            except Exception:  # noqa: BLE001, S110 - logging never changes an API outcome
                pass

            if error is None:
                return result
            if not error.retryable or attempt == max_attempts:
                raise error
            self.sleep(self._backoff(attempt))

        raise AssertionError("unreachable")  # pragma: no cover


if __name__ == "__main__":
    client = RoadwayClient.from_settings(load_settings())
    reservation = client.get_reservation("RWR-29471835")
    print("Connected. Sample reservation:")
    print(f"  {reservation['customer_name']} — {reservation['vehicle']['description']}")
    print(
        f"  returns {reservation['dates']['current_return_datetime']}"
        f" at {reservation['return_location']['code']}"
    )

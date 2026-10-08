"""A small async client for the FoxESS Cloud Open API: only the reads the forecast needs.

Requests are signed the way the Open API expects (an MD5 of the path, the key and a millisecond
timestamp joined by a literal backslash-r-backslash-n, not a CRLF), spaced at least min_interval_s
apart because the API refuses bursts, and every answer whose errno is not 0 becomes a FoxessError.
Nothing here writes to the inverter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aiohttp import ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.foxesscloud.com"
_TIMEOUT = ClientTimeout(total=55)
# Errnos the API answers when it is being called too often; worth one patient retry.
_RATE_LIMITED = {40400, 41200, 41201, 41202, 41203}
_RATE_LIMIT_BACKOFF_S = 30.0
# A per-hour energy above this is a corrupted counter, not a reading (no residential string makes it).
_IMPLAUSIBLE_KWH = 1000.0
_HISTORY_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:.*?([+-])(\d{2}):?(\d{2}))?\s*$")


class FoxessError(RuntimeError):
    """The Open API refused or failed a request."""

    def __init__(self, path: str, errno: Optional[int], message: str, *, fatal: bool = False) -> None:
        super().__init__(f"FoxESS {path}: {message}" + (f" (errno {errno})" if errno is not None else ""))
        self.path = path
        self.errno = errno
        # Every further call would fail the same way (a refused key, no usable inverter): stop asking.
        self.fatal = fatal


def signature(path: str, token: str, timestamp_ms: str) -> str:
    """The Open API request signature. The separator is the four characters \\r\\n, as the API defines it."""
    return hashlib.md5(f"{path}\\r\\n{token}\\r\\n{timestamp_ms}".encode("utf-8")).hexdigest()


def parse_history_time(value: str, tz: tzinfo) -> Optional[datetime]:
    """A history sample's time ("2026-10-08 13:05:00 AEDT+1100"). The offset is used when the API
    gives one; otherwise the time is read as local to the installation."""
    match = _HISTORY_TIME.match(str(value or "").strip())
    if not match:
        return None
    naive = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    if match.group(2):
        sign = 1 if match.group(2) == "+" else -1
        offset = timedelta(hours=int(match.group(3)), minutes=int(match.group(4))) * sign
        return naive.replace(tzinfo=timezone(offset))
    return naive.replace(tzinfo=tz)


def clean_energy(value: Any) -> Optional[float]:
    """One hourly energy figure, or None when it is missing or cannot be real."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f < 0 or f > _IMPLAUSIBLE_KWH:
        return None
    return f


class FoxessClient:
    def __init__(
        self,
        session: ClientSession,
        api_key: str,
        *,
        device_sn: str = "",
        tz_name: str = "UTC",
        min_interval_s: float = 2.0,
        base_url: str = BASE_URL,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._device_sn = device_sn
        self._tz_name = tz_name
        self._min_interval_s = min_interval_s
        self._base_url = base_url.rstrip("/")
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        # Calls made since start, reported in the status so a quota problem is visible.
        self.calls = 0

    async def _request(self, method: str, path: str, *, body: Any = None, params: Any = None) -> Any:
        for attempt in range(2):
            async with self._lock:
                wait = self._last_call + self._min_interval_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                timestamp = str(int(time.time() * 1000))
                headers = {
                    "token": self._api_key,
                    "timestamp": timestamp,
                    "signature": signature(path, self._api_key, timestamp),
                    "lang": "en",
                    "timezone": self._tz_name,
                    "Content-Type": "application/json",
                }
                try:
                    async with self._session.request(
                        method,
                        self._base_url + path,
                        headers=headers,
                        data=json.dumps(body) if body is not None else None,
                        params=params,
                        timeout=_TIMEOUT,
                    ) as resp:
                        text = await resp.text()
                        status = resp.status
                finally:
                    self._last_call = time.monotonic()
                    self.calls += 1
            if status in (401, 403):
                raise FoxessError(
                    path, None, f"HTTP {status}, the API key was refused (check [foxess] api_key)", fatal=True
                )
            if status != 200:
                raise FoxessError(path, None, f"HTTP {status}")
            try:
                payload = json.loads(text)
            except ValueError:
                raise FoxessError(path, None, "the answer is not JSON") from None
            errno = payload.get("errno")
            if errno in (0, None):
                return payload.get("result")
            if errno in _RATE_LIMITED and attempt == 0:
                _LOGGER.info(
                    "FoxESS asked to slow down on %s (errno %s), retrying in %ss", path, errno, _RATE_LIMIT_BACKOFF_S
                )
                await asyncio.sleep(_RATE_LIMIT_BACKOFF_S)
                continue
            raise FoxessError(path, errno, str(payload.get("msg") or "request refused"))
        raise FoxessError(path, None, "request refused")  # pragma: no cover - the loop always returns or raises

    async def device_sn(self) -> str:
        """The configured inverter, or the only one on the account."""
        if self._device_sn:
            return self._device_sn
        result = await self._request("POST", "/op/v0/device/list", body={"currentPage": 1, "pageSize": 100})
        devices = (result or {}).get("data") or []
        serials = [d.get("deviceSN") for d in devices if d.get("deviceSN")]
        if not serials:
            raise FoxessError("/op/v0/device/list", None, "no inverter on this account", fatal=True)
        if len(serials) > 1:
            raise FoxessError(
                "/op/v0/device/list",
                None,
                f"several inverters on this account, set [foxess] device_sn to one of {', '.join(serials)}",
                fatal=True,
            )
        self._device_sn = serials[0]
        return self._device_sn

    async def report_day(self, day: date, variables: Sequence[str]) -> Dict[str, List[Optional[float]]]:
        """Hourly energy (kWh) for one local day, per variable: index i is the hour starting at i:00."""
        sn = await self.device_sn()
        body = {
            "sn": sn,
            "dimension": "day",
            "variables": list(variables),
            "year": day.year,
            "month": day.month,
            "day": day.day,
        }
        result = await self._request("POST", "/op/v0/device/report/query", body=body)
        out: Dict[str, List[Optional[float]]] = {}
        for item in result or []:
            var = item.get("variable")
            if var:
                out[var] = [clean_energy(v) for v in item.get("values") or []]
        return out

    async def history(
        self, begin: datetime, end: datetime, variables: Sequence[str], tz: tzinfo
    ) -> Dict[str, List[Tuple[datetime, float]]]:
        """Raw samples (about every 5 minutes) per variable over [begin, end]; the API caps a call at a day."""
        sn = await self.device_sn()
        body = {
            "sn": sn,
            "variables": list(variables),
            "begin": int(begin.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
        }
        result = await self._request("POST", "/op/v0/device/history/query", body=body)
        out: Dict[str, List[Tuple[datetime, float]]] = {}
        datas = (result[0].get("datas") if result else None) or []
        for item in datas:
            samples = []
            for point in item.get("data") or []:
                t = parse_history_time(point.get("time"), tz)
                try:
                    v = float(point.get("value"))
                except (TypeError, ValueError):
                    continue
                if t is not None:
                    samples.append((t, v))
            out[item.get("variable")] = samples
        return out

    async def real(self, variables: Sequence[str]) -> Dict[str, Optional[float]]:
        """The latest real-time value per variable."""
        sn = await self.device_sn()
        result = await self._request(
            "POST", "/op/v1/device/real/query", body={"sns": [sn], "variables": list(variables)}
        )
        out: Dict[str, Optional[float]] = {}
        for device in result or []:
            for item in device.get("datas") or []:
                try:
                    out[item.get("variable")] = float(item.get("value"))
                except (TypeError, ValueError):
                    out[item.get("variable")] = None
        return out

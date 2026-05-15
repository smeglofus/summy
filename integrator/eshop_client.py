"""HTTP client for the e-shop API.

Responsibilities:
* Rate limit outbound requests (5 req/s by default).
* Retry on 429 — respect Retry-After header when present.
* Retry on transient network failures with exponential backoff.
* Retry on transient 5xx with exponential backoff.
* Raise EshopError on permanent failures.
"""
import email.utils
import logging
import time
from datetime import datetime, timezone
from typing import Callable

import requests
from django.conf import settings

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class EshopError(Exception):
    """Permanent API failure (max retries exceeded or non-retryable status)."""


class EshopClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        rate_limiter: RateLimiter | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.base_url = (base_url or settings.ESHOP_API_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.ESHOP_API_KEY
        self.rate_limiter = rate_limiter or RateLimiter(rate=settings.ESHOP_RATE_LIMIT_PER_SEC)
        self.max_retries = max_retries if max_retries is not None else settings.ESHOP_MAX_RETRIES
        self.timeout = timeout if timeout is not None else settings.ESHOP_REQUEST_TIMEOUT
        self._sleep = sleep_fn
        self.session = session or requests.Session()
        self.session.headers.update({
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        })

    def create_product(self, payload: dict) -> requests.Response:
        return self._request("POST", "/products/", json=payload)

    def update_product(self, sku: str, payload: dict) -> requests.Response:
        return self._request("PATCH", f"/products/{sku}/", json=payload)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        last_response: requests.Response | None = None
        last_error: requests.RequestException | None = None

        for attempt in range(self.max_retries + 1):
            self.rate_limiter.acquire()
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    wait = self._backoff(attempt)
                    logger.warning(
                        "Network error on %s %s (attempt %d/%d) - sleeping %.2fs: %s",
                        method, path, attempt + 1, self.max_retries + 1, wait, exc,
                    )
                    self._sleep(wait)
                    continue
                break

            last_response = response

            if response.status_code == 429:
                if attempt >= self.max_retries:
                    break
                wait = self._parse_retry_after(response) or self._backoff(attempt)
                logger.warning(
                    "Rate limited by e-shop API on %s %s (attempt %d/%d) - sleeping %.2fs",
                    method, path, attempt + 1, self.max_retries + 1, wait,
                )
                self._sleep(wait)
                continue

            if 500 <= response.status_code < 600 and attempt < self.max_retries:
                wait = self._backoff(attempt)
                logger.warning(
                    "Server error %d on %s %s (attempt %d/%d) - sleeping %.2fs",
                    response.status_code, method, path, attempt + 1, self.max_retries + 1, wait,
                )
                self._sleep(wait)
                continue

            if response.ok:
                return response

            raise EshopError(
                f"{method} {url} failed with {response.status_code}: {response.text[:200]}"
            )

        if last_response is not None:
            raise EshopError(
                f"{method} {url} exhausted retries (last status: {last_response.status_code})"
            )
        if last_error is not None:
            raise EshopError(
                f"{method} {url} exhausted retries after network error: {last_error}"
            ) from last_error
        raise EshopError(f"{method} {url} exhausted retries (no response)")

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> float | None:
        # RFC 7231 allows two forms: delta-seconds and HTTP-date.
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(float(raw), 0.0)
        except ValueError:
            pass
        try:
            retry_at = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if retry_at is None:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        now = datetime.now(retry_at.tzinfo)
        return max((retry_at - now).total_seconds(), 0.0)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2 ** attempt * 0.5, 30.0)

"""Lightweight CSRF protection helpers inspired by fastapi-csrf-protect."""
from __future__ import annotations

import base64
import hmac
import hashlib
import secrets
import time
from typing import Callable, Optional

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel

__all__ = [
    "CsrfProtect",
    "CsrfProtectException",
]


class CsrfProtectException(Exception):
    """Exception raised when CSRF validation fails."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


_ConfigLoader = Callable[[], BaseModel]
_CONFIG_LOADER: Optional[_ConfigLoader] = None


class CsrfProtect:
    """Simple token-based CSRF helper with signed cookies."""

    def __init__(self) -> None:
        if _CONFIG_LOADER is None:
            raise RuntimeError("CsrfProtect configuration has not been loaded")
        self._settings = _CONFIG_LOADER()
        self._secret = self._settings.secret_key.encode("utf-8")
        self._cookie_name = getattr(self._settings, "cookie_name", "csrftoken")
        self._header_name = getattr(self._settings, "header_name", "X-CSRF-Token")
        self._time_limit = int(getattr(self._settings, "time_limit", 3600))

    @classmethod
    def load_config(cls, func: _ConfigLoader) -> _ConfigLoader:
        global _CONFIG_LOADER
        _CONFIG_LOADER = func
        return func

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    @property
    def header_name(self) -> str:
        return self._header_name

    def _sign(self, payload: bytes) -> str:
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")

    def _encode(self, payload: bytes) -> str:
        return base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")

    def _decode(self, value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("utf-8"))

    def generate_csrf(self) -> str:
        timestamp = int(time.time()).to_bytes(8, "big")
        nonce = secrets.token_bytes(16)
        payload = timestamp + nonce
        return f"{self._encode(payload)}.{self._sign(payload)}"

    def _parse(self, token: str) -> bytes:
        try:
            payload_value, signature = token.split(".")
        except ValueError as exc:  # pragma: no cover - defensive
            raise CsrfProtectException(403, "Invalid CSRF token format") from exc
        payload = self._decode(payload_value)
        expected_signature = self._sign(payload)
        if not secrets.compare_digest(signature, expected_signature):
            raise CsrfProtectException(403, "Invalid CSRF token signature")
        if len(payload) < 8:
            raise CsrfProtectException(403, "Invalid CSRF token payload")
        timestamp = int.from_bytes(payload[:8], "big")
        if time.time() - timestamp > self._time_limit:
            raise CsrfProtectException(403, "CSRF token expired")
        return payload

    def validate_token(self, token: str) -> None:
        self._parse(token)

    def validate_csrf(self, submitted_token: Optional[str], cookie_token: Optional[str]) -> None:
        if not submitted_token or not cookie_token:
            raise CsrfProtectException(403, "CSRF token missing")
        if not secrets.compare_digest(submitted_token, cookie_token):
            raise CsrfProtectException(403, "CSRF token mismatch")
        self._parse(cookie_token)

    def set_csrf_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            self._cookie_name,
            token,
            httponly=True,
            secure=bool(getattr(self._settings, "cookie_secure", False)),
            samesite=getattr(self._settings, "cookie_samesite", "Lax"),
            domain=getattr(self._settings, "cookie_domain", None),
        )

    def ensure_cookie(self, request: Request, response: Response) -> str:
        existing = request.cookies.get(self._cookie_name)
        if existing:
            try:
                self.validate_token(existing)
                return existing
            except CsrfProtectException:
                pass
        token = self.generate_csrf()
        self.set_csrf_cookie(response, token)
        return token

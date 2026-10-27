from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid


class JournalError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid.uuid4().hex


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def number(value, positive=False) -> Decimal:
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError()
        result = Decimal(value)
        if (not result.is_finite() or result < 0 or (positive and result == 0)
                or result > Decimal("1e24") or not -18 <= result.as_tuple().exponent <= 24):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise JournalError("수량과 가격은 유효한 양의 십진 문자열이어야 합니다.") from None


def decimal_text(value: Decimal) -> str:
    return format(value, "f")

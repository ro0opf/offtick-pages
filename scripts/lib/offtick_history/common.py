"""표준 라이브러리 기반 파일·환경설정·정밀 수치 도구."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


class HistoryError(Exception):
    pass


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path: Path, value) -> None:
    atomic_write(path, (json_text(value) + "\n").encode())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    atomic_write(path, "".join(json_text(row) + "\n" for row in rows).encode())


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise HistoryError("환경 파일의 KEY=VALUE 형식을 확인하세요.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise HistoryError("환경 파일의 따옴표를 확인하세요.")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def date_arg(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from None


def number(value, *, positive: bool = False) -> str:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise HistoryError("필수 수치가 없습니다.")
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation:
        raise HistoryError("유효한 십진수가 아닙니다.") from None
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise HistoryError("유효한 양수 수치가 아닙니다.")
    # 지수 표기 등 비정상 입력의 과도한 메모리 사용을 차단합니다.
    if len(parsed.as_tuple().digits) > 100 or abs(parsed.as_tuple().exponent) > 100:
        raise HistoryError("지원하는 수치 정밀도 범위를 벗어났습니다.")
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if parsed.is_zero() else text


def product(first: str, second: str) -> str:
    with localcontext() as context:
        context.prec = len(Decimal(first).as_tuple().digits) + len(Decimal(second).as_tuple().digits) + 2
        return format(Decimal(first) * Decimal(second), "f")


def exact_sum(values: list[str]) -> str:
    parsed = [Decimal(v) for v in values]
    if not parsed:
        return "0"
    with localcontext() as context:
        context.prec = max(64, max(v.adjusted() for v in parsed) - min(v.as_tuple().exponent for v in parsed)
                           + len(str(len(parsed))) + 3)
        return format(sum(parsed, Decimal(0)), "f")


@contextmanager
def output_lock(root: Path):
    import fcntl
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / ".collector.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HistoryError("같은 데이터 폴더에서 다른 작업이 실행 중입니다.") from None
        yield
    finally:
        os.close(fd)

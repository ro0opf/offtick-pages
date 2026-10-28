"""명시적으로 등록한 읽기 전용 키움 REST API 클라이언트."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import time
from urllib import error, request

from .common import HistoryError, digest, json_text, write_json
from .schema import API_ID, API_PATH, SEOUL

BASE_URL = "https://api.kiwoom.com"
TOKEN_PATH = "/oauth2/token"
ACCOUNT_API_ID = "ka00001"
AUTH_CODES = {8005, 8031, 8103}
ROWS_KEY = "acnt_ord_cntr_prst_array"
READ_PATHS = {
    ACCOUNT_API_ID: API_PATH, API_ID: API_PATH,
    "kt00018": API_PATH,  # 계좌 전체의 추정예탁자산·평가금액
    "ka10032": "/api/dostk/rkinfo",  # 통합 거래대금 순위
    "ka10099": "/api/dostk/stkinfo",  # 종목 유형 확인
    "ka10080": "/api/dostk/chart",   # 종목 1분봉
    "ka20005": "/api/dostk/chart",   # 지수 1분봉
    "ka10086": "/api/dostk/mrkcond", # 종목 일별 거래대금(백만원)
    "ka20006": "/api/dostk/chart",   # 지수 일별 거래대금(백만원)
    "usa20540": "/api/us/rkinfo",  # 미국 당일 거래대금 순위
    "usa20960": "/api/us/rkinfo",  # 미국 전일 거래대금 순위
    "usa10100": "/api/us/stkinfo", # 미국 종목·ETF 여부 확인
    "usa20590": "/api/us/mrkcond", # 미국 일별 OHLC·거래량·거래대금 원문
}


@dataclass
class Reply:
    status: int
    headers: dict
    content: bytes

    def __post_init__(self):
        self.headers = {k.lower(): v for k, v in self.headers.items()}

    def body(self) -> dict:
        try:
            data = json.loads(self.content, parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise HistoryError("키움 응답이 유효한 JSON이 아닙니다.") from None
        if not isinstance(data, dict):
            raise HistoryError("키움 응답은 JSON 객체여야 합니다.")
        return data


def return_code(body: dict) -> int:
    value = body.get("return_code")
    if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
        raise HistoryError("키움 응답에 유효한 return_code가 없습니다.")
    return int(value)


def embedded_code(body: dict) -> int:
    code = return_code(body)
    if code == 3:
        found = re.search(r"\[(\d{3,5}):|CODE=(\d{3,5})", str(body.get("return_msg") or ""))
        if found:
            return int(found.group(1) or found.group(2))
    return code


def checked_body(reply: Reply) -> dict:
    if reply.status != 200:
        raise HistoryError(f"키움 조회 실패(HTTP {reply.status}).")
    body = reply.body()
    code = return_code(body)
    if code != 0:
        # 자유 형식 오류 메시지는 키·계좌번호를 포함할 수 있으므로 로그에 복사하지 않습니다.
        raise HistoryError(f"키움 업무 오류(return_code={code}). 원본 응답을 확인하세요.")
    return body


def checked_rows(reply: Reply) -> list[dict]:
    body = checked_body(reply)
    rows = body.get(ROWS_KEY)
    if rows is None:
        message = re.sub(r"\s+", "", str(body.get("return_msg") or ""))
        if re.search(r"(내역|자료|데이터|결과)(이|가)?(없|존재하지)", message):
            return []
        raise HistoryError("체결 목록이 없습니다. 성공한 0건 조회인지 확인할 수 없습니다.")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HistoryError("체결 목록 형식이 올바르지 않습니다.")
    return rows


def next_cursor(reply: Reply, seen: set[str]) -> tuple[str, str] | None:
    flag = str(reply.headers.get("cont-yn", "")).strip().upper()
    key = str(reply.headers.get("next-key", ""))
    if flag not in {"", "N", "Y"}:
        raise HistoryError("알 수 없는 연속조회 상태입니다.")
    if flag == "N":
        return None
    if flag != "Y":
        if key.strip():
            raise HistoryError("연속조회 키와 완료 상태가 일치하지 않습니다.")
        return None
    if not key.strip() or key in seen:
        raise HistoryError("연속조회 키가 없거나 반복됩니다. 조회를 완료로 처리하지 않았습니다.")
    seen.add(key)
    return flag, key


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    def __init__(self):
        self.opener = request.build_opener(NoRedirect())

    def __call__(self, path: str, payload: bytes, headers: dict) -> Reply:
        if path != TOKEN_PATH and READ_PATHS.get(headers.get("api-id")) != path:
            raise HistoryError("등록된 읽기 전용 조회 API만 허용합니다.")
        if path == TOKEN_PATH:
            params = json.loads(payload)
            if params.get("grant_type") != "client_credentials" or headers.get("api-id"):
                raise HistoryError("허용되지 않은 인증 요청입니다.")
        req = request.Request(BASE_URL + path, data=payload, headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=30) as response:
                return Reply(response.status, dict(response.headers.items()), response.read())
        except error.HTTPError as exc:
            return Reply(exc.code, dict(exc.headers.items()), exc.read())
        except (error.URLError, OSError, TimeoutError):
            raise HistoryError("키움 API 연결에 실패했습니다. 접속 조건을 확인하세요.") from None


class Client:
    def __init__(self, key: str, secret: str, root: Path, transport=None, sleep=time.sleep, interval=0.35):
        if not key or not secret:
            raise HistoryError("KIWOOM_APP_KEY와 KIWOOM_APP_SECRET을 .env에 입력하세요.")
        self.key, self.secret = key, secret
        self.transport = transport or HttpTransport()
        self.data_mode = "demo" if getattr(self.transport, "is_demo", False) else "live"
        self.cache_path = root / ".private/tokens" / (digest([BASE_URL, key, secret])[:24] + ".json")
        self.sleep, self.interval, self.last_request = sleep, interval, 0.0
        self.token, self.expires_at = "", 0.0

    def send(self, path: str, body: dict, headers: dict) -> Reply:
        delay = self.interval - (time.monotonic() - self.last_request)
        if delay > 0:
            self.sleep(delay)
        self.last_request = time.monotonic()
        return self.transport(path, json_text(body).encode(), headers)

    def token_value(self, refresh: bool = False) -> str:
        now = time.time()
        if not refresh:
            if self.token and self.expires_at > now + 60:
                return self.token
            try:
                cached = json.loads(self.cache_path.read_text())
                if isinstance(cached["token"], str) and cached["token"] and float(cached["expires_at"]) > now + 60:
                    self.token, self.expires_at = cached["token"], float(cached["expires_at"])
                    return self.token
            except (ValueError, OSError, KeyError, TypeError):
                pass
        reply = self.send(TOKEN_PATH, {"grant_type": "client_credentials", "appkey": self.key, "secretkey": self.secret},
                          {"Content-Type": "application/json;charset=UTF-8"})
        body = checked_body(reply)
        token = body.get("token")
        try:
            expiry = datetime.strptime(str(body["expires_dt"]), "%Y%m%d%H%M%S").replace(tzinfo=SEOUL).timestamp()
        except (KeyError, ValueError):
            raise HistoryError("토큰 만료시각을 확인할 수 없습니다.") from None
        if not isinstance(token, str) or not token or expiry <= now + 60:
            raise HistoryError("토큰 값·유효기간이 올바르지 않습니다.")
        self.token, self.expires_at = token, expiry
        write_json(self.cache_path, {"token": token, "expires_at": expiry})
        return token

    def post(self, api_id: str, body: dict, flag: str = "N", cursor: str = "") -> Reply:
        if api_id not in READ_PATHS:
            raise HistoryError("등록된 읽기 전용 조회 API만 호출할 수 있습니다.")
        refreshed = False
        for attempt in range(4):
            headers = {"Content-Type": "application/json;charset=UTF-8", "authorization": "Bearer " + self.token_value(),
                       "api-id": api_id, "cont-yn": flag, "next-key": cursor}
            reply = self.send(READ_PATHS[api_id], body, headers)
            try:
                code = embedded_code(reply.body())
            except HistoryError:
                code = None
            if reply.status == 401 or code in AUTH_CODES:
                if refreshed or attempt == 3:
                    raise HistoryError("토큰 갱신 뒤에도 키움 인증에 실패했습니다.")
                self.token_value(refresh=True)
                refreshed = True
                continue
            if reply.status == 429 or reply.status in {500, 502, 503, 504} or code == 1700:
                if attempt == 3:
                    raise HistoryError("키움 일시 오류의 재시도 한도를 초과했습니다.")
                self.sleep(min(2 ** attempt, 8))
                continue
            return reply
        raise HistoryError("키움 조회의 재시도 한도를 초과했습니다.")

    def scrub(self, content: bytes) -> bytes:
        for value in (self.key, self.secret, self.token):
            if value:
                content = content.replace(value.encode(), b"[REDACTED]")
        return content

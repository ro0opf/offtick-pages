"""키움 마감 순위·종목/지수 1분봉의 로컬 수집과 불변 snapshot 조회."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from .collector import verify_scope
from .common import HistoryError, atomic_write, date_arg, json_text, load_env, number, output_lock, write_json
from .kiwoom import Client, READ_PATHS, checked_body, next_cursor
from .schema import SEOUL
from .market_storage import read_market_bytes, write_market_bytes

POLICY = "closing_top50_common_preferred_v1"
INDEX_NAMES = {"001": "코스피", "101": "코스닥"}
SYMBOL = re.compile(r"[0-9A-Z]{6}")


def symbol_code(value):
    text = str(value or "").strip()
    if text.endswith("_AL"):
        text = text[:-3]
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    if not SYMBOL.fullmatch(text):
        raise HistoryError("시장 자료의 종목코드를 확인하세요.")
    return text


def normalize_bar(raw: dict, kind: str, symbol: str) -> dict:
    stamp = str(raw.get("cntr_tm", "")).strip()
    if not re.fullmatch(r"\d{14}", stamp):
        raise HistoryError("분봉의 YYYYMMDDHHmmss 시각이 없습니다.")
    try:
        instant = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=SEOUL)
    except ValueError:
        raise HistoryError("분봉 시각이 올바르지 않습니다.") from None
    if instant.second != 0:
        raise HistoryError("1분봉 시각의 초가 00이 아닙니다. 경계 해석 확인이 필요합니다.")
    prices = {}
    for target, field in (("open", "open_pric"), ("high", "high_pric"), ("low", "low_pric"), ("close", "cur_prc")):
        value = Decimal(number(raw.get(field))).copy_abs()  # 부호는 전일 대비 방향
        if value <= 0:
            raise HistoryError("분봉 가격은 양수여야 합니다.")
        parts = value.as_tuple()
        prices[target] = number(Decimal((parts.sign, parts.digits, parts.exponent - 2)) if kind == "index" else value)
    if not (Decimal(prices["low"]) <= min(Decimal(prices["open"]), Decimal(prices["close"]))
            <= max(Decimal(prices["open"]), Decimal(prices["close"])) <= Decimal(prices["high"])):
        raise HistoryError("분봉 고가·저가 범위가 올바르지 않습니다.")
    volume = number(raw.get("trde_qty"))
    if Decimal(volume) < 0:
        raise HistoryError("분봉 거래량은 음수일 수 없습니다.")
    return {"kind": kind, "symbol": symbol, "at": instant.isoformat(), "trade_date": instant.date().isoformat(),
            **prices, "volume": volume, "price_unit": "index_point" if kind == "index" else "KRW",
            "market_scope": "KRX_index" if kind == "index" else "KRX_NXT_integrated",
            "interval_seconds": 60, "timestamp_basis": "provider_cntr_tm_boundary_unverified",
            "turnover": None, "vwap": None}


class MarketRun:
    def __init__(self, client, root, day, max_pages):
        self.client, self.day, self.max_pages = client, day, max_pages
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.root = root / "market"
        self.path = self.root / "runs" / self.id
        self.manifest = {"schema_version": 1, "broker": "kiwoom", "data_mode": client.data_mode,
            "run_id": self.id, "trade_date": day.isoformat(), "status": "running", "components": {},
            "started_at": datetime.now(SEOUL).isoformat(), "snapshot_storage": "gzip_lossless"}
        self.save()

    def save(self):
        write_json(self.path / "manifest.json", self.manifest)

    def pages(self, api_id, query, component, rows_key):
        flag, cursor, seen = "N", "", set()
        for page in range(1, self.max_pages + 1):
            reply = self.client.post(api_id, query, flag, cursor)
            path = self.path / "raw" / component / f"page-{page:05d}.json"
            content = self.client.scrub(reply.content)
            write_market_bytes(path, content)
            write_json(path.with_suffix(".meta.json"), {"api_id": api_id, "endpoint": READ_PATHS[api_id],
                "request": query, "http_status": reply.status,
                "sha256": hashlib.sha256(content).hexdigest(),
                "credential_redacted": content != reply.content,
                "pagination_headers": {k: v for k, v in reply.headers.items() if k in {"api-id", "cont-yn", "next-key"}}})
            if reply.headers.get("api-id", api_id) != api_id:
                raise HistoryError("시장 응답 API ID가 요청과 다릅니다.")
            body = checked_body(reply)
            rows = body.get(rows_key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise HistoryError("시장 응답 목록 형식이 올바르지 않습니다.")
            yield body, rows, {"file": str(path.relative_to(self.path)), "sha256": hashlib.sha256(content).hexdigest()}
            following = next_cursor(reply, seen)
            if following is None:
                return
            flag, cursor = following
        raise HistoryError("시장 조회의 최대 페이지 수를 초과했습니다.")

    def bars(self, kind, symbol):
        api_id, rows_key = ("ka20005", "inds_min_pole_qry") if kind == "index" else ("ka10080", "stk_min_pole_chart_qry")
        query = {"inds_cd" if kind == "index" else "stk_cd": symbol if kind == "index" else symbol + "_AL",
                 "tic_scope": "1", "base_dt": self.day.strftime("%Y%m%d")}
        if kind == "stock":
            query["upd_stkpc_tp"] = "0"  # 실제 체결 가격과 같은 비수정 가격
        records = {}
        previous_time = None
        for body, rows, source in self.pages(api_id, query, kind + "-" + symbol, rows_key):
            returned = body.get("inds_cd" if kind == "index" else "stk_cd")
            if (str(returned).strip() != symbol if kind == "index" else symbol_code(returned) != symbol):
                raise HistoryError("분봉 응답의 종목·지수가 요청과 다릅니다.")
            dates = []
            for i, raw in enumerate(rows, 1):
                bar = normalize_bar(raw, kind, symbol)
                if previous_time is not None and bar["at"] > previous_time:
                    raise HistoryError("분봉이 최신순으로 반환되지 않았습니다. 조회 범위를 확정할 수 없습니다.")
                previous_time = bar["at"]
                dates.append(bar["trade_date"])
                if bar["trade_date"] > self.day.isoformat():
                    raise HistoryError("기준일 이후 분봉이 반환됐습니다. 기존 자료를 유지합니다.")
                if bar["trade_date"] != self.day.isoformat():
                    continue
                previous = records.get(bar["at"])
                if previous and {k: v for k, v in previous.items() if k != "sources"} != bar:
                    raise HistoryError("같은 분봉 시각의 내용이 충돌합니다.")
                if previous:
                    previous["sources"].append({**source, "row": i})
                else:
                    records[bar["at"]] = {**bar, "sources": [{**source, "row": i}]}
            # 과거 페이지가 섞인 경우에도 목록 전체가 목표일보다 앞서야 종료한다.
            if dates and max(dates) < self.day.isoformat():
                break
        if not records:
            raise HistoryError("해당 날짜 분봉이 없습니다. 휴장·조회 범위·수집 시각을 확인하세요.")
        return {"kind": kind, "symbol": symbol, "trade_date": self.day.isoformat(),
                "data_mode": self.client.data_mode, "bars": sorted(records.values(), key=lambda r: r["at"]),
                "session_completeness": "unverified", "minute_turnover_verified": False}

    def selection(self, count=50):
        universe, excluded = {}, set()
        for market in ("0", "10", "8", "60", "70", "90", "3"):
            for _, rows, _ in self.pages("ka10099", {"mrkt_tp": market}, "universe-" + market, "list"):
                for row in rows:
                    code = symbol_code(row.get("code"))
                    if market not in {"0", "10"}:
                        excluded.add(code)
                    elif str(row.get("marketCode")) in {"0", "10"} and "관리" not in str(row.get("state", "")):
                        if code in universe and universe[code] != row:
                            raise HistoryError("종목 유형 정보가 충돌합니다.")
                        universe[code] = row
        candidates = {}
        for _, rows, source in self.pages("ka10032", {"mrkt_tp": "000", "mang_stk_incls": "0", "stex_tp": "3"}, "ranking", "trde_prica_upper"):
            for i, row in enumerate(rows, 1):
                code = symbol_code(row.get("stk_cd"))
                if code not in universe or code in excluded:
                    continue
                amount = number(row.get("trde_prica"))
                rank = number(row.get("now_rank"), positive=True)
                if Decimal(amount) < 0 or not Decimal(rank) == Decimal(rank).to_integral_value():
                    raise HistoryError("순위·거래대금 형식을 확인하세요.")
                candidate = {"symbol": code, "name": universe[code].get("name"),
                    "provider_rank": int(rank), "turnover_million_krw": amount,
                    "market_code": universe[code]["marketCode"], "source": {**source, "row": i}}
                if code in candidates and {k: v for k, v in candidates[code].items() if k != "source"} != {k: v for k, v in candidate.items() if k != "source"}:
                    raise HistoryError("연속조회 중 순위가 변경됐습니다. 다시 수집하세요.")
                candidates[code] = candidate
            if len(candidates) >= count:
                break
        selected = sorted(candidates.values(), key=lambda r: (r["provider_rank"], r["symbol"]))[:count]
        if len(selected) < count:
            raise HistoryError("필터 후 상위 50종목을 확보하지 못했습니다.")
        if len({r["provider_rank"] for r in selected}) != count:
            raise HistoryError("서로 다른 종목의 순위가 중복됩니다.")
        amounts = [Decimal(row["turnover_million_krw"]) for row in selected]
        if amounts != sorted(amounts, reverse=True):
            raise HistoryError("순위와 거래대금의 정렬이 일치하지 않습니다.")
        return {"status": "captured_after_close", "trade_date": self.day.isoformat(),
            "observed_at": datetime.now(SEOUL).isoformat(), "policy": POLICY, "market_scope": "KRX_NXT_integrated",
            "turnover_unit": "million_KRW", "provider_session_scope_verified": False,
            "intraday_rank_available": False, "items": [dict(row, rank=i) for i, row in enumerate(selected, 1)]}

    def publish(self, component, value):
        path = self.path / (component + ".json")
        content = (json_text(value) + "\n").encode()
        write_market_bytes(path, content)
        return {"run_id": self.id, "file": str(path.relative_to(self.root)),
                "sha256": hashlib.sha256(content).hexdigest()}


def read_component(root: Path, ref: dict):
    if not isinstance(ref, dict):
        raise HistoryError("시장 snapshot 참조는 JSON 객체여야 합니다.")
    relative = ref.get("file", "")
    if not re.fullmatch(r"runs/[0-9TZ]+-[a-f0-9]{8}/(?:index-\d{3}|stock-[0-9A-Z]{6}|selection|turnover)\.json", relative):
        raise HistoryError("시장 snapshot 참조가 올바르지 않습니다.")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise HistoryError("허용되지 않은 시장 자료 경로입니다.")
    content = read_market_bytes(path)
    if hashlib.sha256(content).hexdigest() != ref.get("sha256"):
        raise HistoryError("시장 snapshot 해시가 일치하지 않습니다.")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise HistoryError("시장 snapshot은 JSON 객체여야 합니다.")
    return value


def read_day(root: Path, day: date):
    path = root / "days" / (day.isoformat() + ".json")
    if not path.is_file():
        return {"trade_date": day.isoformat(), "selection_status": "not_collected", "series": {}}
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("trade_date") != day.isoformat()
            or not isinstance(value.get("series"), dict) or not isinstance(value.get("component_statuses", {}), dict)):
        raise HistoryError("시장 날짜별 참조 형식이 올바르지 않습니다.")
    if (any(not isinstance(ref, dict) for ref in value["series"].values())
            or any(not isinstance(status, dict) for status in value.get("component_statuses", {}).values())
            or "selection" in value and not isinstance(value["selection"], dict)
            or "turnover" in value and not isinstance(value["turnover"], dict)):
        raise HistoryError("시장 컴포넌트 참조 형식이 올바르지 않습니다.")
    return value


def collect_market(client: Client, day: date, root: Path, *, now=None, max_pages=200, progress=None):
    now = now or datetime.now(SEOUL)
    now = now.astimezone(SEOUL)
    if day > now.date() or max_pages < 1:
        raise HistoryError("시장 조회일·최대 페이지 수를 확인하세요.")
    verify_scope(root, client.data_mode)
    write_json(root / "profile.json", {"broker": "kiwoom", "data_mode": client.data_mode, "schema_version": 1})
    run = MarketRun(client, root, day, max_pages)
    previous = read_day(run.root, day)
    current = {**previous, "trade_date": day.isoformat(), "data_mode": client.data_mode,
               "series": dict(previous.get("series", {})), "last_attempt": run.id}
    errors = run.manifest["components"]

    def attempt(component, action):
        try:
            value = action()
            reference = run.publish(component, value)
            errors[component] = {"status": "complete", "reference": reference,
                                 "record_count": len(value.get("bars", value.get("items", [])))}
            return value, reference
        except HistoryError as error:
            # HistoryError는 자유 형식 응답 원문을 포함하지 않는 내부 진단이다.
            errors[component] = {"status": "failed", "reason": str(error)}
            return None, None
        except (OSError, ValueError, TypeError, KeyError):
            errors[component] = {"status": "failed", "reason": "파일·응답 형식을 확인하세요. 해당 run의 원본을 보존했습니다."}
            return None, None
        finally:
            run.save()
            if progress:
                progress(component, errors.get(component, {"status": "interrupted"}))

    indices = {}
    try:
        for code in INDEX_NAMES:
            component = "index-" + code
            value, ref = attempt(component, lambda code=code: run.bars("index", code))
            if ref:
                current["series"][component] = ref
                indices[code] = value
        same_closed_day = day == now.date() and day.weekday() < 5 and (now.hour, now.minute) >= (20, 20)
        if same_closed_day and len(indices) == 2:
            selection, ref = attempt("selection", run.selection)
            if ref:
                current["selection"] = ref
                current["selection_status"] = selection["status"]
        elif "selection" not in current:
            current["selection_status"] = ("session_unconfirmed" if same_closed_day else "pending_close") if day == now.date() else "unavailable_historical"
        if same_closed_day and "selection" in errors and errors["selection"]["status"] == "failed" and "selection" not in current:
            current["selection_status"] = "collection_failed"
        if "selection" in current:
            selection = read_component(run.root, current["selection"])
            for row in selection["items"]:
                code = row["symbol"]
                component = "stock-" + code
                _, ref = attempt(component, lambda code=code: run.bars("stock", code))
                if ref:
                    current["series"][component] = ref
        statuses = [component["status"] for component in errors.values()]
        run.manifest["status"] = "partial" if "failed" in statuses else "complete"
        run.manifest["completed_at"] = datetime.now(SEOUL).isoformat()
        run.manifest["normalized_bytes"] = sum(p.stat().st_size for p in run.path.glob("*.json*") if p.name != "manifest.json")
        run.manifest["raw_bytes"] = sum(p.stat().st_size for p in (run.path / "raw").rglob("*.json*"))
        current["last_attempt_status"] = run.manifest["status"]
        current["component_statuses"] = {key: {k: v for k, v in value.items() if k != "reference"} for key, value in errors.items()}
        write_json(run.root / "days" / (day.isoformat() + ".json"), current)
        return {"run_id": run.id, "status": run.manifest["status"], "trade_date": day.isoformat(),
                "selection_status": current.get("selection_status", "failed"),
                "components": errors, "directory": str(run.path)}
    except BaseException:
        run.manifest["status"] = "interrupted_or_failed"
        raise
    finally:
        run.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date_arg, required=True)
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--max-pages", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        load_env(args.env_file)
        client = Client(os.environ.get("KIWOOM_APP_KEY", "").strip(), os.environ.get("KIWOOM_APP_SECRET", "").strip(), args.output)
        with output_lock(args.output):
            result = collect_market(client, args.date, args.output, max_pages=args.max_pages)
        print(json_text(result))
        return 0 if result["status"] == "complete" else 1
    except (HistoryError, OSError, ValueError):
        print("시장 수집 실패. 키·허용 IP·조회일·원본 응답을 확인하세요. 이전 자료는 유지됩니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

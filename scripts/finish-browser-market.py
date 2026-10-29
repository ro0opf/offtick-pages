"""Complete public browser quotes with indices; never receives broker or GitHub keys."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/trade-history/src"), str(ROOT / "apps/api/src"), str(Path(__file__).parent / "lib")]
from offtick_history.common import json_text, number, product, write_json
from offtick_history.market_storage import read_market_bytes
from offtick_history.us_calendar import latest_completed, NEW_YORK
from offtick_history.us_market import index_daily, public_stock_daily
from offtick_journal.public_market import export_market, SELECTION_FIELDS

MAX_BYTES = 64 * 1024 * 1024
REQUEST = re.compile(r"(kr|us)-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")


class CollectionFailure(Exception):
    def __init__(self, code, symbol):
        self.code, self.symbol = code, symbol


def pick(value, fields):
    return {key: value[key] for key in fields if key in value}


def price_check(row):
    values = {key: number(row[key], positive=True) for key in ("open", "high", "low", "close")}
    if not (Decimal(values["low"]) <= min(Decimal(values["open"]), Decimal(values["close"]))
            <= max(Decimal(values["open"]), Decimal(values["close"])) <= Decimal(values["high"])):
        raise ValueError("Invalid OHLC")
    return values


def reference(root, run, component, value):
    path = root / "runs" / run / (component + ".json")
    write_json(path, value)
    return {"file": f"runs/{run}/{component}.json", "run_id": run, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def validate_batch(value, now):
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("market") not in {"kr", "us"}:
        raise ValueError("Invalid batch")
    if set(value) - {"schema_version", "market", "trade_date", "observed_at", "items", "series", "selection", "rank_source", "daily_confirmation"}:
        raise ValueError("Unexpected batch fields")
    confirmation = value.get("daily_confirmation")
    if confirmation is not None and (value["market"] != "us" or confirmation != "yahoo_regular_session_v1"):
        raise ValueError("Invalid daily confirmation source")
    day = date.fromisoformat(value["trade_date"])
    observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    if observed.tzinfo is None or observed > now + timedelta(minutes=5) or now - observed > timedelta(days=7):
        raise ValueError("Invalid observation time")
    items = value["items"]
    if len(items) != 50 or [item["rank"] for item in items] != list(range(1, 51)):
        raise ValueError("Expected exactly 50 ranked stocks")
    if len({(item.get("exchange", ""), item["symbol"]) for item in items}) != 50:
        raise ValueError("Duplicate stocks")
    for item in items:
        if not isinstance(item.get("name"), str) or len(item["name"]) > 200:
            raise ValueError("Invalid stock name")
        if value["market"] == "us":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,19}", item["symbol"]) or ".." in item["symbol"] or item.get("exchange") not in {"ND", "NY", "NA"}:
                raise ValueError("Invalid US identity")
            if item.get("trade_date") != day.isoformat() or item.get("currency") != "USD" or item.get("kind") != "stock":
                raise ValueError("Invalid US quote scope")
            if confirmation:
                # This is a provider snapshot, not a publishable bar. All prices are replaced and
                # checked against a dated regular-session daily source in stage_us, atomically.
                for field in ("open", "high", "low", "close"):
                    number(item[field], positive=True)
            else:
                price_check(item)
            for field in ("volume", "turnover_raw"):
                if Decimal(number(item[field])) < 0:
                    raise ValueError("Negative amount")
            if item.get("turnover_reported_unit") != "thousand_USD":
                raise ValueError("Invalid reported unit")
            # Recompute documented units instead of trusting the browser's derived numbers.
            documented = Decimal(product(number(item["turnover_raw"]), "1000"))
            low, high = (Decimal(product(number(item[key]), number(item["volume"]))) for key in ("low", "high"))
            item["turnover_usd"] = format(documented, "f") if low <= documented <= high else None
        elif not re.fullmatch(r"[0-9A-Z]{6}", item["symbol"]):
            raise ValueError("Invalid KR identity")
    if value["market"] == "us":
        if latest_completed(observed)["trade_date"] != day.isoformat():
            raise ValueError("US target is not the completed session at observation")
        expected = "current" if observed.astimezone(NEW_YORK).date() == day else "previous"
        if value.get("rank_source") != expected:
            raise ValueError("Invalid rank date basis")
    return day


def run_id(value):
    observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    return observed.strftime("%Y%m%dT%H%M%SZ-") + hashlib.sha256(json_text(value).encode()).hexdigest()[:8]


def stage_us(value, day, root, fetch_index, fetch_stock):
    run = run_id(value)
    market = root / "market"
    scope = {"market": "US", "data_mode": "live", "trade_date": day.isoformat()}
    fields = ("kind", "symbol", "exchange", "name", "trade_date", "currency", "open", "high", "low", "close", "previous_close", "change_pct", "volume", "turnover_usd", "turnover_raw", "turnover_reported_unit", "turnover_unit_status")
    stocks = [{**scope, **pick(item, fields)} for item in value["items"]]
    request = SimpleNamespace(day=day, id=run, path=root / "indices-source", observed_at=datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00")))
    if value.get("daily_confirmation"):
        for stock in stocks:
            try:
                stock.update(fetch_stock(request, stock))
                price_check(stock)
                volume = Decimal(number(stock["volume"]))
                if volume < 0 or volume != volume.to_integral_value():
                    raise ValueError("Invalid confirmed volume")
                number(stock["previous_close"], positive=True)
                number(stock["change_pct"])
            except Exception as error:
                raise CollectionFailure("stock_daily_unavailable", stock["symbol"]) from error
            documented = Decimal(product(stock["turnover_raw"], "1000"))
            low, high = (Decimal(product(stock[key], stock["volume"])) for key in ("low", "high"))
            consistent = low <= documented <= high
            stock["turnover_usd"] = format(documented, "f") if consistent else None
            stock["turnover_unit_status"] = "documented_unit_consistent_with_price_volume" if consistent else "documentation_response_inconsistent"
    indices = []
    for code in ("IXIC", "DJI"):
        try:
            indices.append(fetch_index(request, code))
        except Exception as error:
            raise CollectionFailure("index_daily_unavailable", code) from error
    selection = {**scope, "items": [pick(item, ("symbol", "exchange", "name", "rank", "provider_rank")) for item in value["items"]],
                 "observed_at": value["observed_at"], "rank_source": value["rank_source"], "rank_date_explicit_in_response": False}
    daily = {**scope, "records": [*indices, *stocks], "collection_status": "complete", "observed_at": value["observed_at"],
             "turnover_unit_unverified_count": sum(row["turnover_usd"] is None for row in stocks)}
    write_json(market / "days" / (day.isoformat() + ".json"), {**scope, "collection_status": "complete",
        "selection": reference(market, run, "selection", selection), "daily": reference(market, run, "daily", daily)})


def stage_kr(value, day, root, public_root):
    from zoneinfo import ZoneInfo
    observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Seoul"))
    if day > observed.date():
        raise ValueError("Future KR day")
    if day < observed.date():
        import importlib.util
        spec = importlib.util.spec_from_file_location("browser_fx", Path(__file__).with_name("refresh-fx.py"))
        fx = importlib.util.module_from_spec(spec); spec.loader.exec_module(fx)
        manifest = json.loads((public_root / "index.json").read_text())
        saved = fx.load(public_root, manifest, "kr/" + day.isoformat() + "/overview.json")
        if [(row["symbol"], row["rank"]) for row in saved["items"]] != [(row["symbol"], row["rank"]) for row in value["items"]]:
            raise ValueError("Historical selection differs")
        metadata = saved["selection"]
    else:
        if day.weekday() >= 5 or (observed.hour, observed.minute) < (20, 20):
            raise ValueError("KR closing window")
        metadata = pick(value.get("selection", {}), SELECTION_FIELDS)
    run = run_id(value)
    market = root / "market"
    scope = {"trade_date": day.isoformat(), "data_mode": "live"}
    selection = {**scope, **metadata, "items": [pick(item, ("symbol", "name", "rank", "provider_rank", "market_code", "turnover_million_krw")) for item in value["items"]]}
    targets = {("index", "001"), ("index", "101")} | {("stock", row["symbol"]) for row in value["items"]}
    series = value["series"]
    if len(series) != 52 or {(row["kind"], row["symbol"]) for row in series} != targets:
        raise ValueError("Expected exactly 52 series")
    refs, amounts = {}, []
    for row in series:
        previous = ""
        bars = []
        for raw in row["bars"]:
            stamp = raw["at"]
            if not re.fullmatch(re.escape(day.isoformat()) + r"T\d{2}:\d{2}:00\+09:00", stamp) or stamp <= previous:
                raise ValueError("Invalid minute scope or ordering")
            datetime.fromisoformat(stamp)
            previous = stamp
            volume = number(raw["volume"])
            if Decimal(volume) < 0:
                raise ValueError("Invalid minute volume")
            bars.append({"at": stamp, **price_check(raw), "volume": volume})
        if not bars:
            raise ValueError("Empty minute series")
        key = row["kind"] + "-" + row["symbol"]
        refs[key] = reference(market, run, key, {**scope, **pick(row, ("kind", "symbol", "name")), "bars": bars, "session_completeness": "unverified"})
        amount = row["amount"]
        millions, volume = number(amount["turnover_million_krw"]), number(amount["reported_volume"])
        if Decimal(millions) < 0 or Decimal(volume) < 0:
            raise ValueError("Invalid daily amount")
        amounts.append({**scope, **pick(row, ("kind", "symbol", "name")), "turnover_million_krw": millions,
                        "turnover_krw": product(millions, "1000000"), "reported_volume": volume,
                        "market_scope": "KRX_index" if row["kind"] == "index" else "KRX_NXT_integrated", "basis": "provider_reported_daily", "unit": "million_KRW"})
    write_json(market / "days" / (day.isoformat() + ".json"), {**scope, "selection_status": metadata.get("status", "captured_after_close"), "series": refs,
        "selection": reference(market, run, "selection", selection), "turnover": reference(market, run, "turnover", {**scope, "status": "complete", "basis": "provider_reported_daily", "items": amounts})})


def merge_archive(source, target):
    fresh = json.loads((source / "index.json").read_text())
    saved = json.loads((target / "index.json").read_text())
    if saved.get("purpose") != "public_market_prices_only":
        raise ValueError("Invalid public archive")
    for market in ("kr", "us"):
        path = market + "/days.json"
        old = json.loads(read_market_bytes(target / saved["files"][path]["file"]))
        new = json.loads((source / path).read_text())
        entries = {row["trade_date"]: row for row in old["items"]}
        entries.update({row["trade_date"]: row for row in new["items"]})
        new["items"] = sorted(entries.values(), key=lambda row: row["trade_date"], reverse=True)
        content = (json_text(new) + "\n").encode()
        (source / path).write_bytes(content)
        fresh["files"][path] = {"file": path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), "encoding": "json", "format": "json",
                                "uncompressed_bytes": len(content), "content_sha256": hashlib.sha256(content).hexdigest()}
        saved[market + "_days"] = [row["trade_date"] for row in new["items"]]
    for logical, ref in fresh["files"].items():
        path = target / ref["file"]; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((source / ref["file"]).read_bytes())
        saved["files"][logical] = ref
    write_json(target / "index.json", saved)


def finish(root, *, now=None, fetch_index=index_daily, fetch_stock=public_stock_daily):
    now = now or datetime.now(timezone.utc)
    for path in sorted((root / "pending-market").glob("*.json.gz")):
        identity = path.name.removesuffix(".json.gz")
        if not REQUEST.fullmatch(identity):
            raise ValueError("Unexpected pending file")
        receipt = {"id": identity, "state": "failed", "market": identity[:2], "trade_date": "", "finished_at": now.isoformat()}
        try:
            if path.stat().st_size > 12 * 1024 * 1024:
                raise ValueError("Compressed batch too large")
            with gzip.open(path, "rb") as stream:
                plain = stream.read(MAX_BYTES + 1)
            if len(plain) > MAX_BYTES:
                raise ValueError("Batch too large")
            value = json.loads(plain)
            day = validate_batch(value, now)
            if value["market"] != identity[:2]:
                raise ValueError("Request market mismatch")
            receipt["trade_date"] = day.isoformat()
            with tempfile.TemporaryDirectory(prefix=".offtick-browser-", dir=root) as temp:
                stage = Path(temp)
                if value["market"] == "us":
                    stage_us(value, day, stage / "us", fetch_index, fetch_stock)
                else:
                    stage_kr(value, day, stage / "kr", root / "market-data")
                export_market(stage / "kr/market", stage / "us", stage / "public")
                shutil.copytree(root / "market-data", stage / "merged")
                merge_archive(stage / "public", stage / "merged")
                os.replace(root / "market-data", stage / "previous")
                try:
                    os.replace(stage / "merged", root / "market-data")
                except BaseException:
                    os.replace(stage / "previous", root / "market-data")
                    raise
            receipt.update(state="succeeded", message="시세 검증과 Git 저장 완료")
        except CollectionFailure as error:
            label = "종목 마감 일봉" if error.code == "stock_daily_unavailable" else "미국 지수 일봉"
            receipt.update(error_code=error.code, symbol=error.symbol,
                message=f"{error.symbol}의 {receipt['trade_date']} {label}을 확인하지 못했습니다. 이전 자료와 수집 결과는 유지됩니다. 잠시 후 업로드를 재시도해 주세요.")
        except Exception:
            # Never include provider messages, arbitrary input or local paths in public receipts.
            receipt.update(error_code="collection_validation_failed", message="수집 자료 검증에 실패했습니다. 이전 자료와 수집 결과는 유지됩니다. 업로드를 재시도해 주세요.")
        write_json(root / "collection-results" / (identity + ".json"), receipt)
        path.unlink()
        print(identity + ": " + receipt["state"] + (" · " + receipt["error_code"] if "error_code" in receipt else ""), flush=True)


if __name__ == "__main__":
    finish(ROOT)

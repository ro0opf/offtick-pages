"""Archive public USD/KRW observations and value US turnover without a broker or secrets."""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib import request
from zoneinfo import ZoneInfo

MAX_BYTES = 64 * 1024 * 1024
FX_PATH = re.compile(r"fx/(?:index\.json|\d{4}-\d{2}-\d{2}/\d{8}T\d{12}Z-USD-KRW\.json)")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid non-negative decimal.")
    return result


def multiply(left, right):
    a, b = decimal(left), decimal(right)
    with localcontext() as context:
        context.prec = max(28, len(a.as_tuple().digits) + len(b.as_tuple().digits))
        return format(a * b, "f")


def rate_url(day):
    return f"https://api.frankfurter.dev/v2/rate/USD/KRW?date={day.isoformat()}&providers=ecb"


def fetch_rate(day):
    req = request.Request(rate_url(day), headers={"Accept": "application/json", "User-Agent": "offtick/1.0"})
    with request.urlopen(req, timeout=20) as response:
        content = response.read(16385)
    if len(content) > 16384:
        raise ValueError("Exchange-rate response is too large.")
    return json.loads(content, parse_float=Decimal)


def validate_rate(value, day):
    actual = date.fromisoformat(value["date"])
    if value["base"] != "USD" or value["quote"] != "KRW" or not 0 <= (day - actual).days <= 7 or decimal(value["rate"]) <= 0:
        raise ValueError("Invalid currency, reference date or rate.")
    return {"base": "USD", "quote": "KRW", "date": actual.isoformat(), "requested_date": day.isoformat(),
            "rate": format(decimal(value["rate"]), "f"), "source": "ECB · Frankfurter", "source_url": rate_url(day)}


def load(root, manifest, logical):
    reference = manifest["files"][logical]
    filename = reference["file"]
    if filename not in (logical, logical + ".gz"):
        raise ValueError("Unexpected archive path.")
    content = (root / filename).read_bytes()
    if len(content) != reference["bytes"] or hashlib.sha256(content).hexdigest() != reference["sha256"]:
        raise ValueError("Archive checksum mismatch: " + logical)
    plain = gzip.decompress(content) if reference["encoding"] == "gzip" else content
    if (len(plain) > MAX_BYTES or len(plain) != reference["uncompressed_bytes"]
            or hashlib.sha256(plain).hexdigest() != reference["content_sha256"]):
        raise ValueError("Archive content mismatch: " + logical)
    return json.loads(plain)


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == content:
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fx-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh(root, fetch=fetch_rate, now=None, allow_cached=False):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("A timezone-aware capture time is required.")
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    manifest = json.loads((root / "index.json").read_text())
    if manifest.get("schema_version") != 2 or manifest.get("purpose") != "public_market_prices_only":
        raise ValueError("Expected a public market archive.")
    references = dict(manifest["files"])
    pending = {}

    def store(logical, value, compressed=False):
        plain = encoded(value)
        content = gzip.compress(plain, mtime=0) if compressed else plain
        filename = logical + ".gz" if compressed else logical
        pending[filename] = content
        references[logical] = {"file": filename, "encoding": "gzip" if compressed else "json", "format": "json",
                               "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                               "content_sha256": hashlib.sha256(plain).hexdigest(), "uncompressed_bytes": len(plain)}

    registry = load(root, manifest, "fx/index.json") if "fx/index.json" in references else {"schema_version": 1, "rates": {}}
    if registry.get("schema_version") != 1 or not isinstance(registry.get("rates"), dict):
        raise ValueError("Invalid exchange-rate registry.")
    rates = dict(registry["rates"])
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    reused = False

    def cached(day):
        candidates = []
        for logical in references:
            if not FX_PATH.fullmatch(logical) or logical == "fx/index.json":
                continue
            value = json.loads(pending[logical]) if logical in pending else load(root, manifest, logical)
            if value.get("reused_from"):
                continue
            try:
                checked = validate_rate(value, day)
                fetched = datetime.fromisoformat(value["fetched_at"])
                if fetched.tzinfo is None or fetched > now or value["source_url"] != rate_url(date.fromisoformat(value["requested_date"])):
                    raise ValueError("Invalid cached observation")
            except (ValueError, KeyError, TypeError):
                continue
            candidates.append({**value, "archive_file": logical})
        if not candidates:
            raise ValueError("No verified, non-future exchange rate within seven days.")
        return max(candidates, key=lambda value: (value["date"], value["fetched_at"]))

    def capture(day):
        nonlocal reused
        try:
            value = {**validate_rate(fetch(day), day), "fetched_at": now.astimezone(timezone.utc).isoformat()}
        except (OSError, TimeoutError):
            if not allow_cached:
                raise
            original = cached(day)
            reused = True
            if original["requested_date"] == day.isoformat():
                return original
            value = {**validate_rate(original, day), "fetched_at": original["fetched_at"], "source_url": original["source_url"],
                     "capture_method": "reused_verified_archive", "reused_from": original["archive_file"]}
        logical = f"fx/{day.isoformat()}/{stamp}-USD-KRW.json"
        store(logical, value)
        return {**value, "archive_file": logical}

    latest = capture(today)
    listing = load(root, manifest, "us/days.json")
    converted = 0
    for entry in listing["items"]:
        day = date.fromisoformat(entry["trade_date"])
        if day > today:
            raise ValueError("Future US trade date.")
        logical = f"us/{day.isoformat()}/daily.json"
        value = load(root, manifest, logical)
        if value["trade_date"] != day.isoformat() or value["status"] != "complete":
            raise ValueError("US archive date or completion mismatch.")
        saved = rates.get(day.isoformat())
        if saved:
            if not FX_PATH.fullmatch(saved) or saved == "fx/index.json":
                raise ValueError("Invalid saved exchange-rate path.")
            saved_value = json.loads(pending[saved]) if saved in pending else load(root, manifest, saved)
            rate = {**validate_rate(saved_value, day), "fetched_at": saved_value["fetched_at"], "archive_file": saved}
            if saved_value.get("reused_from"):
                if not FX_PATH.fullmatch(saved_value["reused_from"]) or saved_value["reused_from"] == "fx/index.json":
                    raise ValueError("Invalid reused observation path.")
                original = load(root, manifest, saved_value["reused_from"])
                if any(saved_value[key] != original[key] for key in ("rate", "date", "fetched_at", "source_url")):
                    raise ValueError("Reused rate differs from the verified observation.")
                rate.update(source_url=saved_value["source_url"], capture_method="reused_verified_archive", reused_from=saved_value["reused_from"])
            if saved_value["requested_date"] != day.isoformat():
                raise ValueError("Saved exchange-rate request mismatch.")
        else:
            rate = latest if day == today else capture(day)
            rates[day.isoformat()] = rate["archive_file"]
        items = []
        for row in value["items"]:
            updated = {**row}
            try:
                if row.get("currency") != "USD":
                    raise ValueError("Unsupported stock currency.")
                if row.get("turnover_usd") is not None:
                    amount, basis = format(decimal(row["turnover_usd"]), "f"), "provider_reported"
                elif row.get("turnover_display_usd") is not None and row.get("turnover_basis") in ("raw_usd_inferred", "close_times_volume"):
                    amount, basis = format(decimal(row["turnover_display_usd"]), "f"), row["turnover_basis"]
                else:
                    close, low, high, volume = (decimal(row[field]) for field in ("close", "low", "high", "volume"))
                    if not 0 < low <= close <= high:
                        raise ValueError("Invalid stock prices.")
                    amount, basis = multiply(close, volume), "close_times_volume"
                updated.update(turnover_display_usd=amount, turnover_krw=multiply(amount, rate["rate"]),
                               turnover_basis=basis, turnover_is_estimate=basis != "provider_reported")
                converted += 1
            except (ValueError, InvalidOperation, KeyError, TypeError):
                updated.update(turnover_display_usd=None, turnover_krw=None, turnover_basis="unavailable", turnover_is_estimate=False)
            items.append(updated)
        store(logical, {**value, "items": items, "exchange_rate": rate,
                        "turnover_estimate_count": sum(row["turnover_is_estimate"] for row in items)}, compressed=True)
    store("fx/index.json", {"schema_version": 1, "base": "USD", "quote": "KRW", "latest": latest, "rates": rates,
                           "schedule_kst": ["06:17", "18:17"]})
    # Complete all network calls and validation before replacing any published data; publish the manifest last.
    for filename, content in pending.items():
        atomic_write(root / filename, content)
    atomic_write(root / "index.json", encoded({**manifest, "files": references}))
    return {"rate": latest["rate"], "reference_date": latest["date"], "fetched_at": latest["fetched_at"],
            "us_days": len(listing["items"]), "converted_rows": converted, "reused_cached": reused}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "apps/web/public/market-data")
    parser.add_argument("--allow-cached", action="store_true", help="On network failure reuse verified archived rates, retaining the original observation time")
    args = parser.parse_args()
    print(json.dumps(refresh(args.root, allow_cached=args.allow_cached), ensure_ascii=False))


if __name__ == "__main__":
    main()

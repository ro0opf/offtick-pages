"""NYSE 공개 달력(2025~2027)과 IANA 시간대로 계산하는 미국 정규 세션."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .common import HistoryError

NEW_YORK = ZoneInfo("America/New_York")
CALENDAR_SOURCE = "https://www.nyse.com/markets/hours-calendars"
CALENDAR_VERIFIED_ON = "2026-10-26"
HOLIDAYS = {
    2025: {"01-01", "01-09", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04", "09-01", "11-27", "12-25"},
    2026: {"01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"},
    2027: {"01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05", "09-06", "11-25", "12-24"},
}
# 2025-01-09 특별 휴장: https://www.nyse.com/publicdocs/nyse/markets/american-options/rule-interpretations/2025/National_Day_of_Mourning_20250102.pdf
EARLY_CLOSES = {2025: {"07-03", "11-28", "12-24"}, 2026: {"11-27", "12-24"}, 2027: {"11-26"}}


def session(day: date):
    if day.year not in HOLIDAYS:
        raise HistoryError("미국 거래소 달력 지원 연도를 갱신해야 합니다. 평일을 거래일로 추정하지 않았습니다.")
    if day.weekday() >= 5 or day.strftime("%m-%d") in HOLIDAYS[day.year]:
        return None
    early = day.strftime("%m-%d") in EARLY_CLOSES[day.year]
    return {"trade_date": day.isoformat(), "open": datetime.combine(day, time(9, 30), NEW_YORK),
            "close": datetime.combine(day, time(13 if early else 16), NEW_YORK), "early_close": early}


def previous_session(day):
    for offset in range(1, 15):
        result = session(day - timedelta(days=offset))
        if result:
            return result
    raise HistoryError("이전 미국 거래일을 확인하지 못했습니다.")


def latest_completed(now, delay_minutes=20):
    if now.tzinfo is None or delay_minutes < 0:
        raise HistoryError("시간대가 있는 현재시각과 수집 지연값을 지정하세요.")
    local = now.astimezone(NEW_YORK)
    for offset in range(15):
        result = session(local.date() - timedelta(days=offset))
        if result and result["close"] + timedelta(minutes=delay_minutes) <= local:
            return result
    raise HistoryError("완료된 미국 거래 세션을 확인하지 못했습니다.")

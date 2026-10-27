"""정적 분봉의 반복 키·날짜를 한 번만 저장하고 기존 화면용 구조로 정확히 복원한다."""
from datetime import date
import re

from offtick_history.common import HistoryError

FORMAT = "minute_rows_v1"
COLUMNS = ["minute", "open", "high", "low", "close", "volume"]


def pack_bars(value):
    rows = []
    for bar in value["bars"]:
        stamp = re.fullmatch(re.escape(value["trade_date"]) + r"T([0-2]\d):([0-5]\d):00\+09:00", bar["at"])
        if not stamp or int(stamp[1]) > 23:
            raise HistoryError("분봉을 압축할 시각 형식을 확인하세요.")
        minute = int(stamp[1]) * 60 + int(stamp[2])
        rows.append([minute] + [bar[column] for column in COLUMNS[1:]])
    packed = {**{key: item for key, item in value.items() if key != "bars"},
              "format": FORMAT, "timezone": "Asia/Seoul", "columns": COLUMNS, "rows": rows}
    if unpack_bars(packed) != value:
        raise HistoryError("분봉 압축 전후 값이 다릅니다.")
    return packed


def unpack_bars(value):
    if value.get("format") != FORMAT or value.get("columns") != COLUMNS or value.get("timezone") != "Asia/Seoul":
        raise HistoryError("지원하지 않는 압축 분봉 형식입니다.")
    day = date.fromisoformat(value["trade_date"]).isoformat()
    bars, previous = [], -1
    for row in value["rows"]:
        if (not isinstance(row, list) or len(row) != 6 or type(row[0]) is not int or not previous < row[0] < 1440
                or any(not isinstance(item, str) for item in row[1:])):
            raise HistoryError("압축 분봉의 순서·수치 형식을 확인하세요.")
        minute = row[0]
        previous = minute
        bars.append({"at": f"{day}T{minute // 60:02d}:{minute % 60:02d}:00+09:00", **dict(zip(COLUMNS[1:], row[1:]))})
    return {**{key: item for key, item in value.items() if key not in {"format", "timezone", "columns", "rows"}}, "bars": bars}

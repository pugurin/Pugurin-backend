import calendar
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

Clock = Callable[[], datetime]


def kst_now() -> datetime:
    return datetime.now(KST)


def months_ago(d: date, months: int) -> date:
    index = d.year * 12 + (d.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def latest_etl_time(now: datetime) -> datetime:
    """ETL은 매일 KST 04:00에 끝난다고 가정한 가장 최근 기준일시."""
    now = now.astimezone(KST)
    base = datetime.combine(now.date(), time(4, 0), tzinfo=KST)
    return base if now >= base else base - timedelta(days=1)

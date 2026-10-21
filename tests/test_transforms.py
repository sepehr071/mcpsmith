from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mcpsmith.runtime import ApiError
from mcpsmith.runtime.shape import apply_transforms
from mcpsmith.runtime.transforms import INPUT_NAMES, NAMES, OUTPUT_NAMES, transform_in, transform_out


def test_jalali_dates():
    assert transform_in("jalali->date", "1403/12/30") == "2025-03-20"  # leap year end
    assert transform_in("jalali->date", "1404/01/01") == "2025-03-21"
    assert transform_in("date->jalali", "2026-10-20") == "1405/07/28"
    assert transform_in("jalali->date", "۱۴۰۵/۰۷/۲۸") == "2026-10-20"
    assert transform_in("jalali->date", "١٤٠٥/٠٧/٢٨") == "2026-10-20"


def test_unix():
    tehran = ZoneInfo("Asia/Tehran")
    start = int(datetime(2026, 10, 20, tzinfo=tehran).timestamp())
    assert transform_in("jalali->unix", "1405/07/28", tz="Asia/Tehran") == start
    assert transform_in("jalali->unix_end", "1405/07/28", tz="Asia/Tehran") == start + 86399
    assert transform_in("date->unix", "2026-10-20", tz="UTC") == 1792454400
    assert transform_in("date->unix_end", "2026-10-20", tz="UTC") == 1792454400 + 86399
    local = datetime.now().astimezone().tzinfo
    assert transform_in("date->unix", "2026-10-20") == int(datetime(2026, 10, 20, tzinfo=local).timestamp())


def test_other_inputs():
    assert transform_in("toman->rial", 1250) == 12500
    assert transform_in("int->str", 7) == "7"


@pytest.mark.parametrize(
    "name, value, needle",
    [
        ("date->unix", "2026-02-30", "YYYY-MM-DD"),
        ("date->unix", "20/10/2026", "YYYY-MM-DD"),
        ("jalali->date", "1404/12/30", "YYYY/MM/DD"),  # 1404 is not a leap year
        ("jalali->date", "1405/13/01", "YYYY/MM/DD"),
        ("jalali->date", "1600/01/01", "1300"),
        ("date->jalali", "1200-01-01", "1300"),
        ("toman->rial", "abc", "number"),
    ],
)
def test_bad_input(name, value, needle):
    with pytest.raises(ApiError, match=needle):
        transform_in(name, value)


def test_outputs():
    noon = 1792454400 + 12 * 3600  # 2026-10-20 12:00 UTC (the brief's 1792440000 is 2026-10-19 20:00 UTC)
    assert transform_out("unix->date", noon, tz="UTC") == "2026-10-20"
    assert transform_out("unix->date", 1792440000, tz="UTC") == "2026-10-19"
    assert transform_out("unix->datetime", noon, tz="UTC") == "2026-10-20 12:00"
    assert transform_out("date->jalali", "2026-10-20") == "1405/07/28"
    assert transform_out("jalali->date", "1405/07/28") == "2026-10-20"
    assert transform_out("minutes->hh:mm", 125) == "02:05"
    assert transform_out("rial->toman", 125000) == 12500


def test_none_and_unknown():
    assert transform_in("jalali->unix", None) is None
    assert transform_out("rial->toman", None) is None
    with pytest.raises(ApiError, match="Unknown transform"):
        transform_in("nope", 1)
    with pytest.raises(ApiError, match="Unknown transform"):
        transform_out("toman->rial", 1)  # an input-only name


def test_names():
    assert NAMES == INPUT_NAMES | OUTPUT_NAMES


def test_round_trip_every_day():
    d = date(2020, 1, 1)
    while d <= date(2030, 12, 31):
        assert transform_in("jalali->date", transform_in("date->jalali", d.isoformat())) == d.isoformat()
        d += timedelta(days=1)


def test_apply_transforms():
    data = {"items": [{"day": "2026-10-20", "price": 10000}, {"day": None}], "count": 2}
    out = apply_transforms(data, {"day": "date->jalali", "price": "rial->toman"}, "")
    assert out["items"] == [{"day": "1405/07/28", "price": 1000}, {"day": None}]
    assert apply_transforms({"day": "2026-10-20"}, {"day": "date->jalali"}) == {"day": "1405/07/28"}
    assert apply_transforms("text", {"day": "date->jalali"}) == "text"

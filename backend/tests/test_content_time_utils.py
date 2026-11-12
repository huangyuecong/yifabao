from datetime import UTC, datetime

from content_service.time_utils import UTCDateTime, ensure_utc


def test_ensure_utc_treats_naive_values_as_utc():
    naive = datetime(2026, 9, 16, 6, 0, 0)
    aware = ensure_utc(naive)
    assert aware.tzinfo is UTC
    converted = ensure_utc(datetime(2026, 9, 16, 14, 0, tzinfo=UTC))
    assert converted.tzinfo is UTC


def test_utc_datetime_round_trips_without_schema_change():
    column = UTCDateTime()
    assert column.process_bind_param(None, None) is None
    assert column.process_result_value(None, None) is None
    bound = column.process_bind_param(datetime(2026, 9, 16, 8, 0, tzinfo=UTC), None)
    assert bound.tzinfo is None
    restored = column.process_result_value(datetime(2026, 9, 16, 8, 0, 0), None)
    assert restored.tzinfo is UTC
    assert restored.hour == 8

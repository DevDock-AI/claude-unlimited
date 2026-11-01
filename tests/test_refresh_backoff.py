from claude_unlimited import refresh_backoff as rb


def test_a_first_transient_failure_waits_the_ordinary_cooldown():
    assert rb.failure_wait(500, 1, 60.0, 900.0) == 60.0
    assert rb.failure_wait(None, 1, 60.0, 900.0) == 60.0


def test_every_kind_of_failure_escalates_to_its_ceiling():
    for status, ceiling in ((429, rb.SLOW_CEILING_SECONDS), (400, rb.SLOW_CEILING_SECONDS),
                            (500, rb.TRANSIENT_CEILING_SECONDS), (None, rb.TRANSIENT_CEILING_SECONDS)):
        waits = [rb.failure_wait(status, n, 60.0, 900.0) for n in range(1, 30)]
        assert waits == sorted(waits) and waits[0] < waits[-1] == ceiling, status


def test_a_rejected_grant_starts_at_ten_minutes():
    assert rb.failure_wait(400, 1, 60.0, 900.0) == rb.REJECTED_BASE_SECONDS
    assert rb.failure_wait(401, 1, 60.0, 900.0) == rb.REJECTED_BASE_SECONDS


def test_logging_is_logarithmic():
    assert [n for n in range(1, 20) if rb.worth_logging(n)] == [1, 2, 4, 8, 16]


def test_describe_is_readable():
    assert rb.describe(400, 1, 600) == "HTTP 400, 1 in a row — next attempt in 10 min"
    assert rb.describe(None, 3, 7200) == "network error, 3 in a row — next attempt in 2h"

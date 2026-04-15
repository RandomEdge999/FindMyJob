from findmyjob.core.retry import _retry_delay


def test_retry_delay_applies_jitter(monkeypatch) -> None:
    monkeypatch.setattr('findmyjob.core.retry.random.random', lambda: 0.0)
    assert _retry_delay(3, 2.0, 60.0) == 4.0

    monkeypatch.setattr('findmyjob.core.retry.random.random', lambda: 1.0)
    assert _retry_delay(3, 2.0, 60.0) == 8.0

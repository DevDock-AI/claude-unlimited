import threading

import pytest

import claude_unlimited.project_usage as project_usage


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr(project_usage, "USAGE_FILE", tmp_path / "project_usage.json")
    return tmp_path


def test_get_counts_empty_by_default(env):
    assert project_usage.get_counts() == {}


def test_record_request_increments_count(env):
    project_usage.record_request("-Users-a-app")
    project_usage.record_request("-Users-a-app")
    project_usage.record_request("-Users-b-other")
    assert project_usage.get_counts() == {"-Users-a-app": 2, "-Users-b-other": 1}


def test_record_request_persists_across_reloads(env):
    project_usage.record_request("-Users-a-app")
    # Simulate a fresh process reading the same file.
    assert project_usage.get_counts() == {"-Users-a-app": 1}


def test_reset_clears_all_counts(env):
    project_usage.record_request("-Users-a-app")
    project_usage.reset()
    assert project_usage.get_counts() == {}


def test_get_counts_survives_corrupt_file(env):
    project_usage.USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    project_usage.USAGE_FILE.write_text("not json")
    assert project_usage.get_counts() == {}


def test_record_request_is_thread_safe_under_concurrent_calls(env):
    # The daemon calls this from the proxy hot path under ThreadingHTTPServer,
    # so concurrent requests for different projects must not lose increments to
    # a read-modify-write race. 20 threads x 25 calls reliably reproduces a
    # lost update without the lock in project_usage.py.
    threads_count, calls_per_thread = 20, 25

    def hammer():
        for _ in range(calls_per_thread):
            project_usage.record_request("-Users-a-app")

    threads = [threading.Thread(target=hammer) for _ in range(threads_count)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert project_usage.get_counts() == {"-Users-a-app": threads_count * calls_per_thread}

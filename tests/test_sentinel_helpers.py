"""Tests for sentinel.py — SessionStore, HistoryStore, and config helpers."""

from ax_cli.runtimes.hermes.sentinel import (
    HistoryStore,
    SessionStore,
    _load_config,
    parse_args,
)

# ---- SessionStore ----


def test_session_store_get_set():
    store = SessionStore()
    store.set("thread-1", "session-a")
    assert store.get("thread-1") == "session-a"


def test_session_store_get_missing():
    store = SessionStore()
    assert store.get("missing") is None


def test_session_store_delete():
    store = SessionStore()
    store.set("thread-1", "session-a")
    store.delete("thread-1")
    assert store.get("thread-1") is None


def test_session_store_eviction():
    store = SessionStore(max_sessions=3)
    store.set("t1", "s1")
    store.set("t2", "s2")
    store.set("t3", "s3")
    store.set("t4", "s4")
    assert store.count() == 3
    assert store.get("t1") is None


def test_session_store_count():
    store = SessionStore()
    assert store.count() == 0
    store.set("t1", "s1")
    assert store.count() == 1


# ---- HistoryStore ----


def test_history_store_get_set():
    store = HistoryStore()
    store.set("t1", [{"role": "user", "content": "hi"}])
    assert store.get("t1") == [{"role": "user", "content": "hi"}]


def test_history_store_get_missing():
    store = HistoryStore()
    assert store.get("missing") == []


def test_history_store_returns_copies():
    store = HistoryStore()
    store.set("t1", [{"role": "user", "content": "hi"}])
    result = store.get("t1")
    result.append({"role": "assistant", "content": "bye"})
    assert len(store.get("t1")) == 1


def test_history_store_trims():
    store = HistoryStore(max_messages=2)
    store.set("t1", [{"n": 1}, {"n": 2}, {"n": 3}])
    assert len(store.get("t1")) == 2
    assert store.get("t1")[0]["n"] == 2


def test_history_store_evicts():
    store = HistoryStore(max_threads=2)
    store.set("t1", [{"n": 1}])
    store.set("t2", [{"n": 2}])
    store.set("t3", [{"n": 3}])
    assert store.get("t1") == []


def test_history_store_delete():
    store = HistoryStore()
    store.set("t1", [{"n": 1}])
    store.delete("t1")
    assert store.get("t1") == []


# ---- _load_config ----


def test_load_config_no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    result = _load_config()
    assert result == {}


def test_load_config_project_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    (ax_dir / "config.toml").write_text('token = "axp_a_test"\nbase_url = "https://example.com"\n')
    result = _load_config()
    assert result["token"] == "axp_a_test"


# ---- parse_args ----


def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr("sys.argv", ["sentinel"])
    args = parse_args()
    assert not args.dry_run
    assert args.runtime == "hermes_sdk"
    assert args.timeout == 300


def test_parse_args_custom(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "sentinel",
            "--dry-run",
            "--agent",
            "mybot",
            "--runtime",
            "openai_sdk",
            "--timeout",
            "600",
        ],
    )
    args = parse_args()
    assert args.dry_run
    assert args.agent == "mybot"
    assert args.runtime == "openai_sdk"
    assert args.timeout == 600

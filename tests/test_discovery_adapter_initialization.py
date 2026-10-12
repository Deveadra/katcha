"""Concurrent first-use adapter loading, with no provider discovery or network."""

import builtins
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

import katcha.acquisition.adapters as adapters


@pytest.mark.parametrize("second_lookup", ["get", "list"])
def test_concurrent_first_use_loads_builtin_registry_once(monkeypatch, second_lookup):
    # Warm provider modules before interception; exercise initialization, not I/O.
    adapters._load_builtin_adapters()
    original_import = builtins.__import__
    monkeypatch.setattr(
        adapters,
        "_ADAPTERS",
        {
            ("manifest", "v1"): adapters.ManifestDiscoveryAdapter(),
        },
    )
    monkeypatch.setattr(adapters, "_BUILTINS_LOADED", False)
    first_import = Event()
    release_import = Event()
    second_started = Event()
    second_lock_attempt = Event()
    original_lock = adapters._BUILTIN_LOAD_LOCK

    class ObservedLock:
        def __enter__(self):
            if first_import.is_set():
                second_lock_attempt.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    monkeypatch.setattr(adapters, "_BUILTIN_LOAD_LOCK", ObservedLock())
    guard = Lock()
    import_count = 0

    def gated_import(name, *args, **kwargs):
        nonlocal import_count
        if name == "katcha.acquisition.feeds":
            with guard:
                import_count += 1
                first = import_count == 1
            if first:
                first_import.set()
                assert release_import.wait(timeout=5), "test did not release the first import"

        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", gated_import)

    def lookup_second():
        second_started.set()
        return (
            adapters.get_adapter("youtube", "v1")
            if second_lookup == "get"
            else adapters.available_adapters()
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(adapters.get_adapter, "youtube", "v1")
        try:
            assert first_import.wait(timeout=5), "first lookup did not reach its provider import"
            second = pool.submit(lookup_second)
            assert second_started.wait(timeout=5), "second lookup did not start"
            assert second_lock_attempt.wait(timeout=5), "second lookup did not reach the lock"
            assert import_count == 1
        finally:
            release_import.set()
        first_adapter = first.result(timeout=5)
        second_result = second.result(timeout=5)
    assert import_count == 1
    assert first_adapter.key == "youtube"
    if second_lookup == "get":
        assert second_result is first_adapter
    else:
        assert len(second_result) == 6
        assert {item["key"] for item in second_result} == {
            "manifest",
            "operator_feed",
            "reddit",
            "rss_atom",
            "web_scout",
            "youtube",
        }
    assert adapters._BUILTINS_LOADED
    assert len(adapters._ADAPTERS) == 6

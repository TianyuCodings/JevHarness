"""Dataset preparation uses fixtures only; no market or strategy requests."""
import copy
import hashlib
import json
from datetime import datetime

import httpx
import pytest

from auto_jev import datasets


def timestamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def fake_fetch(*, asset, start, end, granularity):
    return {
        "asset": asset, "interval_seconds": granularity, "news": [],
        "bars": [{"timestamp": when, "open": 100., "high": 102., "low": 99., "close": 101., "volume": 10.}
                 for when in range(timestamp(start), timestamp(end), granularity)],
        "provenance": {"synthetic": True, "source": "synthetic test fixture", "retrieved_at": 1800000000.},
    }


@pytest.fixture(autouse=True)
def prohibit_network_and_waits(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dataset tests must not request real market data")

    monkeypatch.setattr(datasets, "fetch_coinbase", forbidden)
    monkeypatch.setattr(datasets.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(datasets.time, "time", lambda: 1800000000.)


def read(path):
    return json.loads(path.read_text())


def small_prepare(path, fetcher=fake_fetch, **kwargs):
    return datasets.prepare_scale_dataset(path, assets=("BTC-USD",), bars_per_split=6,
                                          train_batch_bars=3, fetcher=fetcher, **kwargs)


def test_exact_default_1000_bar_splits_and_forty_fifty_bar_training_windows(tmp_path):
    requests = []

    def fetch(**kwargs):
        requests.append(kwargs)
        return fake_fetch(**kwargs)

    manifest = datasets.prepare_scale_dataset(tmp_path, fetcher=fetch)
    assert manifest["status"] == "complete"
    assert manifest["data_kind"] == "synthetic"  # Fixtures must never be labelled real.
    assert len(requests) == 22
    assert all((timestamp(request["end"]) - timestamp(request["start"])) // 3600 <= 299 for request in requests)
    splits = {name: read(tmp_path / f"{name}.json") for name in ("train", "validation", "test")}
    batches = read(tmp_path / "train_batches.json")
    assert all(len(episodes) == 2 for episodes in splits.values())
    assert all(len(episode["bars"]) == 1000 for episodes in splits.values() for episode in episodes)
    assert len(batches) == 40 and all(len(episode["bars"]) == 50 for episode in batches)
    end = timestamp(datasets.DEFAULT_END)
    start = end - 3000 * 3600
    for asset in ("BTC-USD", "ETH-USD"):
        episodes = [next(episode for episode in splits[split] if episode["asset"] == asset)
                    for split in ("train", "validation", "test")]
        full = [bar for episode in episodes for bar in episode["bars"]]
        assert [bar["timestamp"] for bar in full] == list(range(start, end, 3600))
        assert len({bar["timestamp"] for bar in full}) == 3000
        selected_batches = [episode for episode in batches if episode["asset"] == asset]
        assert [bar for episode in selected_batches for bar in episode["bars"]] == episodes[0]["bars"]
        assert [episode["provenance"]["batch_index"] for episode in selected_batches] == list(range(20))
        assert all(episode["provenance"]["parent_episode_id"] == episodes[0]["id"] for episode in selected_batches)
        assert all(episode["provenance"]["execution"]["executable_decisions"] == 49 for episode in selected_batches)
        assert episodes[1]["provenance"]["execution"]["executable_decisions"] == 999
        assert episodes[2]["provenance"]["execution"]["executable_decisions"] == 999
        assert all(episode["news"] == [] and episode["provenance"]["news_count"] == 0 for episode in episodes + selected_batches)
    assert manifest["experiment"]["type"] == "retrospective_historical"
    assert manifest["experiment"]["prospective_test"] is False
    assert manifest["experiment"]["strategy_executed_during_preparation"] is False
    assert manifest["experiment"]["test_overlaps_known_viewed_window"] is False
    assert all(row["continuous"] and not row["cross_split_overlap"] for row in manifest["continuity"])
    for name, file_info in manifest["files"].items():
        assert file_info["file_sha256"] == hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    assert datasets.verify_manifest(tmp_path) == manifest


def test_completed_dataset_reuse_never_refetches_or_rewrites(tmp_path):
    original = small_prepare(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}

    def forbidden(**kwargs):
        raise AssertionError("A verified completed dataset must be reused")

    assert small_prepare(tmp_path, fetcher=forbidden) == original
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == before


@pytest.mark.parametrize("mutation,error", [
    (lambda episode: episode["bars"].pop(), "expected exactly"),
    (lambda episode: episode["bars"].__setitem__(1, copy.deepcopy(episode["bars"][0])), "duplicate"),
    (lambda episode: episode["bars"][1].update(timestamp=episode["bars"][1]["timestamp"] + 1800), "hourly grid"),
    (lambda episode: episode["bars"][1].update(close=float("nan")), "numeric"),
    (lambda episode: episode["bars"][1].update(high=99.), "OHLC"),
    (lambda episode: episode["bars"][1].update(volume=-1.), "volume"),
    (lambda episode: episode["news"].append({"headline": "unavailable historical story"}), "historical news"),
    (lambda episode: episode.update(asset="ETH-USD"), "another asset"),
])
def test_invalid_source_data_is_not_filled_deduplicated_or_published(tmp_path, mutation, error):
    def fetch(**kwargs):
        episode = fake_fetch(**kwargs)
        mutation(episode)
        return episode

    with pytest.raises(datasets.DatasetIntegrityError, match=error):
        small_prepare(tmp_path, fetcher=fetch)
    assert not (tmp_path / "manifest.json").exists()
    assert not (tmp_path / "train.json").exists()


def test_transient_errors_have_bounded_backoff_and_record_attempts(tmp_path, monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(datasets.time, "sleep", waits.append)

    def fetch(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise httpx.ReadTimeout("temporary market timeout")
        if len(calls) == 2:
            request = httpx.Request("GET", "https://api.exchange.coinbase.com")
            raise httpx.HTTPStatusError("rate limited", request=request, response=httpx.Response(429, request=request))
        return fake_fetch(**kwargs)

    manifest = small_prepare(tmp_path, fetcher=fetch, max_attempts=3)
    assert len(calls) == 3
    assert waits == [.5, 1., .15]
    assert manifest["sources"][0]["attempts"] == 3


@pytest.mark.parametrize("status,expected_calls", [(503, 3), (401, 1), (404, 1)])
def test_download_failure_stops_at_retry_limit_and_skips_permanent_errors(tmp_path, status, expected_calls):
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        request = httpx.Request("GET", "https://api.exchange.coinbase.com")
        raise httpx.HTTPStatusError("test failure", request=request, response=httpx.Response(status, request=request))

    with pytest.raises(httpx.HTTPStatusError):
        small_prepare(tmp_path, fetcher=fetch, max_attempts=3)
    assert len(calls) == expected_calls
    assert not (tmp_path / "manifest.json").exists()


def test_interrupted_download_resumes_verified_chunks_only(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "COINBASE_PAGE_BARS", 5)
    first_calls = []

    def interrupted(**kwargs):
        first_calls.append(kwargs)
        if len(first_calls) == 2:
            raise httpx.ReadTimeout("interrupted")
        return fake_fetch(**kwargs)

    with pytest.raises(httpx.ReadTimeout):
        small_prepare(tmp_path, fetcher=interrupted, max_attempts=1)
    assert len(list((tmp_path / ".chunks").glob("*.json"))) == 1
    assert not (tmp_path / "train.json").exists()
    resumed_calls = []

    def resumed(**kwargs):
        resumed_calls.append(kwargs)
        return fake_fetch(**kwargs)

    manifest = small_prepare(tmp_path, fetcher=resumed)
    assert len(resumed_calls) == 3
    assert first_calls[0]["start"] not in {call["start"] for call in resumed_calls}
    assert manifest["continuity"][0]["bars"] == 18


def test_cached_chunk_corruption_is_not_silently_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "COINBASE_PAGE_BARS", 5)
    calls = []

    def interrupted(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise httpx.ReadTimeout("interrupted")
        return fake_fetch(**kwargs)

    with pytest.raises(httpx.ReadTimeout):
        small_prepare(tmp_path, fetcher=interrupted, max_attempts=1)
    path = next((tmp_path / ".chunks").glob("*.json"))
    cached = read(path)
    cached["bars"][0]["close"] = 100.5
    path.write_text(json.dumps(cached))
    with pytest.raises(datasets.DatasetIntegrityError, match="cached download integrity"):
        small_prepare(tmp_path)


def test_completed_file_corruption_and_configuration_changes_are_rejected(tmp_path):
    small_prepare(tmp_path)
    with pytest.raises(datasets.DatasetIntegrityError, match="different dataset configuration"):
        small_prepare(tmp_path, end="2026-09-14T00:00:00Z")
    path = tmp_path / "test.json"
    changed = read(path)
    changed[0]["bars"][0]["close"] = 100.5
    path.write_text(json.dumps(changed))
    with pytest.raises(datasets.DatasetIntegrityError, match="test.json"):
        datasets.verify_manifest(tmp_path)


@pytest.mark.parametrize("options", [
    {"end": "2026-09-15T00:00:00"},
    {"end": "2026-09-15T00:30:00Z"},
    {"end": "2099-09-15T00:00:00Z"},
    {"granularity": 900},
    {"granularity": 3600.0},
    {"max_attempts": 0},
    {"bars_per_split": 1001},
    {"train_batch_bars": 2},
    {"assets": ("BTC-USD", "BTC-USD")},
])
def test_invalid_configuration_is_rejected_before_download(tmp_path, options):
    with pytest.raises(ValueError):
        datasets.prepare_scale_dataset(tmp_path, **options)
    assert not (tmp_path / "manifest.json").exists()


def test_operator_interrupt_is_not_retried_or_swallowed(tmp_path):
    def interrupted(**kwargs):
        raise KeyboardInterrupt("stop")

    with pytest.raises(KeyboardInterrupt):
        small_prepare(tmp_path, fetcher=interrupted)

"""Concurrent provider calls and explicit, lossless reflection input limits."""
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from auto_jev import providers
from auto_jev.providers import JevClient, JevProviderError, ProposerError, make_proposer


QUESTIONS = {"signal": {"type": "noul", "instructions": "Is the signal present?"}}
FAKE_KEY = "local-test-key-never-persist"


def response(index=0):
    return {
        "answers": {"signal": {"type": "noul", "noul": .25}},
        "model": f"actual-model-{index}",
        "usage": {"inputTokens": index + 1, "outputTokens": 2 * (index + 1)},
        "gateway_metadata": {
            "model_version_pinned": bool(index % 2),
            "provider_metadata": {"gateway": {"cost": str((index + 1) / 1000)}},
        },
    }


@pytest.fixture(autouse=True)
def prohibit_unstubbed_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not make a real model request")

    monkeypatch.setattr(providers, "vercel_judge", forbidden)
    monkeypatch.setattr(providers.httpx, "post", forbidden)
    monkeypatch.setattr(providers.subprocess, "Popen", forbidden)


@pytest.mark.parametrize("transport", ["vercel", "typesafe"])
def test_distinct_requests_really_overlap_and_keep_accounting(monkeypatch, tmp_path, transport):
    count = 8
    barrier = threading.Barrier(count)

    def gateway(key, state, questions, *, model, timeout):
        assert key == FAKE_KEY
        assert questions == QUESTIONS
        barrier.wait(timeout=5)
        return response(state["index"])

    def direct(url, *, headers, json, timeout):
        assert url == "https://api.typesafe.ai/v1/systemone"
        assert headers == {"Authorization": "Bearer " + FAKE_KEY}
        assert json["model"] == f"jev-1.13.{json['state']['index']}"
        barrier.wait(timeout=5)
        data = response(json["state"]["index"])
        data.pop("gateway_metadata")
        return httpx.Response(200, json=data)

    monkeypatch.setattr(providers, "vercel_judge", gateway)
    monkeypatch.setattr(providers.httpx, "post", direct)
    client = JevClient(api_key=FAKE_KEY, transport=transport, cache_dir=tmp_path)
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(client.judge, {"index": i}, QUESTIONS, model=f"jev-1.13.{i}") for i in range(count)]
        results = [future.result(timeout=10) for future in futures]

    assert client.stats["calls"] == count
    assert client.stats["cache_hits"] == 0
    assert client.stats["input_tokens"] == sum(range(1, count + 1))
    assert client.stats["output_tokens"] == 2 * sum(range(1, count + 1))
    assert client.stats["elapsed_ms"] > 0
    assert client.stats["cost_usd"] == pytest.approx(sum(range(1, count + 1)) / 1000 if transport == "vercel" else 0)
    assert client.stats["unpriced_calls"] == (count if transport == "typesafe" else 0)
    for index, result in enumerate(results):
        metadata = result["client_metadata"]
        assert metadata["requested_model"] == f"jev-1.13.{index}"
        assert metadata["actual_model"] == f"actual-model-{index}"
        assert metadata["model_version_pinned"] == (bool(index % 2) if transport == "vercel" else True)
        assert not result["cache_hit"]
    paths = list(client.cache_dir.glob("*.json"))
    assert len(paths) == count
    for path in paths:
        text = path.read_text()
        assert FAKE_KEY not in text
        cached = json.loads(text)
        digest = cached.pop("response_digest")
        assert digest == hashlib.sha256(json.dumps(cached, sort_keys=True, allow_nan=False).encode()).hexdigest()
        assert cached["request_hash"] == path.stem
    # Mutating one response must not change another response or client metadata.
    before = copy.deepcopy(client.metadata)
    results[0]["client_metadata"]["gateway"]["injected"] = True
    assert client.metadata == before
    assert "injected" not in results[1]["client_metadata"]["gateway"]


def test_identical_concurrent_cached_requests_share_one_network_call(monkeypatch, tmp_path):
    count = 8
    start = threading.Barrier(count + 1)
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def gateway(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=5)
        return response()

    monkeypatch.setattr(providers, "vercel_judge", gateway)
    client = JevClient(api_key=FAKE_KEY, transport="vercel", cache_dir=tmp_path)

    def judge():
        start.wait(timeout=5)
        return client.judge({"same": True}, QUESTIONS)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(judge) for _ in range(count)]
        try:
            start.wait(timeout=5)
            assert entered.wait(timeout=5)
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert len(calls) == 1
    assert client.stats["calls"] == 1
    assert client.stats["cache_hits"] == count - 1
    assert client.stats["input_tokens"] == 1
    assert client.stats["cost_usd"] == pytest.approx(.001)
    assert sum(not result["cache_hit"] for result in results) == 1
    assert len({result["request_hash"] for result in results}) == 1
    results[0]["answers"]["signal"]["noul"] = .99
    assert all(result["answers"]["signal"]["noul"] == .25 for result in results[1:])
    assert client.judge({"same": True}, QUESTIONS)["answers"]["signal"]["noul"] == .25
    assert len(list(client.cache_dir.glob("*.json"))) == 1


def test_failed_request_releases_same_key_and_preserves_safe_error(monkeypatch, tmp_path):
    calls = []

    def gateway(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise providers.VercelError("upstream accidentally included " + FAKE_KEY)
        return response()

    monkeypatch.setattr(providers, "vercel_judge", gateway)
    client = JevClient(api_key=FAKE_KEY, transport="vercel", cache_dir=tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.judge, {"same": True}, QUESTIONS) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except JevProviderError as error:
                assert FAKE_KEY not in str(error)
                outcomes.append(error)
    assert sum(isinstance(outcome, JevProviderError) for outcome in outcomes) == 1
    assert client.stats["calls"] == 1
    assert len(calls) == 2
    assert client.judge({"same": True}, QUESTIONS)["cache_hit"] is True


def test_concurrent_replay_rejects_corrupt_cache_without_network(tmp_path):
    writer = JevClient(mock=True, cache_dir=tmp_path, cache_namespace="fixed")
    result = writer.judge({"same": True}, QUESTIONS)
    path = writer.cache_dir / (result["request_hash"] + ".json")
    path.write_text("{broken-json")
    reader = JevClient(mock=True, cache_dir=tmp_path, cache_namespace="fixed", cache_only=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(reader.judge, {"same": True}, QUESTIONS) for _ in range(4)]
        for future in futures:
            with pytest.raises(JevProviderError, match="integrity"):
                future.result(timeout=5)
    assert reader.stats["calls"] == reader.stats["cache_hits"] == 0


@pytest.mark.parametrize("kind", ["claude_cli", "azure", "openai", "anthropic"])
def test_oversize_prompt_rejected_before_provider_dispatch(monkeypatch, kind):
    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized input reached provider setup")

    monkeypatch.setattr(providers.shutil, "which", forbidden)
    proposer = make_proposer({"kind": kind, "max_prompt_bytes": 4})
    with pytest.raises(ProposerError, match="6 UTF-8 bytes; max_prompt_bytes is 4"):
        proposer("你好")
    assert proposer.stats["calls"] == 0


def test_default_prompt_limit_is_explicit_and_enforced():
    proposer = make_proposer({"kind": "claude_cli"})
    assert proposer.config["max_prompt_bytes"] == proposer.metadata["max_prompt_bytes"] == 1_000_000
    with pytest.raises(ProposerError, match="1000001 UTF-8 bytes"):
        proposer("a" * 1_000_001)
    assert proposer.stats["calls"] == 0


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "100", None])
def test_invalid_prompt_limits_fail_explicitly(limit):
    with pytest.raises(ProposerError, match="positive integer"):
        make_proposer({"max_prompt_bytes": limit})


def test_exact_utf8_boundary_is_forwarded_without_truncation(monkeypatch):
    import openai

    seen = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == FAKE_KEY
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            seen.append(kwargs["messages"][0]["content"])
            return SimpleNamespace(model="configured", choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))], usage=None)

    monkeypatch.setattr(openai, "OpenAI", Client)
    monkeypatch.setenv("OPENAI_DIRECT_API_KEY", FAKE_KEY)
    proposer = make_proposer({"kind": "openai", "max_prompt_bytes": 4})
    assert proposer("你a") == "{}"
    assert seen == ["你a"]
    assert proposer.stats["calls"] == 1


@pytest.mark.parametrize("prompt", [None, b"hello", "\ud800"])
def test_invalid_prompt_text_is_rejected_without_dispatch(prompt):
    proposer = make_proposer({"kind": "claude_cli"})
    with pytest.raises(ProposerError, match="must be"):
        proposer(prompt)

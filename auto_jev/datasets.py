"""Strict, reproducible hourly Coinbase datasets; no strategy is executed here."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .data import fetch_coinbase
from .storage import atomic_write_json


DEFAULT_END = "2026-09-15T00:00:00Z"
COINBASE_PAGE_BARS = 299
SPLITS = ("train", "validation", "test")
PRICE_FIELDS = ("open", "high", "low", "close", "volume")


class DatasetIntegrityError(ValueError):
    """Downloaded or stored observations do not satisfy the dataset contract."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp(value):
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("end must include an explicit timezone")
        value = parsed.timestamp()
    elif isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("end must include an explicit timezone")
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value:
        raise ValueError("end must identify a finite, whole UTC second")
    return int(value)


def _positive_integer(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _bars(raw, *, start, end, granularity, asset):
    expected = (end - start) // granularity
    if not isinstance(raw, list) or len(raw) != expected:
        actual = len(raw) if isinstance(raw, list) else "not a list"
        raise DatasetIntegrityError(f"{asset}: expected exactly {expected} bars, received {actual}")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise DatasetIntegrityError(f"{asset}: a bar is not an object")
        bar = {}
        for field in ("timestamp",) + PRICE_FIELDS:
            number = item.get(field)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise DatasetIntegrityError(f"{asset}: invalid numeric bar field {field}")
            bar[field] = number
        timestamp = bar["timestamp"]
        if int(timestamp) != timestamp or timestamp % granularity:
            raise DatasetIntegrityError(f"{asset}: bar timestamp is off the UTC hourly grid")
        bar["timestamp"] = int(timestamp)
        if (min(bar[field] for field in ("open", "high", "low", "close")) <= 0
                or bar["volume"] < 0 or bar["low"] > min(bar["open"], bar["close"])
                or bar["high"] < max(bar["open"], bar["close"])):
            raise DatasetIntegrityError(f"{asset}: inconsistent OHLC or volume")
        result.append(bar)
    result.sort(key=lambda bar: bar["timestamp"])
    actual_timestamps = [bar["timestamp"] for bar in result]
    if len(set(actual_timestamps)) != len(actual_timestamps):
        raise DatasetIntegrityError(f"{asset}: duplicate bar timestamps")
    if actual_timestamps != list(range(start, end, granularity)):
        raise DatasetIntegrityError(f"{asset}: missing, extra or non-contiguous hourly bars")
    return result


def _retryable(error):
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, httpx.HTTPStatusError)
        and (error.response.status_code == 429 or 500 <= error.response.status_code < 600)
    )


def _chunk(asset, start, end, granularity, directory, fetcher, max_attempts):
    path = directory / f"{asset}-{start}-{end}.json"
    if path.exists():
        record = json.loads(path.read_text())
        expected = {"asset": asset, "start": start, "end_exclusive": end, "interval_seconds": granularity}
        if any(record.get(key) != value for key, value in expected.items()):
            raise DatasetIntegrityError(f"{asset}: cached download parameters differ")
        if record.get("record_hash") != _digest({k: v for k, v in record.items() if k != "record_hash"}):
            raise DatasetIntegrityError(f"{asset}: cached download integrity check failed")
        record["bars"] = _bars(record.get("bars"), start=start, end=end, granularity=granularity, asset=asset)
        return record
    for attempt in range(1, max_attempts + 1):
        try:
            episode = fetcher(asset=asset, start=_iso(start), end=_iso(end), granularity=granularity)
            break
        except Exception as error:
            if attempt == max_attempts or not _retryable(error):
                raise
            time.sleep(min(8., .5 * 2 ** (attempt - 1)))
    if not isinstance(episode, dict) or episode.get("asset") != asset or episode.get("interval_seconds") != granularity:
        raise DatasetIntegrityError(f"{asset}: downloaded episode identifies another asset or interval")
    bars = _bars(episode.get("bars"), start=start, end=end, granularity=granularity, asset=asset)
    provenance = episode.get("provenance", {})
    if not isinstance(provenance.get("synthetic"), bool) or not isinstance(provenance.get("source"), str):
        raise DatasetIntegrityError(f"{asset}: source and synthetic/real provenance are required")
    if episode.get("news"):
        raise DatasetIntegrityError(f"{asset}: candle download must not supply historical news")
    record = {
        "asset": asset, "start": start, "end_exclusive": end, "interval_seconds": granularity,
        "source_url": f"https://api.exchange.coinbase.com/products/{asset}/candles",
        "request": {"start": _iso(start), "end": _iso(end), "granularity": granularity},
        "attempts": attempt, "retrieved_at": provenance.get("retrieved_at", time.time()),
        "provenance": copy.deepcopy(provenance), "bars": bars, "bars_hash": _digest(bars),
    }
    record["record_hash"] = _digest(record)
    atomic_write_json(path, record)
    time.sleep(.15)
    return record


def _episode(asset, split, bars, granularity, source_records, *, parent_id=None, batch_index=None):
    start, end = bars[0]["timestamp"], bars[-1]["timestamp"] + granularity
    selected_sources = [record for record in source_records if record["start"] < end and record["end_exclusive"] > start]
    identifier = f"scale-{asset}-{split}-{start}-{end}"
    provenance = {
        "synthetic": any(record["provenance"]["synthetic"] for record in selected_sources),
        "source": "; ".join(sorted({record["provenance"]["source"] for record in selected_sources})),
        "source_url": f"https://api.exchange.coinbase.com/products/{asset}/candles",
        "retrieved_at": max(record["retrieved_at"] for record in selected_sources),
        "data_hash": _digest(bars), "source_chunk_hashes": [record["record_hash"] for record in selected_sources],
        "split": split, "start": _iso(start), "end_exclusive": _iso(end), "bar_count": len(bars),
        "missing_intervals": 0, "duplicate_timestamps": 0, "continuous": True,
        "news_coverage": "none", "news_count": 0,
        "experiment_type": "retrospective_historical", "prospective_test": False,
        "execution": {"reset_account_at_episode_start": True, "reset_memory_at_episode_start": True,
                      "executable_decisions": len(bars) - 1, "liquidate_at_episode_end": True},
    }
    if parent_id is not None:
        provenance.update(parent_episode_id=parent_id, batch_index=batch_index, training_batch=True)
    return {"id": identifier, "asset": asset, "interval_seconds": granularity,
            "bars": copy.deepcopy(bars), "news": [], "provenance": provenance}


def verify_manifest(directory):
    """Verify completed output bytes before reusing an existing preparation."""
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("manifest_hash") != _digest({k: v for k, v in manifest.items() if k != "manifest_hash"}):
        raise DatasetIntegrityError("Manifest integrity check failed")
    for name, expected in manifest["files"].items():
        if name not in ("train.json", "train_batches.json", "validation.json", "test.json"):
            raise DatasetIntegrityError("Unexpected dataset file in manifest")
        path = directory / name
        if not path.is_file() or _file_hash(path) != expected["file_sha256"]:
            raise DatasetIntegrityError(f"Dataset file integrity check failed: {name}")
    return manifest


def prepare_scale_dataset(output_dir, *, assets=("BTC-USD", "ETH-USD"), end=DEFAULT_END,
                          bars_per_split=1000, train_batch_bars=50, granularity=3600,
                          max_attempts=3, fetcher=None):
    """Build full train/validation/test episodes and independent train windows.

    All intervals are half-open [start, end). Gaps and duplicate timestamps are
    errors, never repaired. The split spans are shared by both assets; each
    validation/test asset remains one continuous account evaluation episode.
    """
    bars_per_split = _positive_integer(bars_per_split, "bars_per_split", 3)
    train_batch_bars = _positive_integer(train_batch_bars, "train_batch_bars", 3)
    _positive_integer(max_attempts, "max_attempts")
    _positive_integer(granularity, "granularity")
    if granularity != 3600:
        raise ValueError("This dataset requires 3600-second hourly bars")
    if bars_per_split % train_batch_bars:
        raise ValueError("train_batch_bars must divide bars_per_split exactly")
    if isinstance(assets, str):
        raise ValueError("assets must be a sequence of market IDs")
    assets = tuple(assets)
    if not assets or len(set(assets)) != len(assets) or any(not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{2,15}-[A-Z0-9]{2,15}", asset) for asset in assets):
        raise ValueError("assets must contain distinct valid Coinbase market IDs")
    end_epoch = _timestamp(end)
    if end_epoch % granularity or end_epoch > time.time():
        raise ValueError("end must be an already closed UTC hourly boundary")
    start = end_epoch - 3 * bars_per_split * granularity
    configuration = {"assets": list(assets), "start": _iso(start), "end_exclusive": _iso(end_epoch),
                     "bars_per_split": bars_per_split, "train_batch_bars": train_batch_bars,
                     "interval_seconds": granularity}
    output_dir = Path(output_dir)
    if (output_dir / "manifest.json").exists():
        manifest = verify_manifest(output_dir)
        if manifest.get("configuration") != configuration:
            raise DatasetIntegrityError("Existing output belongs to a different dataset configuration")
        return manifest
    output_dir.mkdir(parents=True, exist_ok=True)
    fetcher = fetcher or fetch_coinbase
    split_episodes = {split: [] for split in SPLITS}
    train_batches, all_sources, continuity = [], [], []
    for asset in assets:
        records, full = [], []
        for chunk_start in range(start, end_epoch, COINBASE_PAGE_BARS * granularity):
            chunk_end = min(end_epoch, chunk_start + COINBASE_PAGE_BARS * granularity)
            record = _chunk(asset, chunk_start, chunk_end, granularity, output_dir / ".chunks", fetcher, max_attempts)
            records.append(record)
            full.extend(record["bars"])
        full = _bars(full, start=start, end=end_epoch, granularity=granularity, asset=asset)
        for index, split in enumerate(SPLITS):
            selected = full[index * bars_per_split:(index + 1) * bars_per_split]
            episode = _episode(asset, split, selected, granularity, records)
            split_episodes[split].append(episode)
            if split == "train":
                for batch_index, offset in enumerate(range(0, bars_per_split, train_batch_bars)):
                    batch = selected[offset:offset + train_batch_bars]
                    train_batches.append(_episode(asset, split, batch, granularity, records,
                                                   parent_id=episode["id"], batch_index=batch_index))
        all_sources.extend({k: v for k, v in record.items() if k != "bars"} for record in records)
        continuity.append({"asset": asset, "start": _iso(start), "end_exclusive": _iso(end_epoch),
                           "bars": len(full), "unique_timestamps": len({bar["timestamp"] for bar in full}),
                           "missing_intervals": 0, "duplicate_timestamps": 0, "continuous": True,
                           "cross_split_overlap": False, "full_bars_hash": _digest(full)})
    documents = {f"{split}.json": episodes for split, episodes in split_episodes.items()}
    documents["train_batches.json"] = train_batches
    files = {}
    for name, episodes in documents.items():
        path = output_dir / name
        atomic_write_json(path, episodes)
        files[name] = {"file_sha256": _file_hash(path), "episode_count": len(episodes),
                       "total_bars": sum(len(episode["bars"]) for episode in episodes),
                       "episodes": [{"id": episode["id"], "asset": episode["asset"],
                                     "bars": len(episode["bars"]), "executable_decisions": len(episode["bars"]) - 1,
                                     "start": episode["provenance"]["start"], "end_exclusive": episode["provenance"]["end_exclusive"],
                                     "bars_hash": episode["provenance"]["data_hash"], "episode_hash": _digest(episode)}
                                    for episode in episodes]}
    known_viewed_start = _timestamp("2026-09-19T00:00:00Z")
    known_viewed_end = _timestamp("2026-09-21T00:00:00Z")
    test_start = end_epoch - bars_per_split * granularity
    manifest = {
        "schema": "auto_jev.scale_dataset.v1", "status": "complete", "created_at": _iso(int(time.time())),
        "configuration": configuration, "files": files, "continuity": continuity, "sources": all_sources,
        "data_kind": "synthetic" if all(record["provenance"]["synthetic"] for record in all_sources) else
                     "mixed" if any(record["provenance"]["synthetic"] for record in all_sources) else "real",
        "news": {"coverage": "none", "count": 0, "fabricated": False},
        "experiment": {"type": "retrospective_historical", "prospective_test": False,
                       "strategy_executed_during_preparation": False,
                       "test_is_new_evaluation_split": True,
                       "excluded_known_viewed_window": {"start": _iso(known_viewed_start), "end_exclusive": _iso(known_viewed_end)},
                       "test_overlaps_known_viewed_window": test_start < known_viewed_end and end_epoch > known_viewed_start,
                       "note": "事后历史实验，不是真正未来测试；本次数据准备不运行策略，不评价任何窗口收益。"},
        "execution": {"training": "independent fixed-length windows; reset cash and memory for each window",
                      "validation_and_test": "one continuous account and memory episode per asset and split",
                      "train_batch_bars": train_batch_bars, "train_batch_executable_decisions": train_batch_bars - 1,
                      "validation_test_bars_per_asset": bars_per_split,
                      "validation_test_executable_decisions_per_asset": bars_per_split - 1},
        "hashes": {"algorithm": "sha256", "json_encoding": "UTF-8; sort_keys; ensure_ascii=False; compact separators",
                   "file_hashes_cover": "exact saved file bytes", "bars_hashes_cover": "ordered OHLCV bars"},
    }
    manifest["manifest_hash"] = _digest(manifest)
    atomic_write_json(output_dir / "manifest.json", manifest)
    return verify_manifest(output_dir)

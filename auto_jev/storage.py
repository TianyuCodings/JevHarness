"""运行档案存储：规范 JSON 文件 + 原子写。

目录结构（root/<run_id>/）:
  run.json            运行摘要、配置（已脱敏）、进度、结果、状态
  candidates.json     {hash: {spec, parents, origin, gepa_idx, ...}}
  evaluations.jsonl   每次 候选×窗口 评估的摘要（仅 train/validation）
  events.jsonl        parent 选择、提案、失败、GEPA 回调等事件
  traces/<split>/<hash>__<episode>.json  净值曲线、交易、决策 trace
  frozen.json         冻结 artifact
  holdout.json        冻结后显式封存评估结果（列表）
  sealed_episodes.json  封存窗口原始数据（不通过 HTTP 暴露）
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,79}$")
HASH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{5,79}$")
EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\.]{0,119}$")
SPLITS = ("train", "validation", "holdout")
SEARCH_SPLITS = ("train", "validation")
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|secret|password|passwd|authorization|credential|(^|_)token$|bearer)", re.I
)
_ENV_NAME_SUFFIX = ("_env", "_env_var", "_env_name")


class StoreError(ValueError):
    """非法 ID / 路径 / 状态。"""


class TraceRevisionError(StoreError):
    """The requested summary and decision no longer name the same trace."""


LEGACY_TRACE_LIMIT = 8 * 1024 * 1024
_TRACE_SUMMARY_FIELDS = frozenset({
    'episode_id', 'asset', 'score', 'status', 'error', 'net_profit', 'return_pct',
    'max_drawdown_pct', 'initial_cash', 'final_equity', 'fees_paid', 'fee_bps',
    'slippage_bps', 'decisions', 'completed_decisions', 'market_bars', 'trades',
    'equity_curve', 'cash', 'quantity', 'current_equity',
})


def _trace_binding(stat):
    return {key: getattr(stat, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')}


def _trace_summary(payload, revision):
    summary = {k: json_ready(v) for k, v in payload.items() if k in _TRACE_SUMMARY_FIELDS}
    decisions = payload.get('traces', [])
    rows = []
    for index, record in enumerate(decisions if isinstance(decisions, list) else []):
        row = {k: json_ready(record[k]) for k in
               ('timestamp', 'target', 'trade_index', 'elapsed_ms', 'status', 'phase') if k in record}
        row.update(decision_index=index, node_count=len(record.get('trace', [])))
        rows.append(row)
    summary.update(traces=rows, trace_count=len(rows), trace_revision=revision, trace_paged=True)
    return summary


# ---------------------------------------------------------------- 工具


def scrub_secrets(obj: Any) -> Any:
    """递归移除疑似凭据字段；保留布尔（用于“密钥是否存在”）与 *_env 名称字段。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if (
                _SECRET_KEY_RE.search(ks)
                and not ks.lower().endswith(_ENV_NAME_SUFFIX)
                and not isinstance(v, bool)
                and v is not None
            ):
                out[k] = "<redacted>"
            else:
                out[k] = scrub_secrets(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub_secrets(x) for x in obj]
    if isinstance(obj, str) and len(obj) > 24 and (obj.startswith("sk-") or obj.lower().startswith("bearer ")):
        return "<redacted>"
    return obj


def json_ready(obj: Any, _depth: int = 0) -> Any:
    """转为可 JSON 序列化且浏览器可解析的结构（NaN/Inf -> None）。"""
    if _depth > 40:
        return "<depth-limit>"
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {str(k): json_ready(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [json_ready(v, _depth + 1) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json_ready(dataclasses.asdict(obj), _depth + 1)
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "tolist"):
        try:
            return json_ready(obj.tolist(), _depth + 1)
        except Exception:  # pragma: no cover
            pass
    return repr(obj)[:500]


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(json_ready(data), f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(json_ready(record), ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 崩溃产生的半行，忽略
    return out


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def episode_data_kind(episode: dict) -> str:
    """'synthetic' | 'real' | 'unknown'，来自 episode.provenance / 标记。"""
    prov = episode.get("provenance") or {}
    text = json.dumps({k: v for k, v in prov.items() if k not in ("hash", "synthetic")}, ensure_ascii=False).lower()
    if episode.get("synthetic") is True or prov.get("synthetic") is True:
        return "synthetic"
    if any(w in text for w in ("synthetic", "demo", "simulated", "generated")):
        return "synthetic"
    if any(w in text for w in ("coinbase", "exchange", "rss", "http://", "https://")):
        return "real"
    return "unknown"


def episode_summary(episode: dict, split: str) -> dict:
    bars = episode.get("bars") or []
    return {
        "id": episode.get("id"),
        "asset": episode.get("asset"),
        "split": split,
        "data_kind": episode_data_kind(episode),
        "bars": len(bars),
        "news": len(episode.get("news") or []),
        "start": bars[0].get("timestamp") if bars else None,
        "end": bars[-1].get("timestamp") if bars else None,
        "interval_seconds": episode.get("interval_seconds"),
        "provenance": scrub_secrets(episode.get("provenance") or {}),
    }


def _check(pattern: re.Pattern, value: Any, what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise StoreError(f"非法{what}: {value!r}")
    return value


# ---------------------------------------------------------------- RunStore


class RunStore:
    def __init__(self, root: Path | str = "runs"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 路径
    def run_dir(self, run_id: str, must_exist: bool = True) -> Path:
        _check(RUN_ID_RE, run_id, "run_id")
        p = (self.root / run_id).resolve()
        if p.parent != self.root:
            raise StoreError("run 路径越界")
        if must_exist and not (p / "run.json").exists():
            raise StoreError(f"run 不存在: {run_id}")
        return p

    def _trace_path(self, run_id: str, split: str, cand_hash: str, episode_id: str) -> Path:
        if split not in SPLITS:
            raise StoreError(f"非法 split: {split}")
        _check(HASH_RE, cand_hash, "候选 hash")
        _check(EPISODE_ID_RE, episode_id, "episode id")
        p = (self.run_dir(run_id) / "traces" / split / f"{cand_hash}__{episode_id}.json").resolve()
        if not p.is_relative_to(self.root):
            raise StoreError("trace 路径越界")
        return p

    # ---- run
    def create_run(self, name: str, config: dict, kind: str = "evolution") -> str:
        slug = re.sub(r"[^A-Za-z0-9_\-]+", "-", (name or "run"))[:24].strip("-") or "run"
        run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{slug}-{uuid.uuid4().hex[:6]}"
        d = self.run_dir(run_id, must_exist=False)
        d.mkdir(parents=True, exist_ok=False)
        atomic_write_json(
            d / "run.json",
            {
                "run_id": run_id,
                "name": name,
                "kind": kind,
                "status": "created",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "config": scrub_secrets(config),
                "progress": {},
                "result": None,
                "error": None,
                "frozen_hash": None,
                "holdout_evaluations": 0,
            },
        )
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> dict:
        d = self.run_dir(run_id)
        run = read_json(d / "run.json", {})
        if "config" in fields:
            fields["config"] = scrub_secrets(fields["config"])
        run.update(fields)
        run["updated_at"] = now_iso()
        atomic_write_json(d / "run.json", run)
        return run

    def get_run(self, run_id: str) -> dict:
        return scrub_secrets(read_json(self.run_dir(run_id) / "run.json", {}))

    def list_runs(self) -> list[dict]:
        runs = []
        for p in self.root.iterdir():
            if not p.is_dir() or not RUN_ID_RE.match(p.name) or not (p / "run.json").exists():
                continue
            try:
                r = read_json(p / "run.json", {})
            except json.JSONDecodeError:
                continue
            res = r.get("result") or {}
            runs.append(
                {
                    "run_id": r.get("run_id", p.name),
                    "name": r.get("name"),
                    "kind": r.get("kind"),
                    "status": r.get("status"),
                    "created_at": r.get("created_at"),
                    "updated_at": r.get("updated_at"),
                    "data_kind": (r.get("config") or {}).get("data_kind"),
                    "jev_mock": (r.get("config") or {}).get("jev_mock"),
                    "proposer": (r.get("config") or {}).get("proposer_label"),
                    "best_score": res.get("best_score"),
                    "n_candidates": len(res.get("candidates") or []),
                    "frozen_hash": r.get("frozen_hash"),
                    "error": r.get("error"),
                }
            )
        runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return runs

    # ---- 候选
    def save_candidate(
        self,
        run_id: str,
        spec: dict,
        cand_hash: str,
        *,
        parents: list[str] | None = None,
        origin: str = "proposal",
        gepa_idx: int | None = None,
        extra: dict | None = None,
    ) -> dict:
        _check(HASH_RE, cand_hash, "候选 hash")
        d = self.run_dir(run_id)
        path = d / "candidates.json"
        cands = read_json(path, {})
        rec = cands.get(cand_hash) or {
            "hash": cand_hash,
            "spec": spec,
            "parents": list(parents or []),
            "origin": origin,
            "gepa_idx": None,
            "created_at": now_iso(),
            "seq": len(cands),
        }
        if parents or gepa_idx is not None:
            occurrence = {"parents": list(parents or []), "gepa_idx": gepa_idx, "origin": origin}
            occurrences = rec.setdefault("occurrences", [])
            if occurrence not in occurrences:
                occurrences.append(occurrence)
                self.append_event(run_id, "candidate_occurrence", {"candidate": cand_hash, **occurrence})
        if parents:
            rec["parents"] = sorted(set(rec.get("parents", []) + list(parents)) - {cand_hash})
        if gepa_idx is not None:
            rec["gepa_idx"] = int(gepa_idx)
            rec["origin"] = origin
        if extra:
            rec.update(extra)
        cands[cand_hash] = rec
        atomic_write_json(path, cands)
        return rec

    def list_candidates(self, run_id: str) -> list[dict]:
        cands = list(read_json(self.run_dir(run_id) / "candidates.json", {}).values())
        cands.sort(key=lambda c: (c.get("gepa_idx") is None, c.get("gepa_idx") or 0, c.get("seq", 0)))
        return cands

    def get_candidate(self, run_id: str, cand_hash: str) -> dict:
        _check(HASH_RE, cand_hash, "候选 hash")
        rec = read_json(self.run_dir(run_id) / "candidates.json", {}).get(cand_hash)
        if rec is None:
            raise StoreError(f"候选不存在: {cand_hash}")
        return rec

    # ---- 评估 / trace / 事件
    def append_evaluation(self, run_id: str, record: dict) -> None:
        if record.get("split") not in SEARCH_SPLITS:
            raise StoreError("evaluations.jsonl 仅记录 train/validation；封存结果走 append_holdout")
        record = dict(record)
        record.setdefault("timestamp", time.time())
        append_jsonl(self.run_dir(run_id) / "evaluations.jsonl", record)

    def list_evaluations(self, run_id: str, *, candidate: str | None = None, split: str | None = None) -> list[dict]:
        rows = read_jsonl(self.run_dir(run_id) / "evaluations.jsonl")
        if candidate is not None:
            rows = [r for r in rows if r.get("candidate") == candidate]
        if split is not None:
            rows = [r for r in rows if r.get("split") == split]
        return rows

    def save_trace(self, run_id: str, split: str, cand_hash: str, episode_id: str, payload: dict) -> None:
        """Write complete JSON plus a version-bound, byte-addressable sidecar.

        Only one decision is serialized at a time. The sidecar contains no node
        contexts, and publishing it last is the commit point for paged readers.
        """
        path = self._trace_path(run_id, split, cand_hash, episode_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.write.lock').open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._save_indexed_trace(path, payload)

    def _save_indexed_trace(self, path: Path, payload: dict) -> None:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.trace-', suffix='.json')
        digest = hashlib.sha256()
        offsets = []
        try:
            with os.fdopen(fd, 'wb') as stream:
                def write(raw):
                    stream.write(raw)
                    digest.update(raw)

                def encode(value):
                    return json.dumps(json_ready(value), ensure_ascii=False, separators=(',', ':')).encode('utf-8')

                write(b'{')
                for number, (name, value) in enumerate(payload.items()):
                    if number:
                        write(b',')
                    write(encode(str(name)) + b':')
                    if name == 'traces' and isinstance(value, list):
                        write(b'[')
                        for index, decision in enumerate(value):
                            if index:
                                write(b',')
                            raw = encode(decision)
                            offsets.append([stream.tell(), len(raw)])
                            write(raw)
                        write(b']')
                    else:
                        write(encode(value))
                write(b'}')
                stream.flush()
                os.fsync(stream.fileno())
                binding = _trace_binding(os.fstat(stream.fileno()))
            revision = digest.hexdigest()
            index = {'version': 1, 'binding': binding, 'revision': revision,
                     'decisions': offsets, 'summary': _trace_summary(payload, revision)}
            os.replace(temporary, path)
            atomic_write_json(path.with_suffix('.index.json'), index)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load_trace(self, run_id: str, split: str, cand_hash: str, episode_id: str) -> dict:
        p = self._trace_path(run_id, split, cand_hash, episode_id)
        if not p.exists():
            raise StoreError("trace 不存在")
        return read_json(p, {})

    def _paged_trace(self, path: Path):
        """Return a matching open descriptor and index, or a small legacy value."""
        if not path.exists():
            raise StoreError('Trace does not exist')
        manifest_path = path.with_suffix('.index.json')
        manifest = read_json(manifest_path, None)
        stream = path.open('rb')
        try:
            binding = _trace_binding(os.fstat(stream.fileno()))
            if manifest is not None:
                if manifest.get('version') != 1 or manifest.get('binding') != binding:
                    raise TraceRevisionError('Trace is being replaced; refresh its summary')
                return stream, manifest, None
            if binding['st_size'] > LEGACY_TRACE_LIMIT:
                raise StoreError('Large legacy trace has no paging index; re-index before opening it in the dashboard')
            payload = json.load(stream)
            if _trace_binding(os.fstat(stream.fileno())) != binding:
                raise TraceRevisionError('Trace changed while reading; refresh its summary')
            revision = 'legacy-' + hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
            return stream, {'revision': revision, 'binding': binding}, payload
        except BaseException:
            stream.close()
            raise

    def load_trace_summary(self, run_id: str, split: str, cand_hash: str, episode_id: str) -> dict:
        path = self._trace_path(run_id, split, cand_hash, episode_id)
        stream, manifest, legacy = self._paged_trace(path)
        try:
            if legacy is not None:
                return {**legacy, 'trace_revision': manifest['revision'], 'trace_paged': False,
                        'trace_count': len(legacy.get('traces', []))}
            return manifest['summary']
        finally:
            stream.close()

    def load_trace_decision(self, run_id: str, split: str, cand_hash: str, episode_id: str,
                            decision_index: int, revision: str | None = None) -> dict:
        if isinstance(decision_index, bool) or not isinstance(decision_index, int) or decision_index < 0:
            raise StoreError('Invalid decision index')
        path = self._trace_path(run_id, split, cand_hash, episode_id)
        stream, manifest, legacy = self._paged_trace(path)
        try:
            if revision is not None and revision != manifest['revision']:
                raise TraceRevisionError('Trace revision changed; refresh its summary before selecting a decision')
            if legacy is not None:
                records = legacy.get('traces', [])
                if decision_index >= len(records):
                    raise StoreError('Decision does not exist')
                decision = records[decision_index]
            else:
                offsets = manifest.get('decisions', [])
                if decision_index >= len(offsets):
                    raise StoreError('Decision does not exist')
                offset, length = offsets[decision_index]
                if (not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or
                        length < 1 or offset + length > manifest['binding']['st_size']):
                    raise StoreError('Invalid trace byte index')
                stream.seek(offset)
                decision = json.loads(stream.read(length))
                if _trace_binding(os.fstat(stream.fileno())) != manifest['binding']:
                    raise TraceRevisionError('Trace changed while reading; refresh its summary')
            return {'trace_revision': manifest['revision'], 'decision_index': decision_index, 'decision': decision}
        finally:
            stream.close()

    def append_event(self, run_id: str, kind: str, payload: dict | None = None) -> None:
        append_jsonl(
            self.run_dir(run_id) / "events.jsonl",
            {"timestamp": time.time(), "kind": kind, "payload": scrub_secrets(payload or {})},
        )

    def list_events(self, run_id: str, kind: str | None = None) -> list[dict]:
        rows = read_jsonl(self.run_dir(run_id) / "events.jsonl")
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return rows

    # ---- 冻结 / 封存
    def save_frozen(self, run_id: str, artifact: dict) -> None:
        atomic_write_json(self.run_dir(run_id) / "frozen.json", artifact)
        self.update_run(run_id, frozen_hash=artifact.get("spec_hash"))

    def get_frozen(self, run_id: str) -> dict | None:
        return read_json(self.run_dir(run_id) / "frozen.json", None)

    def save_sealed_episodes(self, run_id: str, episodes: list[dict]) -> None:
        atomic_write_json(self.run_dir(run_id) / "sealed_episodes.json", episodes)

    def load_sealed_episodes(self, run_id: str) -> list[dict]:
        return read_json(self.run_dir(run_id) / "sealed_episodes.json", [])

    def append_holdout(self, run_id: str, result: dict) -> None:
        d = self.run_dir(run_id)
        rows = read_json(d / "holdout.json", [])
        rows.append(result)
        atomic_write_json(d / "holdout.json", rows)
        self.update_run(run_id, holdout_evaluations=len(rows))

    def list_holdout(self, run_id: str) -> list[dict]:
        return read_json(self.run_dir(run_id) / "holdout.json", [])

    # ---- 视图
    def matrix(self, run_id: str, split: str = "validation") -> dict:
        """候选 × 窗口 矩阵（仅搜索 split；封存集永不出现在此视图）。"""
        if split not in SEARCH_SPLITS:
            raise StoreError("matrix 仅提供 train/validation；封存集不进入搜索视图")
        run = self.get_run(run_id)
        result = run.get("result") or {}
        cands = {c["hash"]: c for c in self.list_candidates(run_id)}
        selected: dict[str, int] = {}
        for ev in self.list_events(run_id, "parent_selected"):
            h = (ev.get("payload") or {}).get("candidate")
            if h:
                selected[h] = selected.get(h, 0) + 1
        latest: dict[tuple[str, str], dict] = {}
        episodes: list[str] = []
        for r in self.list_evaluations(run_id, split=split):
            key = (r.get("candidate"), r.get("episode_id"))
            latest[key] = r
            if r.get("episode_id") not in episodes:
                episodes.append(r.get("episode_id"))
        kinds = {e.get("id"): e.get("data_kind") for e in (run.get("config") or {}).get("episodes", [])}
        rows = []
        for h, c in cands.items():
            cells = {}
            for ep in episodes:
                r = latest.get((h, ep))
                if r is None:
                    continue
                cells[ep] = {
                    k: r.get(k)
                    for k in ("score", "status", "return_pct", "max_drawdown_pct", "n_trades", "error", "elapsed_ms", "jev_calls")
                }
            if not cells:
                continue
            ok = [v["score"] for v in cells.values() if v.get("status") == "ok" and v.get("score") is not None]
            rows.append(
                {
                    "hash": h,
                    "gepa_idx": c.get("gepa_idx"),
                    "origin": c.get("origin"),
                    "parents": c.get("parents", []),
                    "name": (c.get("spec") or {}).get("name"),
                    "val_aggregate": c.get("val_aggregate"),
                    "is_gepa_best": h == result.get("best_hash"),
                    "is_frozen": h == run.get("frozen_hash"),
                    "selected_count": selected.get(h, 0),
                    "mean_score": sum(v["score"] for v in cells.values() if v.get("score") is not None) / len(cells) if cells else None,
                    "n_ok": len(ok),
                    "n_failed": len(cells) - len(ok),
                    "cells": cells,
                }
            )
        champions = {}
        for ep in episodes:
            scored = [(r["hash"], r["cells"][ep]["score"]) for r in rows
                      if ep in r["cells"] and r["cells"][ep].get("status") == "ok"
                      and r["cells"][ep].get("score") is not None]
            best = max((score for _, score in scored), default=None)
            champions[ep] = [h for h, score in scored if score == best]
        return {
            "run_id": run_id,
            "split": split,
            "episodes": [{"id": ep, "data_kind": kinds.get(ep, run.get("config", {}).get("data_kind"))} for ep in episodes],
            "rows": rows,
            "champions": champions,
            "gepa_best": result.get("best_hash"),
            "frozen": run.get("frozen_hash"),
            "note": "每个窗口独立重置资金后回放；均值不是连续复利收益。冠军=该窗口净利润最高的候选，"
            "GEPA 实际父代选择见 selected_count 与事件，不在此重算。",
        }

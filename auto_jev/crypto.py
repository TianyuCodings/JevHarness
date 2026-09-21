"""Spot backtest harness for auto_jev pipeline specs (no lookahead, full cost accounting)."""
from __future__ import annotations

import copy
import math
from typing import Any

from .runtime import PipelineExecutionError, PipelineRuntime
from .observations import build_observation

PRICE_FIELDS = ("open", "high", "low", "close")
MAX_BPS = 1000.0
BASELINE_EXPRESSIONS = {
    "cash": "0.0",
    "buy_hold": "1.0",
    "momentum": "1.0 if obs['closes'][-1] > mean(obs['closes'][-10:]) else 0.0",
}


class EpisodeExecutionError(PipelineExecutionError):
    """An incomplete episode with its ledger and the original system cause."""

    def __init__(self, message, *, cause, partial_result):
        # The runtime constructor is intentionally not part of this adapter's
        # interface; preserve its public cause/partial_result contract directly.
        ValueError.__init__(self, message)
        self.cause = cause
        seen = set()
        while isinstance(self.cause, PipelineExecutionError) and id(self.cause) not in seen:
            seen.add(id(self.cause))
            underlying = getattr(self.cause, "cause", None)
            if underlying is None:
                break
            self.cause = underlying
        self.causes = tuple(getattr(cause, "causes", (self.cause,)))
        self.partial_result = copy.deepcopy(partial_result)


def _run_evidence(result, memory_before):
    """Keep the runtime evidence without depending on scheduling details."""
    result = result if isinstance(result, dict) else {}
    return copy.deepcopy({
        "output": result.get("output"),
        "memory": result.get("memory", memory_before),
        "memory_before": result.get("memory_before", memory_before),
        "nodes": result.get("nodes", {}),
        "trace": result.get("trace", []),
        "elapsed_ms": result.get("elapsed_ms"),
        "execution": result.get("execution"),
        "finalization": result.get("finalization"),
    })


def _error_evidence(error):
    return {"type": type(error).__name__, "message": str(error)}


def _num(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _cost_config(initial_cash, fee_bps, slippage_bps):
    cash = _num(initial_cash, "initial_cash")
    if cash <= 0:
        raise ValueError("initial_cash must be positive")
    fee, slip = _num(fee_bps, "fee_bps"), _num(slippage_bps, "slippage_bps")
    if not 0 <= fee <= MAX_BPS or not 0 <= slip <= MAX_BPS:
        raise ValueError(f"fee_bps and slippage_bps must lie in [0, {MAX_BPS:g}]")
    return cash, fee / 1e4, slip / 1e4


def _normalize_bars(episode):
    raw = episode.get("bars")
    if not isinstance(raw, list) or len(raw) < 3:
        raise ValueError("episode needs a list of at least 3 bars")
    interval = _num(episode.get("interval_seconds"), "interval_seconds")
    if interval <= 0:
        raise ValueError("interval_seconds must be positive")
    bars = []
    for k, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"bars[{k}] must be a dict")
        bar = {f: _num(item.get(f), f"bars[{k}].{f}") for f in ("timestamp",) + PRICE_FIELDS + ("volume",)}
        if min(bar[f] for f in PRICE_FIELDS) <= 0 or bar["volume"] < 0:
            raise ValueError(f"bars[{k}] has non-positive prices or negative volume")
        if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]):
            raise ValueError(f"bars[{k}] violates low <= open/close <= high")
        if bars and bar["timestamp"] <= bars[-1]["timestamp"]:
            raise ValueError(f"bars[{k}] timestamp is not strictly increasing")
        if bars and bar["timestamp"] < bars[-1]["timestamp"] + interval:
            raise ValueError(f"bars[{k}] opens before bars[{k - 1}] closes (decision time)")
        bars.append(bar)
    return bars, interval


def _normalize_news(episode):
    raw = episode.get("news") or []
    if not isinstance(raw, list):
        raise ValueError("news must be a list")
    feed = []
    for k, item in enumerate(raw):
        if not isinstance(item, dict) or "id" not in item:
            raise ValueError(f"news[{k}] must be a dict with an id")
        available = _num(item.get("available_at"), f"news[{k}].available_at")
        if item.get("published_at") is not None: _num(item["published_at"], f"news[{k}].published_at")
        feed.append((available, k, item))
    feed.sort(key=lambda entry: entry[:2])
    return feed


def _parse_target(output):
    value = output.get("target") if isinstance(output, dict) else output
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"pipeline output must be a finite float in [0, 1] or {{'target': x}}, got {output!r}")
    return float(value)


class _Book:
    def __init__(self, cash, fee, slip):
        self.cash, self.qty, self.fee, self.slip = cash, 0.0, fee, slip
        self.fees_paid, self.trades = 0.0, []

    def equity(self, price):
        return _num(self.cash + self.qty * price, "account equity")

    def _record(self, ts, side, px, qty, fee, mark, reason):
        self.fees_paid += fee
        self.trades.append({"timestamp": ts, "side": side, "price": px, "quantity": qty, "fee": fee, "cash": self.cash,
                            "position": self.qty, "equity": self.equity(mark), "reason": reason})
        return len(self.trades) - 1

    def rebalance(self, target, price, ts):
        equity = self.equity(price)
        notional = _num(target * equity - self.qty * price, "target difference")
        if abs(notional) <= 8 * math.ulp(equity):
            return None
        reason = f"target={target:.6g}"
        if notional > 0:
            px = _num(price * (1 + self.slip), "execution price")
            unit_cost = _num(px * (1 + self.fee), "unit cost")
            denominator = _num(price + target * (unit_cost-price), "rebalance denominator")
            qty = min(_num(notional/denominator, "quantity"), _num(self.cash/unit_cost, "affordable quantity"))
            fee = _num(qty * px * self.fee, "fee")
            cash = _num(self.cash-qty*unit_cost, "cash after buy")
            position = _num(self.qty+qty, "position after buy")
            side = "buy"
        else:
            px = _num(price * (1-self.slip), "execution price")
            unit_proceeds = _num(px * (1-self.fee), "unit proceeds")
            denominator = _num(price-target*(price-unit_proceeds), "rebalance denominator")
            qty = min(_num(-notional/denominator, "quantity"), self.qty)
            fee = _num(qty*px*self.fee, "fee")
            cash = _num(self.cash+qty*unit_proceeds, "cash after sell")
            position = _num(self.qty-qty, "position after sell")
            side = "sell"
        if qty <= 0 or cash < -8*math.ulp(equity) or position < 0:
            raise ValueError("transaction cannot be represented without borrowing")
        self.cash, self.qty = max(cash, 0.), position
        return self._record(ts, side, px, qty, fee, price, reason)

    def liquidate(self, close, ts):
        if self.qty <= 0:
            return None
        px = _num(close * (1 - self.slip), "liquidation price")
        qty = self.qty
        fee = _num(qty * px * self.fee, "liquidation fee")
        self.cash = _num(self.cash + qty * px - fee, "final cash")
        self.qty = 0.0
        return self._record(ts, "sell", px, qty, fee, close, "liquidation")


def evaluate_episode(spec, episode, jev, *, initial_cash=10000.0, fee_bps=10.0, slippage_bps=5.0, capture_traces=True):
    cash0, fee, slip = _cost_config(initial_cash, fee_bps, slippage_bps)
    if not isinstance(episode, dict):
        raise ValueError("episode must be a dict")
    bars, interval = _normalize_bars(episode)
    feed = _normalize_news(episode)
    runtime = PipelineRuntime(spec, jev)
    book = _Book(cash0, fee, slip)
    memory: dict = {}
    traces, curve, closes, latest = [], [], [], {}
    peak, max_dd, ptr = cash0, 0.0, 0

    def mark(ts, equity):
        nonlocal peak, max_dd
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0
        max_dd = max(max_dd, dd)
        curve.append({"timestamp": ts, "equity": equity, "drawdown_pct": dd})

    phase, snapshot, result, target = "observation", None, None, None
    memory_before = {}
    decision_time = bars[0]["timestamp"] + interval
    completed_decisions = 0
    book_before = (book.cash, book.qty, book.fees_paid, len(book.trades))
    try:
        for i in range(len(bars) - 1):
            phase, snapshot, result, target = "observation", None, None, None
            memory_before = copy.deepcopy(memory)
            book_before = (book.cash, book.qty, book.fees_paid, len(book.trades))
            bar, nxt = bars[i], bars[i + 1]
            closes.append(bar["close"])
            decision_time = bar["timestamp"] + interval
            if nxt["timestamp"] < decision_time:
                raise ValueError(f"bars[{i + 1}] opens before decision time of bars[{i}]")
            while ptr < len(feed) and feed[ptr][0] <= decision_time:
                latest[feed[ptr][2]["id"]] = feed[ptr][2]
                ptr += 1
            visible = sorted(latest.values(), key=lambda n: (n["available_at"], n.get("published_at") or n["available_at"]))
            equity_now = book.equity(bar["close"])
            mark(decision_time, equity_now)
            obs = build_observation(episode.get("asset"), bars[:i+1], visible,
                {"cash": book.cash, "quantity": book.qty, "equity": equity_now}, decision_time)
            # Keep failure evidence even when the successful return omits traces.
            snapshot = copy.deepcopy(obs)
            phase = "runtime"
            result = runtime.run(obs, copy.deepcopy(memory_before))
            phase = "output"
            if not isinstance(result, dict) or "output" not in result:
                raise ValueError("PipelineRuntime.run must return a dict containing 'output'")
            target = _parse_target(result["output"])
            proposed_memory = result.get("memory")
            proposed_memory = {} if proposed_memory is None else proposed_memory
            if not isinstance(proposed_memory, dict):
                raise ValueError("pipeline memory must be a dict")
            phase = "execution"
            trade_index = book.rebalance(target, nxt["open"], nxt["timestamp"])
            memory = copy.deepcopy(proposed_memory)
            traces.append({"timestamp": decision_time, "obs": snapshot,
                           **_run_evidence(result, memory_before), "memory": copy.deepcopy(memory),
                           "status": "ok", "target": target, "trade_index": trade_index})
            completed_decisions += 1
        phase = "liquidation"
        book_before = (book.cash, book.qty, book.fees_paid, len(book.trades))
        memory_before = copy.deepcopy(memory)
        close_time = bars[-1]["timestamp"] + interval
        book.liquidate(bars[-1]["close"], close_time)
        mark(close_time, book.cash)
        phase = "scoring"
        net_profit = book.cash - cash0
        score = _num(net_profit / cash0, "score")
    except Exception as error:
        runtime_partial = getattr(error, "partial_result", None)
        run_partial = runtime_partial if isinstance(runtime_partial, dict) else result
        failed_decision = None
        if phase not in ("liquidation", "scoring"):
            # Rebalance may fail after arithmetic begins. Do not present that
            # attempted mutation as a successfully executed transaction.
            book.cash, book.qty, book.fees_paid, trade_count = book_before
            del book.trades[trade_count:]
            memory = copy.deepcopy(memory_before)
            failed_decision = {"timestamp": decision_time, "obs": snapshot,
                **_run_evidence(run_partial, memory_before), "memory": copy.deepcopy(memory_before),
                "status": "error", "phase": phase, "target": target, "trade_index": None,
                "error": _error_evidence(error)}
            traces.append(failed_decision)
        elif phase == "liquidation":
            book.cash, book.qty, book.fees_paid, trade_count = book_before
            del book.trades[trade_count:]
        partial = {
            "episode_id": episode.get("id"), "asset": episode.get("asset"), "status": "error",
            "market_bars": copy.deepcopy(bars),
            "score": None, "net_profit": None, "return_pct": None, "final_equity": None,
            "initial_cash": cash0, "fees_paid": book.fees_paid, "fee_bps": fee * 1e4,
            "slippage_bps": slip * 1e4, "cash": book.cash, "quantity": book.qty,
            "current_equity": curve[-1]["equity"] if curve else cash0, "max_drawdown_pct": max_dd,
            "decisions": len(traces), "completed_decisions": completed_decisions,
            "trades": book.trades, "equity_curve": curve, "traces": traces, "memory": memory,
            "failed_decision": failed_decision, "runtime_partial_result": run_partial,
            "failure": {"phase": phase, **_error_evidence(error)},
        }
        raise EpisodeExecutionError(f"Episode execution failed during {phase}: {error}",
                                    cause=error, partial_result=partial) from error
    return {
        "episode_id": episode.get("id"), "asset": episode.get("asset"), "score": score,
        "market_bars": copy.deepcopy(bars),
        "net_profit": net_profit, "return_pct": 100.0 * net_profit / cash0, "max_drawdown_pct": max_dd,
        "initial_cash": cash0, "final_equity": book.cash, "fees_paid": book.fees_paid, "fee_bps": fee * 1e4,
        "slippage_bps": slip * 1e4, "decisions": len(bars) - 1, "trades": book.trades,
        "equity_curve": curve, "traces": traces if capture_traces else [],
    }


def _baseline_spec(name, expression):
    return {"version": 1, "name": f"baseline_{name}", "jev_model": "typesafe-ai/jev", "nodes": [], "output": expression}


def evaluate_baselines(episode, **cost_config):
    cost_config.setdefault("capture_traces", False)
    return {name: evaluate_episode(_baseline_spec(name, expr), episode, None, **cost_config)
            for name, expr in BASELINE_EXPRESSIONS.items()}

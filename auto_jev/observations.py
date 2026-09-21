"""The same bounded observation contract for historical and forward execution."""
import copy

OBSERVATION_CONFIG = {"lookback_bars": 72, "news_limit": 20, "news_chars": 1500}


def build_observation(asset, bars, news, portfolio, decision_time):
    history = copy.deepcopy(bars[-OBSERVATION_CONFIG["lookback_bars"]:])
    latest = {}
    for item in sorted(news, key=lambda n: n["available_at"]):
        if item["available_at"] <= decision_time:
            latest[item["id"]] = item
    visible = []
    for item in sorted(latest.values(), key=lambda n: n["available_at"])[-OBSERVATION_CONFIG["news_limit"]:]:
        content = str(item.get("content", ""))
        visible.append({"id": item["id"], "headline": item.get("headline", item.get("title", "")),
            "content": content[:OBSERVATION_CONFIG["news_chars"]], "source": item.get("source"),
            "published_at": item.get("published_at"), "available_at": item["available_at"],
            "input_truncated": bool(item.get("input_truncated") or item.get("content_truncated") or len(content) > OBSERVATION_CONFIG["news_chars"])})
    return {"asset": asset, "bar": history[-1], "history": history, "closes": [b["close"] for b in history],
        "news": visible, "timestamp": decision_time,
        "portfolio": {k: portfolio[k] for k in ("cash", "quantity", "equity")}}

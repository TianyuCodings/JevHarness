"""Command-line entry points. Credentials are loaded only at the application edge."""
import argparse
import json
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

from .storage import RunStore, atomic_write_json


def read_episodes(path):
    data = json.loads(Path(path).read_text())
    return data if isinstance(data, list) else [data]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auto_Jev：通用进化与加密现货研究")
    parser.add_argument("--runs", default="runs", help="运行档案目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查配置存在，不显示密钥")
    demo = sub.add_parser("demo", help="合成数据＋模拟模型的完整 GEPA 演示")
    demo.add_argument("--budget", type=int, default=24)
    evolve = sub.add_parser("evolve", help="使用真实 Jev 和本机或 API 反思模型")
    evolve.add_argument("--train", required=True)
    evolve.add_argument("--validation", required=True)
    evolve.add_argument("--proposer-config", default="configs/claude_local.json")
    evolve.add_argument("--seed-pipeline")
    evolve.add_argument("--name", default="crypto")
    evolve.add_argument("--budget", type=int, default=None, help="GEPA episode 评估预算；未指定轮数时默认24")
    evolve.add_argument("--rounds", type=int, default=None, help="实际进化轮数；包含完整父子比较，不等于episode预算")
    evolve.add_argument("--resume-run", default=None, help="从同一运行的GEPA检查点恢复；输入配置必须一致")
    evolve.add_argument("--initial-cash", type=float, default=10000)
    evolve.add_argument("--fee-bps", type=float, default=10)
    evolve.add_argument("--slippage-bps", type=float, default=5)
    evolve.add_argument("--mock-jev", action="store_true")
    evolve.add_argument("--reflection-batch-size", type=int, default=None, help="默认对全部训练样本完整反思；显式指定时仅改变样本数，不裁剪样本轨迹")
    evolve.add_argument("--max-prompt-bytes", type=int, default=None, help="完整反思提示词容量；超限归档并报错，不截断")
    experiment = sub.add_parser("experiment", help="持久化进化任务：选定、冻结后自动评估封存测试集")
    experiment.add_argument("--config", required=True)
    experiment.add_argument("--resume", action="store_true")
    market = sub.add_parser("collect-market")
    market.add_argument("--asset", default="BTC-USD")
    market.add_argument("--start", required=True)
    market.add_argument("--end", required=True)
    market.add_argument("--granularity", type=int, default=3600)
    market.add_argument("--output", required=True)
    news = sub.add_parser("collect-news")
    news.add_argument("--url", default="https://cointelegraph.com/rss")
    news.add_argument("--output", default="data/news.jsonl")
    freeze = sub.add_parser("freeze")
    freeze.add_argument("run_id")
    freeze.add_argument("--output")
    evaluate = sub.add_parser("evaluate", help="仅执行冻结版本，不启动进化器")
    evaluate.add_argument("--artifact", required=True)
    evaluate.add_argument("--data", required=True)
    evaluate.add_argument("--output", default="artifacts/evaluation.json")
    evaluate.add_argument("--mock-jev", action="store_true")
    paper = sub.add_parser("paper", help="冻结版本的纸面交易；只用公开报价")
    paper.add_argument("--artifact", required=True)
    paper.add_argument("--asset", default="BTC-USD")
    paper.add_argument("--state", default="artifacts/paper.json")
    paper.add_argument("--rss-url", default="https://cointelegraph.com/rss")
    paper.add_argument("--steps", type=int, default=1)
    paper.add_argument("--interval", type=float, default=60)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    load_dotenv(Path.cwd() / ".env", override=False)

    if args.command == "doctor":
        print(json.dumps({"claude_available": bool(shutil.which("claude")),
            "jev_transport": "vercel" if os.getenv("AI_GATEWAY_API_KEY") else "typesafe" if os.getenv("TYPESAFE_API_KEY") else "unconfigured",
            "credentials_present": {key: bool(os.getenv(key)) for key in ("AI_GATEWAY_API_KEY", "TYPESAFE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")}}, indent=2))
        return
    if args.command == "serve":
        import uvicorn
        from .server import create_app
        uvicorn.run(create_app(args.runs), host="127.0.0.1", port=args.port)
        return
    if args.command == "experiment":
        from .experiment import run_experiment
        result = run_experiment(args.config, resume=args.resume)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "collect-market":
        from .data import fetch_coinbase, save_episode
        episode = fetch_coinbase(args.asset, args.start, args.end, args.granularity)
        save_episode(episode, args.output)
        print(json.dumps({"output": args.output, "bars": len(episode["bars"])}))
        return
    if args.command == "collect-news":
        from .data import fetch_rss, append_received_news
        records = fetch_rss(args.url)
        written = append_received_news(args.output, records)
        print(json.dumps({"output": args.output, "received_articles": len(records), "new_revisions": written}))
        return
    if args.command == "freeze":
        from .evolution import freeze_run
        artifact = freeze_run(RunStore(args.runs), args.run_id)
        if args.output:
            atomic_write_json(Path(args.output), artifact)
        print(json.dumps({"run_id": args.run_id, "spec_hash": artifact["spec_hash"], "output": args.output}, ensure_ascii=False))
        return
    if args.command == "evaluate":
        from .frozen import evaluate_frozen
        from .providers import JevClient
        artifact = json.loads(Path(args.artifact).read_text())
        result = evaluate_frozen(artifact, read_episodes(args.data), JevClient(mock=args.mock_jev, transport=artifact.get("jev_metadata",{}).get("transport","auto") if not args.mock_jev else "auto", cache_dir=".cache/jev"))
        atomic_write_json(Path(args.output), result)
        print(json.dumps({"output": args.output, "episodes": len(result), "net_profits": [r.get("net_profit") for r in result]}))
        return
    if args.command == "paper":
        import time
        from .paper import paper_step
        from .providers import JevClient
        if args.steps < 1 or args.interval <= 0:
            parser.error("steps 和 interval 必须为正数")
        artifact = json.loads(Path(args.artifact).read_text())
        transport = artifact.get("jev_metadata", {}).get("transport", "auto")
        jev = JevClient(transport=transport, cache_dir=".cache/jev")
        for step in range(args.steps):
            result = paper_step(artifact, jev, asset=args.asset, state_path=args.state, rss_url=args.rss_url)
            print(json.dumps({"skipped": result["skipped"], "target": result.get("target"), "state": args.state}, ensure_ascii=False), flush=True)
            if step + 1 < args.steps:
                time.sleep(args.interval)
        return

    from .evolution import run_evolution, demo_proposer
    from .providers import JevClient, make_proposer
    store = RunStore(args.runs)
    if args.command == "demo":
        from .data import demo_episodes
        episodes = demo_episodes()
        result = run_evolution(episodes[:2], episodes[2:4], jev=JevClient(mock=True), proposer=demo_proposer,
            store=store, run_name="合成演示", max_metric_calls=args.budget)
    else:
        config = json.loads(Path(args.proposer_config).read_text())
        if args.max_prompt_bytes is not None:
            config["max_prompt_bytes"] = args.max_prompt_bytes
        prior = store.get_run(args.resume_run) if args.resume_run else None
        namespace = (prior or {}).get('jev_metadata', {}).get('cache_namespace')
        result = run_evolution(read_episodes(args.train), read_episodes(args.validation),
            jev=JevClient(mock=args.mock_jev, cache_dir=".cache/jev", cache_namespace=namespace), proposer=make_proposer(config),
            store=store, run_name=args.name, max_metric_calls=args.budget, reflection_batch_size=args.reflection_batch_size,
            evolution_rounds=args.rounds, resume_run_id=args.resume_run,
            seed_pipeline=json.loads(Path(args.seed_pipeline).read_text()) if args.seed_pipeline else None,
            costs={"initial_cash": args.initial_cash, "fee_bps": args.fee_bps, "slippage_bps": args.slippage_bps})
    print(json.dumps({k: result.get(k) for k in ("run_id", "best_hash", "best_score", "total_metric_calls")}, ensure_ascii=False, indent=2))

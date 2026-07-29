"""Quotamux CLI —— 与 coder 无关的通用入口。

  quotamux                四池一张表（人读·永不打印密钥）
  quotamux --pick         印最优池 provider:订阅名（脚本判据）
  quotamux --export       印 shell 可 eval 的 env —— **任意 coder 直接吃**：
                            eval "$(quotamux --export)" && claude -p "..."
                            eval "$(quotamux --export)" && <your own coder>
  quotamux --json         机读（默认脱敏；--no-redact 才含明文 key）
  --model M[,M2,...]
                **要指定模型有余量的订阅**（认 id 与别名）。三档语义：
                  不给          = 任意模型有余量的订阅（只看订阅整体额度）
                  --model opus  = 只在提供 opus 的订阅里挑
                  --model k3,m3 = 这组候选**都行**（如"kimi 或 minimax 均可"），
                                  提供任一者即入选，再跨服务商挑余量最多的
                多值按给定顺序表示偏好：同池同时提供多个候选时用排在前面的
  --provider X  限定服务商   --min-window N  限流窗余量门槛(默认15)
  --allow-metered  允许回落按量计费池   --fresh  绕过缓存实时查
退出码: 0=有可用池 1=全部告急（应排队等窗，别加订阅）
"""
from __future__ import annotations

import argparse
import json
import sys

from .core import collect, pick


def _table(pools, best, model=None) -> None:
    hdr = f"（模型 {'/'.join(model)}）" if model else ""
    print(f"{'池' + hdr:<22} {'周剩':>7} {'窗剩':>7}  重置")
    for p in pools:
        if p.metered:
            print(f"{'💳' + p.name:<22} {'按量':>7} {'-':>7}  无订阅池")
            continue
        if not p.ok:
            print(f"{'⚠️ ' + p.name:<22} {p.error}")
            continue
        lamp = "🟢" if (p.quota_for(model)[0] or 0) >= 30 else (
            "🟡" if (p.quota_for(model)[0] or 0) >= 10 else "🔴")
        star = " ←选它" if best is p else ""
        wk, win = p.quota_for(model)
        win = win if win is not None else 100
        print(f"{lamp + p.name:<22} {wk:6.0f}% {win:6.0f}%  {p.reset}{star}")
    if not best:
        print("\n🔴 全部告急——排队等窗（撞窗多是调度问题，不是容量问题）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quotamux", description=__doc__.split("\n")[0])
    ap.add_argument("--pick", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--model", help="模型或候选列表（逗号分隔）——见 --help 三档语义")
    ap.add_argument("--provider")
    ap.add_argument("--min-window", type=float, default=15.0)
    ap.add_argument("--allow-metered", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="绕过缓存实时查")
    ap.add_argument("--no-redact", action="store_true",
                    help="--json 时输出明文 key（默认脱敏·慎用）")
    a = ap.parse_args(argv)

    pools = collect(cache_ttl=0 if a.fresh else None or 60.0)
    if a.provider:
        pools = [p for p in pools if p.provider == a.provider]
    want = [m.strip() for m in a.model.split(",") if m.strip()] if a.model else None
    if want:
        pools = [p for p in pools if p.serves(want) or p.metered]
    best = pick(pools, a.min_window, a.allow_metered, model=want)

    if a.json:
        print(json.dumps([p.as_dict(redact=not a.no_redact) for p in pools],
                         ensure_ascii=False, indent=2, default=str))
    elif a.pick:
        if not best:
            return 1
        print(f"{best.provider}:{best.name}")
    elif a.export:
        if not best:
            print("# no pool available — wait for the window", file=sys.stderr)
            return 1
        env = best.coder_env(want)
        if not env:
            print(f"# {best.provider}:{best.name} is a native subscription — "
                  "run your coder with no env override", file=sys.stderr)
        for k, v in env.items():
            print(f"export {k}={v!r}")
        hit = best.serves(want) if want else None
        print(f"# picked {best.provider}:{best.name}"
              + (f" model={hit.get('id')}" if hit else ""), file=sys.stderr)
    else:
        _table(pools, best, want)
    return 0 if best else 1


if __name__ == "__main__":
    sys.exit(main())

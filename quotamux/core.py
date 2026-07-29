"""Quotamux —— 多模型多订阅额度调度器（独立开源包·零业务依赖）。

一句话：**让任意 coder 自动落到余量最多的那个订阅上。**
（英文定位句：route your coder to the subscription with the most quota left.）
🔴 是**余量最多**，不是「还有余量」——「没空就行」正是把一个订阅烧穿、另一个闲置的成因。
名称核可（2026-07-29 实查）：PyPI/npm/GitHub org 与仓库搜索均无同名占用，可开源直用。


**它是什么**：一个与 coder 无关、与服务商无关的"订阅额度真数 + 按余量选池"模块。
- 与 coder 无关：只产 `env` 字典（base_url/model/key/附加变量），谁消费都行——
  Claude Code、Kimi CLI、Deep Code、自研 harness、CI 脚本皆可（`--export` 出 shell 可 eval 的行）。
- 与服务商无关：服务商与订阅全部声明在 `providers.yaml`，**加一家/加一个订阅＝改配置不改代码**。
- 一个模型可挂多个订阅：`subscriptions` 是列表，各自独立额度池；同一订阅名下的多把 key
  **共享同一池**（kimi 实测同账号 5 把 key 读数逐位相同），故查一把即代表全池。

**为什么要有它**（真实教训）：额度若靠本地日志反推，误差大且看不见不走日志的调用，
必然出现"一个订阅耗光、另一个一点没用"与反复撞窗停机。两家其实都有官方用量端点：
  kimi       GET {base}/usages          （出处=官方 CLI 源码 ui/shell/usage.py::_usage_url·
                                          🔴 usages 复数·单数 404，先前八连探测全栽在这）
  minimax    GET www.minimaxi.com/v1/token_plan/remains（出处=官方 Token Plan FAQ）
  anthropic  GET api.anthropic.com/api/oauth/usage（出处=Claude Code 二进制内嵌端点·
                                          与其 /usage 命令同源；返回 five_hour/seven_day
                                          的 utilization=**已用**%，故 mode=percent_used）
  openai     配置就位但**未实测**（本机无该订阅·首次接入者须核对返回体后校准 parse）

**选池判据**：限流窗剩余 ≥ `min_window_pct`（默认 15%）才算可用，其中**周剩最多者优先**
——这条直接消灭"一个耗光一个没用"。全不可用时返回 None（调用方应排队等窗，别加订阅）。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Sequence
from typing import Any

_REGISTRY = Path(__file__).with_name("providers.yaml")
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
DEFAULT_TIMEOUT = 20
MIN_WINDOW_PCT = 15.0
MAX_WORKERS = 8          # 并发查询：N 个池一轮往返，而不是 N × timeout
CACHE_TTL = 60.0         # 秒。批量起 worker 时不该把用量端点打穿

# 用户配置（不进代码库·可含私有服务商与订阅）。优先级：QUOTAMUX_CONFIG > ~/.quotamux/config.yaml
_USER_CONFIG_ENV = "QUOTAMUX_CONFIG"
_USER_CONFIG_DEFAULT = Path.home() / ".quotamux" / "config.yaml"
_CACHE_PATH = Path(os.environ.get("QUOTAMUX_CACHE",
                                  str(Path.home() / ".quotamux" / "cache.json")))


@dataclass
class Pool:
    """一个订阅的额度池（= 选池的最小单位）。"""

    provider: str
    name: str
    keys: list[str]                       # 环境变量名（非明文 key）
    ok: bool = False
    week_left: float | None = None        # 周剩百分比
    window_left: float | None = None      # 最紧限流窗剩余百分比
    reset: str = ""
    error: str = ""
    lane: dict[str, Any] = field(default_factory=dict)
    metered: bool = False                 # 按量计费（无订阅池）
    models: list[dict[str, Any]] = field(default_factory=list)   # 本池能提供哪些模型
    per_model: dict[str, tuple] = field(default_factory=dict)    # 模型→(周剩%,窗剩%)·服务商按模型分池时才有

    key_source: dict[str, Any] | None = None   # 从凭据文件取 key（如 Anthropic OAuth）

    @property
    def key_value(self) -> str:
        for e in self.keys:
            v = os.environ.get(e, "").strip()
            if v:
                return v
        # 凭据文件路径（OAuth 类订阅：token 在 CLI 的凭据文件里，不在 env）
        src = self.key_source or {}
        if src.get("file"):
            try:
                data = json.loads(Path(src["file"]).expanduser().read_text(encoding="utf-8"))
                v = _dig(data, src.get("path", ""))
                if isinstance(v, str) and v.strip():
                    return v.strip()
            except Exception:  # noqa: BLE001 —— 凭据不可读=该池不可用，不是崩溃
                pass
        return ""

    def serves(self, model: str | Sequence[str] | None) -> dict[str, Any] | None:
        """本池是否提供该模型 / 这组候选模型之一（认 id 与别名·大小写不敏感）。

        `model` 可以是一个名字，也可以是**候选列表**——"这个任务 kimi 或 minimax 都行"
        就写 `["k3", "m3"]`，命中任一即算本池可用。返回命中的模型声明（供取 id 与按模型额度）。
        按候选**给定顺序**优先：先写的偏好高，同池提供多个候选时用排在前面的那个。
        """
        if not model:
            return None
        wants = [model] if isinstance(model, str) else list(model)
        for want in wants:
            w = str(want).strip().lower()
            for m in self.models:
                names = [str(m.get("id", ""))] + [str(a) for a in (m.get("aliases") or [])]
                if any(n.lower() == w for n in names if n):
                    return m
        return None

    def quota_for(self, model: str | Sequence[str] | None = None
                  ) -> tuple[float | None, float | None]:
        """取余量：指定模型且服务商按模型分池时用该模型的行，否则用订阅整体余量。"""
        if model and (m := self.serves(model)):
            key = str(m.get("id", ""))
            if key in self.per_model:
                return self.per_model[key]
        return self.week_left, self.window_left

    def as_dict(self, redact: bool = True) -> dict[str, Any]:
        """机读表示。redact=True（默认）**绝不输出明文 key**——只给环境变量名。"""
        d = {k: v for k, v in self.__dict__.items() if k != "key_source"}
        d["has_key"] = bool(self.key_value)
        if not redact:
            d["key"] = self.key_value
        return d

    def coder_env(self, model: str | Sequence[str] | None = None) -> dict[str, str]:
        """产出任意 coder 可直接吃的环境变量（本模块的对外契约）。

        model 给定且本池提供它时，用该模型的真实 id（而非 lane 默认模型）。

        🔴 native 池返回空字典：原生订阅（如 Claude Max OAuth）**不能**被注入
        base_url/AUTH_TOKEN——注了就从 OAuth 降成 Bearer、订阅通道当场失效
        （生产实弹踩过）。原生订阅的正确用法是"什么都不设，直接跑 coder"。
        """
        lane = self.lane or {}
        if lane.get("native"):
            return {}
        model_id = lane.get("model", "")
        if model and (m := self.serves(model)):
            model_id = str(m.get("id") or model_id)
        env = {
            "ANTHROPIC_BASE_URL": lane.get("base_url", ""),
            "ANTHROPIC_MODEL": model_id,
            lane.get("auth_env", "ANTHROPIC_AUTH_TOKEN"): self.key_value,
        }
        # Anthropic 兼容端点：五个模型别名必须整族同切，漏一个=子代理带原生模型名打第三方 401
        for alias in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                      "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                      "CLAUDE_CODE_SUBAGENT_MODEL"):
            env[alias] = model_id
        env.update({k: str(v) for k, v in (lane.get("extra") or {}).items()})
        return {k: v for k, v in env.items() if v}


def _expand(s: str) -> str:
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), s)


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(remaining: Any, limit: Any) -> float | None:
    r, l = _num(remaining), _num(limit)
    if r is None or not l:
        return None
    return r / l * 100


def _read_conf(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        return json.loads(text) or {}
    try:
        import yaml  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        raise RuntimeError("读 YAML 配置需 PyYAML（pip install pyyaml），或改用 .json") from None
    return yaml.safe_load(text) or {}


def _user_config_path() -> Path | None:
    """用户配置：私有服务商/订阅写这里，不进代码库（故开源包可原样发布）。"""
    if (env := os.environ.get(_USER_CONFIG_ENV, "").strip()):
        p = Path(env).expanduser()
        return p if p.exists() else None
    for cand in (_USER_CONFIG_DEFAULT, _USER_CONFIG_DEFAULT.with_suffix(".json")):
        if cand.exists():
            return cand
    return None


def load_registry(path: Path | None = None, *, with_user: bool = True) -> dict:
    """读注册表 = 内置 providers.yaml ⊕ 用户配置（后者按 provider id 深合并、可覆盖可新增）。"""
    reg = _read_conf(path or _REGISTRY)
    if not with_user or path is not None:
        return reg
    up = _user_config_path()
    if not up:
        return reg
    user = _read_conf(up)
    merged = dict(reg.get("providers") or {})
    for pid, conf in (user.get("providers") or {}).items():
        base = dict(merged.get(pid) or {})
        base.update(conf or {})          # 用户键整体覆盖同名键（subscriptions 整体替换=语义明确）
        merged[pid] = base
    reg["providers"] = merged
    return reg


def _fetch(url: str, key: str, auth: str, timeout: int,
           extra_headers: dict[str, str] | None = None) -> dict | None:
    hdr = {"Content-Type": "application/json", **(extra_headers or {})}
    hdr["x-api-key" if auth == "x-api-key" else "Authorization"] = (
        key if auth == "x-api-key" else f"Bearer {key}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr),
                                    timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 —— 单家不可达不许拖垮整张表
        return None


def _parse(payload: dict, spec: dict) -> tuple[float | None, float | None, str]:
    """按声明式 spec 解析 →（周剩%, 窗剩%, 重置时刻）。两种 mode 覆盖已知两家。"""
    mode = spec.get("mode")
    if mode == "ratio":
        w = spec.get("week") or {}
        week = _pct(_dig(payload, w.get("remaining", "")), _dig(payload, w.get("limit", "")))
        reset = str(_dig(payload, w.get("reset", "")) or "")[:16].replace("T", " ")
        win = spec.get("window") or {}
        worst = None
        for item in (_dig(payload, win.get("list", "")) or []):
            p = _pct(_dig(item, win.get("remaining", "")), _dig(item, win.get("limit", "")))
            if p is not None:
                worst = p if worst is None else min(worst, p)
        return week, (100.0 if worst is None else worst), reset
    if mode == "percent_used":
        # 服务端给 utilization(已用%)——剩余 = 100 - 已用。Anthropic /api/oauth/usage 即此形态。
        w, win = spec.get("week") or {}, spec.get("window") or {}
        wu, iu = _num(_dig(payload, w.get("used_pct", ""))), _num(_dig(payload, win.get("used_pct", "")))
        reset = str(_dig(payload, w.get("reset", "")) or "")[:16].replace("T", " ")
        return (None if wu is None else 100 - wu, None if iu is None else 100 - iu, reset)
    if mode == "percent":
        sel = spec.get("select") or {}
        node: Any = payload
        if sel.get("path"):
            rows = _dig(payload, sel["path"]) or []
            where = sel.get("where") or {}
            node = next((r for r in rows
                         if all(r.get(k) == v for k, v in where.items())), None)
        if not isinstance(node, dict):
            return None, None, ""
        w, win = spec.get("week") or {}, spec.get("window") or {}
        ms = _num(_dig(node, w.get("reset_ms", "")))
        reset = datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else ""
        return (_num(_dig(node, w.get("pct", ""))),
                _num(_dig(node, win.get("pct", ""))), reset)
    return None, None, ""


def _cache_read(ttl: float) -> dict | None:
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - float(raw.get("at", 0)) <= ttl:
            return raw.get("data")
    except Exception:  # noqa: BLE001 —— 缓存坏了就当没有
        pass
    return None


def _cache_write(data: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({"at": time.time(), "data": data}),
                               encoding="utf-8")
        os.chmod(_CACHE_PATH, 0o600)   # 缓存含用量不含 key，仍按最小可见性
    except Exception:  # noqa: BLE001
        pass


def collect(registry: dict | None = None, timeout: int = DEFAULT_TIMEOUT,
            *, cache_ttl: float = CACHE_TTL) -> list[Pool]:
    """查全部订阅的真实额度（并发·带 TTL 缓存）。无 key 的订阅直接跳过（未配置≠故障）。

    并发：N 个池一轮往返而非 N×timeout（起一批 worker 时这是可感知的差别）。
    缓存：批量派单会连打用量端点，TTL 内复用（cache_ttl=0 强制实时）。
    """
    reg = registry or load_registry()
    cached = _cache_read(cache_ttl) if cache_ttl > 0 else None
    pools: list[Pool] = []
    jobs: list[tuple[Pool, dict]] = []
    for pid, pconf in (reg.get("providers") or {}).items():
        usage = pconf.get("usage")
        lane = pconf.get("lane") or {}
        for sub in (pconf.get("subscriptions") or []):
            pool = Pool(provider=pid, name=sub.get("name", pid),
                        keys=list(sub.get("keys") or []), lane=lane,
                        key_source=sub.get("key_source"),
                        models=list(pconf.get("models") or []))
            if not pool.key_value:
                continue                       # 未配 key = 该订阅不存在，不报错
            if not usage:                      # 按量计费类：无池可查，标记后仍可用
                pool.ok, pool.metered = True, True
                pools.append(pool)
                continue
            pools.append(pool)
            jobs.append((pool, usage))

    fresh: dict[str, Any] = {}

    def _one(job: tuple[Pool, dict]) -> None:
        pool, usage = job
        ck = f"{pool.provider}:{pool.name}"
        data = (cached or {}).get(ck)
        if data is None:
            data = _fetch(_expand(usage["url"]), pool.key_value,
                          usage.get("auth", "bearer"), timeout, usage.get("headers"))
            if data is not None:
                fresh[ck] = data
        if data is None:
            pool.error = "用量端点不可达"
            return
        spec = usage.get("parse") or {}
        week, win, reset = _parse(data, spec)
        pool.ok = week is not None
        pool.week_left, pool.window_left, pool.reset = week, win, reset
        # 服务商按模型分池时（如返回体每模型一行），逐模型解析各自余量
        for m in pool.models:
            row = m.get("usage_row")
            if not row or not spec.get("select"):
                continue
            sub_spec = dict(spec)
            sel = dict(spec["select"])
            sel["where"] = {**(sel.get("where") or {}),
                            list((sel.get("where") or {"model_name": ""}).keys())[0]: row}
            sub_spec["select"] = sel
            mw, mi, _ = _parse(data, sub_spec)
            if mw is not None:
                pool.per_model[str(m.get("id", ""))] = (mw, mi)
        if not pool.ok:
            pool.error = "返回体解析不出额度字段（注册表 parse 需校准）"

    if jobs:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs))) as ex:
            list(ex.map(_one, jobs))
    if fresh and cache_ttl > 0:
        _cache_write({**(cached or {}), **fresh})
    return pools


def pick(pools: list[Pool], min_window_pct: float = MIN_WINDOW_PCT,
         allow_metered: bool = False,
         model: str | Sequence[str] | None = None) -> Pool | None:
    """选池：窗口有余量者中，周剩最多的那个。按量池默认不参选（花钱的兜底须显式要）。

    **三档「哪个订阅可用」**（2026-07-29 用户指出的区分）——三档都取**余量最多**者，
    不是「还有余量就行」（后者正是烧穿一个、闲置另一个的成因）：
      · `model=None` —— **任意模型**有余量的订阅：只看订阅整体额度（跑什么都行的活）。
      · `model="X"`  —— **指定模型**有余量的订阅：先滤掉不提供 X 的池（周剩再多也没用，
        它根本不给这个模型），再在提供 X 的池里比余量；服务商按模型分池时（返回体每模型
        一行）用**该模型自己的**余量而非订阅整体余量。
      · `model=["X","Y"]` —— **一组候选都行**（"这个任务 kimi 或 minimax 都可以"）：
        提供其中任一者即入选，然后**跨服务商比余量**挑最空的那个。这是最常用的一档——
        既保证跑得起来（模型对），又保证不把某个订阅烧穿（余量最多）。
    """
    cand = [p for p in pools if p.ok and not p.metered]
    if model:
        cand = [p for p in cand if p.serves(model)]
    live = [p for p in cand
            if (q := p.quota_for(model))[1] is None or q[1] >= min_window_pct]
    if live:
        return max(live, key=lambda p: p.quota_for(model)[0] or 0)
    if allow_metered:
        pool = [p for p in pools if p.ok and p.metered]
        if model:
            pool = [p for p in pool if p.serves(model)]
        return pool[0] if pool else None
    return None

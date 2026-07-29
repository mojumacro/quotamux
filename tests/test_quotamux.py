"""Quotamux 守卫测试。

护三件事：
① **声明式可扩展**：加服务商/加订阅只改 providers.yaml——用一份纯虚构的注册表跑通
   全流程（解析两种 mode、选池、产 coder env），证明代码不含任何服务商专属分支；
② **选池判据**：窗口有余量者中周剩最多者胜（治"一个订阅耗光、另一个一点没用"）；
   按量池默认不参选（花钱的兜底须显式 allow_metered）；
③ **coder env 契约**：五个模型别名整族同切（漏一个＝子代理带原生模型名去打第三方
   端点，401·生产实弹踩过）+ key 注入到该家声明的 auth_env。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quotamux import Pool, load_registry, pick  # noqa: E402
from quotamux.core import _parse, collect  # noqa: E402

RATIO_PAYLOAD = {
    "usage": {"limit": "100", "used": "93", "remaining": "7",
              "resetTime": "2026-08-04T00:39:05Z"},
    "limits": [{"window": {"duration": 300}, "detail": {"limit": "100", "remaining": "99"}},
               {"window": {"duration": 60}, "detail": {"limit": "100", "remaining": "40"}}],
}
PERCENT_PAYLOAD = {
    "model_remains": [
        {"model_name": "video", "current_weekly_remaining_percent": 5,
         "current_interval_remaining_percent": 5},
        {"model_name": "general", "current_weekly_remaining_percent": 99,
         "current_interval_remaining_percent": 98, "weekly_end_time": 1785686400000},
    ]
}


def test_parse_ratio_mode_takes_tightest_window():
    spec = {"mode": "ratio",
            "week": {"remaining": "usage.remaining", "limit": "usage.limit",
                     "reset": "usage.resetTime"},
            "window": {"list": "limits", "remaining": "detail.remaining",
                       "limit": "detail.limit"}}
    week, win, reset = _parse(RATIO_PAYLOAD, spec)
    assert week == pytest.approx(7.0)
    assert win == pytest.approx(40.0), "多个限流窗必须取最紧的一条，否则会在紧窗上撞墙"
    assert reset.startswith("2026-08-04")


def test_parse_percent_mode_selects_row():
    spec = {"mode": "percent",
            "select": {"path": "model_remains", "where": {"model_name": "general"}},
            "week": {"pct": "current_weekly_remaining_percent", "reset_ms": "weekly_end_time"},
            "window": {"pct": "current_interval_remaining_percent"}}
    week, win, _ = _parse(PERCENT_PAYLOAD, spec)
    assert (week, win) == (99, 98), "select.where 没挑对行=读了别的模型的额度"


def _pool(name, week, win, metered=False, ok=True, **lane):
    return Pool(provider="x", name=name, keys=["K"], ok=ok, week_left=week,
                window_left=win, metered=metered, lane=lane)


def test_pick_prefers_most_weekly_remaining():
    """核心判据：治'一个订阅耗光、另一个一点没用'。"""
    pools = [_pool("a", 7, 99), _pool("b", 46, 100), _pool("c", 99, 100)]
    assert pick(pools).name == "c"


def test_pick_skips_rate_limited_even_if_weekly_rich():
    """周额度再多，撞了限流窗也不能选——否则必 429。"""
    pools = [_pool("rich-but-throttled", 99, 3), _pool("modest-but-free", 40, 90)]
    assert pick(pools).name == "modest-but-free"


def test_pick_excludes_metered_unless_explicit():
    pools = [_pool("payg", None, None, metered=True)]
    assert pick(pools) is None, "按量池默认不选——花钱的兜底必须显式要"
    assert pick(pools, allow_metered=True).name == "payg"


def test_pick_returns_none_when_all_throttled():
    assert pick([_pool("a", 99, 1), _pool("b", 80, 2)]) is None


def test_coder_env_switches_model_alias_family():
    """五别名整族同切——漏一个=子代理带原生模型名去打第三方端点，401。"""
    p = _pool("p", 50, 90, base_url="https://x/anthropic", model="M-1",
              auth_env="ANTHROPIC_AUTH_TOKEN", extra={"FOO": "1"})
    import os
    os.environ["K"] = "secret"
    env = p.coder_env()
    for alias in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                  "CLAUDE_CODE_SUBAGENT_MODEL"):
        assert env[alias] == "M-1", f"{alias} 未同切"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "secret" and env["FOO"] == "1"


def test_registry_is_pure_config_new_provider_needs_no_code(tmp_path, monkeypatch):
    """可扩展性硬证：一家**代码里完全不存在**的虚构服务商，仅凭配置即可跑通全流程。"""
    reg = tmp_path / "p.yaml"
    reg.write_text("""
providers:
  acme:
    display: ACME AI
    usage: null
    lane:
      base_url: "https://acme.example/anthropic"
      model: "acme-1"
      auth_env: ANTHROPIC_API_KEY
    subscriptions:
      - name: "acme-seat-1"
        keys: [ACME_KEY_1]
      - name: "acme-seat-2"
        keys: [ACME_KEY_2]
""", encoding="utf-8")
    monkeypatch.setenv("ACME_KEY_1", "k1")
    monkeypatch.setenv("ACME_KEY_2", "k2")
    pools = collect(load_registry(reg))
    assert {p.name for p in pools} == {"acme-seat-1", "acme-seat-2"}, \
        "同一模型挂多个订阅必须各自成池"
    assert pools[0].coder_env()["ANTHROPIC_API_KEY"] == "k1", "auth_env 声明未被尊重"


def test_shipped_registry_wellformed():
    reg = load_registry()
    provs = reg.get("providers") or {}
    assert provs, "注册表为空"
    for pid, c in provs.items():
        lane = c.get("lane") or {}
        assert lane.get("base_url") or lane.get("native"), \
            f"{pid} 既无 lane.base_url 又未标 native"
        subs = c.get("subscriptions") or []
        assert subs, f"{pid} 无订阅条目"
        names = [s["name"] for s in subs]
        assert len(names) == len(set(names)), f"{pid} 订阅重名=选池会认错池"
        if c.get("usage"):
            assert c["usage"].get("parse", {}).get("mode") in (
                "ratio", "percent", "percent_used"), f"{pid} parse.mode 非法"


def test_parse_percent_used_inverts_utilization():
    """Anthropic 形态：服务端给 utilization(已用%)，模块须换算成剩余%——
    弄反了会在"周额度只剩 22%"时报成"还有 78%"，正好在最该刹车时踩油门。"""
    payload = {"five_hour": {"utilization": 13.0},
               "seven_day": {"utilization": 78.0, "resets_at": "2026-08-01T00:00:00Z"}}
    spec = {"mode": "percent_used",
            "week": {"used_pct": "seven_day.utilization", "reset": "seven_day.resets_at"},
            "window": {"used_pct": "five_hour.utilization"}}
    week, win, reset = _parse(payload, spec)
    assert (week, win) == (22.0, 87.0)
    assert reset.startswith("2026-08-01")


def test_native_pool_emits_no_env():
    """原生订阅（Claude Max OAuth）绝不产 env——注入即把 OAuth 降成 Bearer、通道失效。"""
    p = Pool(provider="anthropic", name="claude-max", keys=[], ok=True,
             week_left=22, window_left=87, lane={"native": True})
    assert p.coder_env() == {}


# ── 硬化面（脱敏 / 用户覆盖 / 并发+缓存 / 容错 / CLI）──────────────────────

def test_json_output_never_leaks_key_by_default():
    """默认脱敏是安全底线：--json 会进日志/CI，泄 key 就是事故。"""
    import os
    os.environ["K"] = "sk-super-secret"
    p = _pool("p", 50, 90)
    d = p.as_dict()
    assert "sk-super-secret" not in str(d), "默认输出含明文 key"
    assert d["has_key"] is True, "脱敏后仍须能看出'配没配 key'"
    assert p.as_dict(redact=False)["key"] == "sk-super-secret", "显式要明文时须给"


def test_user_config_overrides_and_extends(tmp_path, monkeypatch):
    """用户配置能覆盖出厂订阅、也能新增服务商——这是'开源包不含我方配置'的前提。"""
    user = tmp_path / "config.yaml"
    user.write_text("""
providers:
  kimi:
    subscriptions:
      - name: "my-own-kimi"
        keys: [MY_KIMI_KEY]
  brandnew:
    display: Brand New
    usage: null
    lane: {base_url: "https://x/anthropic", model: "bn-1"}
    subscriptions:
      - name: "bn-1"
        keys: [BN_KEY]
""", encoding="utf-8")
    monkeypatch.setenv("QUOTAMUX_CONFIG", str(user))
    reg = load_registry()
    kimi_subs = [s["name"] for s in reg["providers"]["kimi"]["subscriptions"]]
    assert kimi_subs == ["my-own-kimi"], "用户配置未覆盖出厂订阅"
    assert "brandnew" in reg["providers"], "用户配置未能新增服务商"
    assert "minimax" in reg["providers"], "覆盖不该抹掉未提及的出厂服务商"


def test_collect_runs_concurrently_and_caches(tmp_path, monkeypatch):
    """并发=N 池一轮往返；缓存=批量派单不打穿端点。两者都靠 _fetch 调用次数与耗时验。"""
    import time as _t
    from quotamux import core
    monkeypatch.setattr(core, "_CACHE_PATH", tmp_path / "c.json")
    reg = {"providers": {"p": {
        "usage": {"url": "https://x/u", "auth": "bearer",
                  "parse": {"mode": "percent", "week": {"pct": "w"}, "window": {"pct": "i"}}},
        "lane": {"base_url": "b", "model": "m"},
        "subscriptions": [{"name": f"s{i}", "keys": [f"E{i}"]} for i in range(4)]}}}
    for i in range(4):
        monkeypatch.setenv(f"E{i}", f"k{i}")
    calls = []

    def fake_fetch(url, key, auth, timeout, extra=None):
        calls.append(key)
        _t.sleep(0.25)                       # 串行需 1.0s，并发应 ≈0.25s
        return {"w": 80, "i": 90}
    monkeypatch.setattr(core, "_fetch", fake_fetch)

    t0 = _t.time()
    pools = core.collect(reg, cache_ttl=30)
    elapsed = _t.time() - t0
    assert len(pools) == 4 and all(p.ok for p in pools)
    assert elapsed < 0.8, f"未并发（耗时 {elapsed:.2f}s ≈ 串行）"
    n = len(calls)
    core.collect(reg, cache_ttl=30)          # 第二轮应全部命中缓存
    assert len(calls) == n, "缓存未生效——批量派单会把用量端点打穿"
    core.collect(reg, cache_ttl=0)           # 强制实时
    assert len(calls) > n, "cache_ttl=0 未绕过缓存"


def test_single_provider_failure_does_not_kill_table(monkeypatch):
    """一家不可达不许拖垮整张表——否则撞窗当天连仪表都看不了。"""
    from quotamux import core
    reg = {"providers": {
        "bad": {"usage": {"url": "https://nope.invalid/u", "auth": "bearer",
                          "parse": {"mode": "percent", "week": {"pct": "w"}}},
                "lane": {"base_url": "b", "model": "m"},
                "subscriptions": [{"name": "bad-1", "keys": ["BADK"]}]},
        "good": {"usage": None, "lane": {"base_url": "b", "model": "m"},
                 "subscriptions": [{"name": "good-1", "keys": ["GOODK"]}]}}}
    monkeypatch.setenv("BADK", "x")
    monkeypatch.setenv("GOODK", "y")
    monkeypatch.setattr(core, "_fetch", lambda *a, **k: None)
    pools = core.collect(reg, cache_ttl=0)
    by = {p.name: p for p in pools}
    assert by["bad-1"].ok is False and by["bad-1"].error, "失败池未标错"
    assert by["good-1"].ok is True, "一家失败带崩了另一家"


def test_cli_smoke(monkeypatch, capsys):
    """CLI 四种模式端到端（假注册表·不打网络）。"""
    from quotamux import cli, core
    fake = {"providers": {"p": {
        "usage": None, "lane": {"base_url": "https://b", "model": "m1"},
        "subscriptions": [{"name": "s1", "keys": ["CK"]}]}}}
    monkeypatch.setattr(core, "load_registry", lambda *a, **k: fake)
    monkeypatch.setenv("CK", "secret-key")
    assert cli.main(["--allow-metered", "--pick"]) == 0
    assert "p:s1" in capsys.readouterr().out
    assert cli.main(["--allow-metered", "--json"]) == 0
    assert "secret-key" not in capsys.readouterr().out, "--json 泄了明文 key"
    assert cli.main(["--allow-metered", "--export"]) == 0
    assert "secret-key" in capsys.readouterr().out, "--export 该给 key（它的职责）"
    assert cli.main(["--allow-metered"]) == 0


# ── 模型维度（2026-07-29·「余量最多」的三档语义）──────────────────────────────

def _mpool(name, week, win, models, per_model=None):
    return Pool(provider="x", name=name, keys=["K"], ok=True, week_left=week,
                window_left=win, models=models, per_model=per_model or {},
                lane={"base_url": "https://b", "model": models[0]["id"] if models else ""})


A = [{"id": "kimi-k3", "aliases": ["k3", "kimi"]}]
B = [{"id": "MiniMax-M3", "aliases": ["m3", "minimax"]}]
C = [{"id": "claude-opus-4-6", "aliases": ["opus"]}]


def test_any_model_mode_ignores_model_support():
    """不给 model = 任意模型有余量：只比订阅整体额度。"""
    pools = [_mpool("a", 30, 99, A), _mpool("b", 90, 99, C)]
    assert pick(pools).name == "b"


def test_specific_model_filters_out_pools_that_do_not_serve_it():
    """指定模型：周剩再多也没用——它根本不给这个模型。"""
    pools = [_mpool("rich-no-opus", 99, 99, A), _mpool("poor-has-opus", 20, 99, C)]
    assert pick(pools, model="opus").name == "poor-has-opus"


def test_model_alias_and_case_insensitive():
    pools = [_mpool("a", 50, 99, A)]
    for name in ("k3", "K3", "kimi-k3", "KIMI"):
        assert pick(pools, model=name) is not None, f"别名/大小写未认: {name}"
    assert pick(pools, model="opus") is None


def test_candidate_set_picks_emptiest_across_providers():
    """一组候选都行（kimi 或 minimax 均可）→ 跨服务商挑余量最多的。"""
    pools = [_mpool("kimi-A", 7, 99, A), _mpool("kimi-B", 46, 99, A),
             _mpool("mm-1", 73, 99, B), _mpool("mm-2", 99, 99, B),
             _mpool("claude", 95, 99, C)]     # 95 但不提供候选模型→不该被选
    best = pick(pools, model=["k3", "m3"])
    assert best.name == "mm-2", "候选集内未挑余量最多的"
    assert pick(pools, model=["k3"]).name == "kimi-B", "单候选时未在其内挑最多"


def test_candidate_order_is_preference_within_a_pool():
    """同池同时提供多个候选时，用排在前面的那个（顺序=偏好）。"""
    both = _mpool("both", 50, 99, A + B)
    assert both.serves(["m3", "k3"])["id"] == "MiniMax-M3"
    assert both.serves(["k3", "m3"])["id"] == "kimi-k3"


def test_per_model_quota_beats_pool_level_when_declared():
    """服务商按模型分池时，用该模型自己的余量——否则会拿'订阅整体还很空'去掩盖
    '这个模型已经见底'。"""
    p = _mpool("mm", 90, 99, [{"id": "MiniMax-M3", "aliases": ["m3"], "usage_row": "general"}],
               per_model={"MiniMax-M3": (5.0, 99.0)})
    assert p.quota_for("m3") == (5.0, 99.0)
    assert p.quota_for() == (90, 99), "不指定模型时仍看订阅整体"
    other = _mpool("k", 40, 99, A)
    assert pick([p, other], model=["m3", "k3"]).name == "k", \
        "按模型余量选池失效：会选中该模型已见底的池"


def test_coder_env_uses_the_matched_model_id():
    """候选集命中哪个，就把哪个真实 id 写进 env（不是 lane 默认模型）。"""
    import os
    os.environ["K"] = "kk"
    p = _mpool("both", 50, 99, A + B)
    assert p.coder_env(["m3", "k3"])["ANTHROPIC_MODEL"] == "MiniMax-M3"
    assert p.coder_env(["k3"])["ANTHROPIC_MODEL"] == "kimi-k3"
    assert p.coder_env()["ANTHROPIC_MODEL"] == "kimi-k3", "不指定时用 lane 默认"


def test_shipped_registry_declares_models():
    """出厂注册表每家都要声明 models——否则 --model 选池对它永远选不中。"""
    for pid, c in (load_registry().get("providers") or {}).items():
        models = c.get("models") or []
        assert models, f"{pid} 未声明 models（--model 选池会漏掉它）"
        for m in models:
            assert m.get("id"), f"{pid} 有模型条目缺 id"

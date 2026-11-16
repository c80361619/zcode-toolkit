#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCode 自定义供应商模型拉取（CLI 版，不动 app.asar）
====================================================
从各自定义供应商的 /models 接口拉取模型列表，把新模型写入 v2 配置（config.json +
provider_config.json，后者是 3.14.x 界面模型列表的唯一事实源；根目录按
~/.zcode/v2/setting.json 的 dataBaseDir 解析，与客户端 bootstrap 同逻辑）。
已支持读取网关透出的每模型元数据（context_length / max_output_tokens / supported_efforts /
default_effort，如 workbuddy2api），按模型写实 limit 与思考档位；元数据缺失时退回保守模板
（1M/128k + off/high/max）。已有条目默认原样保留（含手改的配置）；--refresh 可按服务器
元数据刷新已有条目的 limit 与思考档位。

配置同步逻辑基于 MIT 许可的社区项目二次开发（许可声明见 NOTICE.md），
改动：跨平台路径、非交互参数、元数据感知模板、--refresh、dry-run、写前备份。

用法：
  python model_pull.py                  # 交互式选择供应商
  python model_pull.py --all            # 同步全部自定义供应商
  python model_pull.py --provider deep  # 按名称/ID 子串匹配供应商
  python model_pull.py --all --dry-run  # 只看会新增什么，不写配置
  python model_pull.py --all --refresh  # 刷新已有条目的 limit/思考档位（按服务器元数据）
  python model_pull.py --all --no-reasoning   # 新模型不带思考档位
  python model_pull.py --test https://api.example.com/v1 [KEY]  # 只测连通并看元数据

注意：ZCode 运行中写 config.json 存在被客户端回写覆盖的竞态，建议完全退出后执行；
注入版（zcode_patcher.py --model-puller）在界面内写入并自动触发刷新，无此问题。
"""

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:                                   # 控制台编码安全网（见 _console.py 的说明）
    from _console import bad_mark, glyph, ok_mark, safe_stdio
except ImportError:                    # 被别处 import 时脚本目录可能不在 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _console import bad_mark, glyph, ok_mark, safe_stdio

def resolve_v2_root() -> Path:
    """解析 v2 配置根目录：~/.zcode/v2/setting.json 的 dataBaseDir 优先
    （与 ZCode 客户端 bootstrap 同逻辑），未设置时回退 home 目录。"""
    base = Path.home()
    try:
        s = json.loads((base / ".zcode" / "v2" / "setting.json").read_text(encoding="utf-8"))
        dbd = s.get("dataBaseDir")
        if isinstance(dbd, str) and dbd.strip():
            base = Path(dbd.strip())
    except Exception:
        pass
    return base / ".zcode" / "v2"


V2_ROOT = resolve_v2_root()
CONFIG_PATH = V2_ROOT / "config.json"


def set_v2_root(root: str | None) -> None:
    """用 --v2-root 覆盖配置根目录（数据目录迁移 / 测试用）。"""
    global V2_ROOT, CONFIG_PATH
    if root:
        V2_ROOT = Path(root)
        CONFIG_PATH = V2_ROOT / "config.json"

FALLBACK_EFFORTS = ["off", "low", "high", "max"]
FALLBACK_LIMIT = {"context": 1000000, "output": 128000}

# 3.14.x 原生档位机制：optionSpecs.reasoningLevel.map 为 CEL 表达式，
# 按档位生成 JSON 合并补丁，由内核 createModelOptionMapFetch 直接打进请求体
MAP_OPENAI = "{'reasoning_effort':reasoningLevel}"
MAP_ANTHROPIC = ("reasoningLevel=='off' ? {'thinking':{'type':'disabled'}} : "
                 "{'thinking':{'type':'adaptive'},'output_config':{'effort':reasoningLevel}}")


def build_option_specs(kind: str | None, values: list[str]) -> dict:
    m = MAP_ANTHROPIC if kind == "anthropic" else MAP_OPENAI
    return {"reasoningLevel": {"values": values, "map": m}}


def _parse_model_item(item) -> tuple[str | None, dict]:
    """从 /models 条目提取 (id, 元数据)；字符串条目只有 id。"""
    if isinstance(item, str):
        s = item.strip()
        return (s or None), {}
    if not isinstance(item, dict):
        return None, {}
    mid = (item.get("id") or item.get("name") or "").strip() or None
    if not mid:
        return None, {}
    top = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
    meta = {
        "context": int(item.get("context_length") or item.get("max_input_tokens") or 0),
        "output": int(item.get("max_output_tokens") or item.get("max_completion_tokens")
                      or top.get("max_completion_tokens") or 0),
        "efforts": [str(x).strip() for x in (item.get("supported_efforts") or [])
                    if isinstance(x, str) and x.strip()],
        "default": item.get("default_effort") if isinstance(item.get("default_effort"), str) else None,
    }
    return mid, meta


def build_efforts(meta: dict) -> list[str]:
    efforts = list(meta.get("efforts") or []) or list(FALLBACK_EFFORTS)
    if "off" not in efforts:
        efforts = ["off"] + efforts
    d = meta.get("default")
    if d in efforts and d != efforts[-1]:   # 3.14.x 约定：values 末位即默认档
        efforts.remove(d)
        efforts.append(d)
    return efforts


def build_entry(meta: dict, kind: str | None = None, with_reasoning: bool = True) -> dict:
    m = meta or {}
    efforts = build_efforts(m)
    tpl = {
        "limit": {
            "context": m.get("context") or FALLBACK_LIMIT["context"],
            "output": m.get("output") or FALLBACK_LIMIT["output"],
        },
        "modalities": {"input": ["text", "image"], "output": ["text"]},
        "zcode": {"modalitiesConfigured": True, "modified": True},
    }
    if with_reasoning:
        tpl["optionSpecs"] = build_option_specs(kind, efforts)
    return tpl


def fetch_models_from_api(base_url: str, api_key: str = "", timeout: int = 10):
    """请求供应商 /models 接口，返回 (ok, msg, ids, metas)。自动适配 OpenAI 兼容/OneAPI 等。"""
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return False, "Base URL 为空", [], {}

    candidates = []
    if base_url.endswith("/v1"):
        candidates.append(f"{base_url}/models")
        candidates.append(f"{base_url[:-3]}/models")
    else:
        candidates.append(f"{base_url}/v1/models")
        candidates.append(f"{base_url}/models")
    if "/api" in base_url and not base_url.endswith("/models"):
        candidates.append(f"{base_url}/v1/models")
    # 兼容带协议前缀的 baseUrl（如 https://api.deepseek.com/anthropic）：
    # 子路径下通常没有 /models，回退到同域名根路径再试。
    try:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme and parts.netloc:
            origin = f"{parts.scheme}://{parts.netloc}"
            for candidate in (f"{origin}/v1/models", f"{origin}/models"):
                if candidate not in candidates:
                    candidates.append(candidate)
    except Exception:
        pass

    headers = {"User-Agent": "ZCode", "Accept": "application/json"}
    if api_key:
        api_key = api_key.strip()
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    last_error = ""
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if not (200 <= resp.status < 300):
                    continue
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}: {e.reason}"
            continue
        except Exception as e:
            last_error = str(e)
            continue

        models_raw = []
        if isinstance(data, list):
            models_raw = data
        elif isinstance(data, dict):
            if isinstance(data.get("data"), list):
                models_raw = data["data"]
            elif isinstance(data.get("models"), list):
                models_raw = data["models"]

        ids, metas = [], {}
        for item in models_raw:
            mid, meta = _parse_model_item(item)
            if mid and mid not in metas:
                metas[mid] = meta
                ids.append(mid)
        if ids:
            ids.sort()
            return True, f"成功从 {url} 获取", ids, metas

    return False, f"拉取失败: {last_error or '未能解析到有效模型列表'}", [], {}


def list_custom_providers(config_data):
    providers = config_data.get("provider", {})
    return [(pid, pdata) for pid, pdata in providers.items()
            if not pid.startswith("builtin:")
            and pdata.get("source") in (None, "custom", "user")]


def refresh_entry(ent: dict, meta: dict, kind: str | None = None) -> list[str]:
    """按服务器元数据就地刷新单条已有模型（limit + 档位），返回变更描述。
    旧格式 reasoning.variants 顺手迁移为 3.14.x 的 optionSpecs.reasoningLevel。"""
    changed = []
    if meta.get("context") or meta.get("output"):
        old_limit = ent.get("limit") or {}
        new_limit = {"context": meta.get("context") or old_limit.get("context"),
                     "output": meta.get("output") or old_limit.get("output")}
        if old_limit != new_limit:
            ent["limit"] = new_limit
            changed.append(f"limit {new_limit['context']}/{new_limit['output']}")
    old_reasoning = ent.pop("reasoning", None)
    specs = ent.get("optionSpecs")
    efforts = build_efforts(meta)
    if old_reasoning is not None or not isinstance(specs, dict) or "reasoningLevel" not in specs:
        if not isinstance(specs, dict):
            specs = ent["optionSpecs"] = {}
        cur = specs.get("reasoningLevel") or {}
        specs["reasoningLevel"] = {"values": efforts, "map": cur.get("map") or build_option_specs(kind, efforts)["reasoningLevel"]["map"]}
        changed.append(f"档位 {'/'.join(efforts)}" + ("（旧格式已迁移 optionSpecs）" if old_reasoning is not None else ""))
    elif specs["reasoningLevel"].get("values") != efforts:
        specs["reasoningLevel"]["values"] = efforts
        changed.append(f"档位 {'/'.join(efforts)}")
    return changed


def sync(providers, with_reasoning: bool, dry_run: bool, refresh: bool) -> int:
    total_added = 0
    for pid, pdata in providers:
        name = pdata.get("name", "未命名")
        kind = pdata.get("kind")
        opts = pdata.get("options") or {}
        base_url = opts.get("baseURL") or ""
        api_key = opts.get("apiKey") or ""

        print(f"\n🔄 供应商「{name}」（{pid}）")
        success, msg, fetched, metas = fetch_models_from_api(base_url, api_key)
        if not success:
            print(f"  ❌ {msg}")
            continue

        models = pdata.get("models") or {}
        new_models = [m for m in fetched if m not in models]
        print(f"  ✅ {msg}，共 {len(fetched)} 个模型，已存在 {len(fetched) - len(new_models)} 个，"
              f"新增 {len(new_models)} 个" + ("，刷新元数据" if refresh else ""))
        for m in new_models:
            meta = metas.get(m) or {}
            print(f"    + {m}  (ctx {meta.get('context') or '-'} / out {meta.get('output') or '-'}"
                  f" / 档位 {'/'.join(meta.get('efforts') or []) or '-'}{(' 默认 ' + meta['default']) if meta.get('default') else ''})")
        if not new_models and not refresh:
            continue
        total_added += len(new_models)

        for m in new_models:
            models[m] = build_entry(metas.get(m) or {}, kind, with_reasoning)
        if refresh:
            for mid, ent in models.items():
                meta = metas.get(mid)
                if not meta:
                    continue
                changed = refresh_entry(ent, meta, kind)
                if changed:
                    print(f"    {'[预览] ' if dry_run else glyph('↻', '~') + ' '}{mid}: "
                          + "；".join(changed))
        if dry_run:
            continue
        pdata["models"] = models
    return total_added


def sync_provider_config(cfg: dict) -> None:
    """把 config.json 的 provider 段同步进新版 provider_config.json——3.14.x 起
    界面模型列表以它为唯一事实源（providerRules.personalModelIds/modelOrder +
    modelConfigRules.providerModelRules），只写 config.json 界面永远看不到。"""
    pc_path = V2_ROOT / "provider_config.json"
    try:
        pc = json.loads(pc_path.read_text(encoding="utf-8"))
        if not isinstance(pc, dict):
            pc = {}
    except Exception:
        pc = {}
    pc.setdefault("schemaVersion", 1)
    conf = pc.setdefault("config", {})
    rules = conf.setdefault("providerConfigRules", {}).setdefault("providerRules", [])
    mrules = conf.setdefault("modelConfigRules", {}).setdefault("providerModelRules", [])

    def kind_to_api(kind: str | None) -> str:
        if kind == "anthropic":
            return "anthropic-messages"
        if kind == "openai":
            return "openai-responses"
        return "openai-chat-completions"

    by_id = {r.get("providerId"): r for r in rules
             if isinstance(r, dict) and r.get("providerId")}
    for pid, pdata in (cfg.get("provider") or {}).items():
        if not isinstance(pdata, dict) or pid.startswith("builtin:"):
            continue
        models = pdata.get("models") or {}
        ids = list(models.keys())
        opts = pdata.get("options") or {}
        rule = by_id.get(pid)
        if rule is None:  # 兼容 provider_config 与 config.json id 不一致：按 baseURL 兜底
            bu = (opts.get("baseURL") or "").rstrip("/")
            for r in rules:
                u = ((r.get("config") or {}).get("api") or {}).get("baseUrl") or ""
                if u.rstrip("/") == bu:
                    rule = r
                    break
        if rule is None:
            rule = {"providerId": pid, "providerName": pdata.get("name") or pid,
                    "config": {"group": "standard-personal", "access": {"type": "api-key"},
                               "api": {"type": kind_to_api(pdata.get("kind"))},
                               "personalModelIds": [], "modelOrder": []}}
            rules.append(rule)
            by_id[pid] = rule
        c = rule.setdefault("config", {})
        c.setdefault("group", "standard-personal")
        acc = c.setdefault("access", {"type": "api-key"})
        if opts.get("apiKey"):
            acc["apiKey"] = opts["apiKey"]
        api = c.setdefault("api", {})
        if opts.get("baseURL"):
            api["baseUrl"] = opts["baseURL"]
        if pdata.get("kind"):
            api["type"] = kind_to_api(pdata["kind"])
        # 保序：既有顺序保留，新模型追加到末尾（整体覆盖会把用户在界面里排好的顺序打乱）
        old_order = [i for i in (c.get("modelOrder") or []) if i in ids]
        c["personalModelIds"] = old_order + [i for i in ids if i not in old_order]
        c["modelOrder"] = list(c["personalModelIds"])
        # 该供应商的模型规则：保留既有（界面手改的 contextWindow 等），缺失才补，
        # 已删除模型的规则随之移除；其它供应商与 account 级规则原样不动。
        # 注意：同一模型不能同时出现在 providerModelRules（智能规则）与
        # manualProviderModelRules（界面手动配置）里，否则内核 schema 校验失败、
        # 整个供应商配置降级为空——手动配置过的模型跳过，不补智能规则。
        old_rules = {m.get("modelId"): m for m in mrules
                     if isinstance(m, dict) and m.get("providerId") == pid}
        # 冲突检测是全局的（内核 schema 按 providerId+modelId 全局查重），
        # 任何供应商的手动配置都会让同名模型不能再补智能规则
        manual_ids = {f"{m.get('providerId')}/{m.get('modelId')}" for m in
                      (conf.get("modelConfigRules", {}).get("manualProviderModelRules") or [])
                      if isinstance(m, dict)}
        mrules[:] = [m for m in mrules
                     if not (isinstance(m, dict) and m.get("providerId") == pid)]
        for mid in ids:
            if mid in old_rules and f"{pid}/{mid}" not in manual_ids:
                mrules.append(old_rules[mid])
                continue
            if f"{pid}/{mid}" in manual_ids:
                continue
            ctx = ((models.get(mid) or {}).get("limit") or {}).get("context") or 1000000
            mrules.append({"modelId": mid, "providerId": pid,
                           "config": {"properties": {"contextWindow": ctx}}})
    try:
        shutil.copy2(pc_path, pc_path.with_name(pc_path.name + ".puller-bak"))
    except Exception:
        pass
    pc_path.write_text(json.dumps(pc, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    safe_stdio()          # 输出被重定向时 cp936 会编不出符号，先把这条路封死
    ap = argparse.ArgumentParser(description="ZCode 自定义供应商模型拉取（CLI，不动 asar）")
    ap.add_argument("--all", action="store_true", help="同步全部自定义供应商")
    ap.add_argument("--provider", default=None, help="按名称/ID/baseURL 子串匹配供应商")
    ap.add_argument("--dry-run", action="store_true", help="只显示会新增/刷新什么，不写配置")
    ap.add_argument("--refresh", action="store_true",
                    help="按服务器元数据刷新已有条目的 limit 与思考档位（其余键不动）")
    ap.add_argument("--no-reasoning", action="store_true", help="新模型不配思考档位")
    ap.add_argument("--test", nargs="+", metavar=("BASE_URL", "API_KEY"), help="只测试 URL/Key 连通与模型列表")
    ap.add_argument("--v2-root", default=None,
                    help="覆盖 v2 配置根目录（默认按 setting.json 的 dataBaseDir 解析）")
    args = ap.parse_args()
    set_v2_root(args.v2_root)

    if args.test:
        url = args.test[0]
        key = args.test[1] if len(args.test) > 1 else ""
        ok, msg, models, metas = fetch_models_from_api(url, key)
        print((ok_mark() if ok else bad_mark()) + " " + msg)
        if ok:
            print(f"共 {len(models)} 个模型：")
            for m in models:
                meta = metas.get(m) or {}
                print(f"  {m:35} ctx {meta.get('context') or '-':>8}  out {meta.get('output') or '-':>7}"
                      f"  档位 {'/'.join(meta.get('efforts') or []) or '-'}"
                      f"{(' 默认 ' + meta['default']) if meta.get('default') else ''}")
        sys.exit(0 if ok else 1)

    if not CONFIG_PATH.is_file():
        raise SystemExit(f"[!] 找不到配置文件: {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    customs = list_custom_providers(cfg)
    if not customs:
        raise SystemExit("[!] config 里没有自定义供应商（builtin 之外、source=custom）")

    if args.all or args.provider:
        kw = (args.provider or "").lower()
        selected = [(pid, p) for pid, p in customs
                    if not kw or kw in pid.lower() or kw in (p.get("name") or "").lower()
                    or kw in (p.get("options") or {}).get("baseURL", "").lower()]
        if not selected:
            raise SystemExit(f"[!] 没有匹配「{args.provider}」的供应商；现有："
                             + ", ".join(p.get("name", pid) for pid, p in customs))
    else:
        print(f"找到 {len(customs)} 个自定义供应商：")
        for i, (pid, p) in enumerate(customs, 1):
            print(f"  [{i}] {p.get('name', '未命名')}  {(p.get('options') or {}).get('baseURL', '-')}"
                  f"  （已有 {len(p.get('models') or {})} 模型）")
        choice = input("选择编号（a=全部，q=退出）: ").strip().lower()
        if choice == "q":
            return
        if choice == "a":
            selected = customs
        elif choice.isdigit() and 1 <= int(choice) <= len(customs):
            selected = [customs[int(choice) - 1]]
        else:
            raise SystemExit("[!] 无效选择")

    total = sync(selected, with_reasoning=not args.no_reasoning,
                 dry_run=args.dry_run, refresh=args.refresh)

    if args.dry_run:
        print(f"\n[dry-run] 将新增 {total} 个模型，未写配置。去掉 --dry-run 执行写入。")
        return
    if total > 0 or args.refresh:
        bak = CONFIG_PATH.with_name(f"config.json.bak.{int(time.time())}")
        shutil.copy2(CONFIG_PATH, bak)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            sync_provider_config(cfg)
            pc_note = f"；已同步 provider_config.json（{V2_ROOT}）"
        except Exception as e:
            pc_note = f"；⚠️ provider_config.json 同步失败: {e}"
        print(f"\n🎉 已写入配置（新增 {total} 个模型，备份: {bak.name}）{pc_note}。")
        print("    ZCode 运行中请在界面里切换一下页面刷新模型列表，或重启 ZCode。")


if __name__ == "__main__":
    main()

"""模型库 e2e：缺省迁移、CRUD、激活切换（单选）、测试、引擎联动。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def req(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        raw = resp.read()
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return json.loads(raw.decode("utf-8"))
        return {"_raw": raw}


def main():
    # 1. 缺省迁移
    ms = req("GET", "/api/models")
    check("缺省模型 5 个", len(ms) == 5, f"({[m['provider'] for m in ms]})")
    edge = next((m for m in ms if m["provider"] == "edge"), None)
    check("Edge 缺省激活（voice）", edge and edge["is_active"] == 1)
    check("MiniMax 价格迁移", any(m["provider"] == "minimax" and m["price"] == 4 for m in ms))
    check("DeepSeek 缺省存在（llm）", any(m["kind"] == "llm" and m["provider"] == "deepseek" for m in ms))

    # 2. 预设模板
    presets = req("GET", "/api/models/presets")
    check("预设模板 15 个", len(presets) == 15, f"({len(presets)})")
    check("模板含 GLM 免费", any(p["provider"] == "glm" and p["price"] == 0 for p in presets))

    # 3. 追加自定义模型并激活（llm 类）
    m = req("POST", "/api/models", {
        "kind": "llm", "provider": "glm", "name": "测试 GLM-4-Flash", "model": "glm-4-flash",
        "api_key": "", "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "price": 0, "price_output": 0, "price_unit": "百万token",
        "desc": "免费测试", "extra": {}, "activate": True,
    })
    check("追加模型", bool(m["id"]) and m["is_active"] == 1)
    llms = req("GET", "/api/models?kind=llm")
    active_llm = [x for x in llms if x["is_active"]]
    check("LLM 激活单选（GLM 激活、DeepSeek 停用）",
          len(active_llm) == 1 and active_llm[0]["provider"] == "glm",
          f"({[x['provider'] for x in active_llm]})")

    # 4. 测试（无 key → 未填写）
    t = req("POST", f"/api/models/{m['id']}/test")
    check("LLM 测试-无key提示", t["ok"] is False and "API Key" in t["message"], f"({t['message']})")

    # 5. 编辑价格
    upd = req("PATCH", f"/api/models/{m['id']}", {"price": 0.5, "desc": "已改价"})
    check("编辑价格", upd["price"] == 0.5)

    # 6. 切回 DeepSeek 激活
    ds = next((x for x in llms if x["provider"] == "deepseek"), None)
    req("POST", f"/api/models/{ds['id']}/activate")
    llms2 = req("GET", "/api/models?kind=llm")
    active2 = [x for x in llms2 if x["is_active"]]
    check("激活切换回 DeepSeek", len(active2) == 1 and active2[0]["provider"] == "deepseek")

    # 6.5 停用（deactivate）
    req("POST", f"/api/models/{ds['id']}/deactivate")
    llms2b = req("GET", "/api/models?kind=llm")
    active2b = [x for x in llms2b if x["is_active"]]
    check("停用后 llm 类无激活", len(active2b) == 0)
    req("POST", f"/api/models/{ds['id']}/activate")
    llms2c = req("GET", "/api/models?kind=llm")
    check("重新激活", len([x for x in llms2c if x["is_active"]]) == 1)

    # 7. 删除非激活模型
    req("DELETE", f"/api/models/{m['id']}")
    llms3 = req("GET", "/api/models?kind=llm")
    check("删除模型", len(llms3) == 1, f"({len(llms3)})")

    # 8. 激活 minimax（补 key）→ 引擎 check 联动
    voice_ms = req("GET", "/api/models?kind=voice")
    mm = next((x for x in voice_ms if x["provider"] == "minimax"), None)
    req("PATCH", f"/api/models/{mm['id']}", {"api_key": "sk-e2e-test-key", "extra": {"group_id": "gp-e2e"}})
    req("POST", f"/api/models/{mm['id']}/activate")
    engines = req("GET", "/api/voices/engines/info")
    mm_eng = next((e for e in engines if e["id"] == "minimax"), None)
    check("激活联动：MiniMax 引擎 ok", mm_eng["ok"] is True, f"({mm_eng['check_message']})")
    edge_eng = next((e for e in engines if e["id"] == "edge"), None)
    check("Edge 引擎仍可用", edge_eng["ok"] is True)

    # 9. 删除最后一个激活模型应被拒绝
    # 先删掉其它 voice 模型（非激活可删），只剩 minimax（激活）
    for prov in ("openvino", "fish", "edge"):
        x = next((m for m in voice_ms if m["provider"] == prov), None)
        if x:
            req("DELETE", f"/api/models/{x['id']}")
    try:
        req("DELETE", f"/api/models/{mm['id']}")
        check("删除最后一个激活被拒绝", False)
    except urllib.error.HTTPError as e:
        check("删除最后一个激活被拒绝", e.code == 400, f"(HTTP {e.code})")

    # 10. 恢复现场（幂等规范化）：删除非缺省条目与重复 minimax → 补全缺省 → 激活 edge、停用无 key 的 llm
    voice_final = req("GET", "/api/models?kind=voice")
    for x in voice_final:
        if x["provider"] not in ("edge", "openvino", "minimax", "fish"):
            req("DELETE", f"/api/models/{x['id']}")
    voice_final = req("GET", "/api/models?kind=voice")
    seen = {}
    for x in voice_final:
        if x["provider"] in seen:
            req("DELETE", f"/api/models/{x['id']}")
        seen[x["provider"]] = x["id"]
    voice_final = req("GET", "/api/models?kind=voice")
    have = {x["provider"] for x in voice_final}
    for p in presets:
        is_mm_hd = p["provider"] == "minimax" and p["model"] == "speech-02-hd"
        if (p["kind"] == "voice" and p["provider"] in ("edge", "openvino", "fish") and p["provider"] not in have) or (is_mm_hd and "minimax" not in have):
            req("POST", "/api/models", {**p, "activate": False})
            have.add(p["provider"])
    voice2 = req("GET", "/api/models?kind=voice")
    e2 = next((x for x in voice2 if x["provider"] == "edge"), None)
    req("POST", f"/api/models/{e2['id']}/activate")
    mm2 = next((x for x in voice2 if x["provider"] == "minimax"), None)
    if mm2:
        req("PATCH", f"/api/models/{mm2['id']}", {"api_key": ""})
    # llm 类：deepseek 无 key → 取消激活（与开箱状态一致）
    llm_restore = req("GET", "/api/models?kind=llm")
    ds_restore = next((x for x in llm_restore if x["provider"] == "deepseek"), None)
    if ds_restore and ds_restore["is_active"] and not ds_restore["api_key"]:
        req("POST", f"/api/models/{ds_restore['id']}/deactivate")
    voice_final = req("GET", "/api/models?kind=voice")
    llm_final = req("GET", "/api/models?kind=llm")
    check("恢复：Edge 激活且缺省齐全、无 key 的 LLM 停用",
          len(voice_final) == 4
          and next((x for x in voice_final if x["is_active"]), None)["provider"] == "edge"
          and len([x for x in llm_final if x["is_active"]]) == 0,
          f"(voice={[x['provider'] for x in voice_final]}, llm_active={[x['provider'] for x in llm_final if x['is_active']]})")

    print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

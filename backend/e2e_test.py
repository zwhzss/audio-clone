"""完整端到端测试：声音 → 文稿 → 项目 → 生成 → 分段参数 → 章节导出 → 对比试听 → 渲染（含闪避曲线）→ 成品。"""
import json
import sys
import time
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
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r, timeout=300) as resp:
        raw = resp.read()
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return json.loads(raw.decode("utf-8"))
        return {"_raw": raw, "_ct": ct}


def wait_job(job_id, timeout=400):
    for _ in range(timeout // 2):
        time.sleep(2)
        s = req("GET", f"/api/jobs/{job_id}")
        if s["status"] not in ("queued", "running"):
            return s
    return {"status": "timeout"}


def main():
    # 1. 创建两个声音（对比试听用）
    v1 = req("POST", "/api/voices", {"name": "晓晓", "source": "edge", "edge_voice": "zh-CN-XiaoxiaoNeural"})
    v2 = req("POST", "/api/voices", {"name": "云希", "source": "edge", "edge_voice": "zh-CN-YunxiNeural"})
    check("创建声音", v1["id"] and v2["id"])

    # 2. 文稿（带章节标题）
    doc = req("POST", "/api/documents", {
        "title": "测试文稿",
        "content": "第一章 初见\n这是第一章的内容，用来验证自动切分章节的功能。\n第二章 重逢\n这是第二章的内容。\n第三章 告别\n这是第三章的内容，也是最后一段。",
    })
    check("创建文稿", bool(doc["id"]))

    # 3. 项目
    p = req("POST", "/api/projects", {"title": "测试作品", "document_id": doc["id"], "voice_id": v1["id"]})
    check("创建项目", bool(p["id"]))

    # 4. 生成声音轨
    g = req("POST", f"/api/projects/{p['id']}/generate")
    s = wait_job(g["job_id"])
    check("生成声音轨", s["status"] == "success", f"({s['message']})")
    if s["status"] != "success":
        print("FAILED:", str(s.get("result", {}).get("traceback", ""))[-800:])
        sys.exit(1)

    # 5. 自动章节（应识别 第X章 标题 → 3 章）
    chs = req("GET", f"/api/projects/{p['id']}/chapters")
    check("自动章节", len(chs) == 3, f"({len(chs)} 章: {[c['title'] for c in chs]})")
    check("章节时间", all("start" in c and "end" in c for c in chs))

    # 6. 保存自定义章节
    saved = req("POST", f"/api/projects/{p['id']}/chapters", {
        "chapters": [{"title": "自定义第一章", "start_seg": 0, "end_seg": 1}],
    })
    check("保存章节", saved[0]["title"] == "自定义第一章")

    # 7. 导出索引
    for fmt in ("cue", "lrc", "json"):
        r = req("POST", f"/api/projects/{p['id']}/chapters/export", {"format": fmt, "split": False})
        if fmt == "json":
            check(f"导出 {fmt.upper()}", isinstance(r, list) and len(r) > 0, f"({len(r)} 章)")
        else:
            check(f"导出 {fmt.upper()}", r.get("_raw") and len(r["_raw"]) > 0, f"({len(r.get('_raw', b''))}B)")

    # 8. 分段级参数
    seg = req("PATCH", f"/api/projects/{p['id']}/segments/0", {"params": {"speed": 1.2, "pitch": 2, "eq": {"low": 3}}})
    check("分段参数保存", seg["params"].get("speed") == 1.2 and seg["params"]["eq"]["low"] == 3)
    seg2 = req("DELETE", f"/api/projects/{p['id']}/segments/0/params")
    check("分段参数清除", seg2["params"] == {})

    # 9. 对比试听（2 个声音）
    c = req("POST", "/api/synthesis/compare", {"voice_ids": [v1["id"], v2["id"]], "text": "对比试听文本。"})
    sc = wait_job(c["job_id"])
    check("对比试听", sc["status"] == "success" and len(sc["result"]["items"]) == 2,
          f"({sc['status']}, {len(sc.get('result', {}).get('items', []))} 个)")
    if sc["status"] == "success":
        paths = [it["path"] for it in sc["result"]["items"] if it.get("path")]
        check("对比音频可播", len(paths) == 2, f"({paths[0]})")

    # 10. 混音参数：闪避曲线 + 淡入淡出 + 分段参数
    req("PATCH", f"/api/projects/{p['id']}/segments/0", {"params": {"speed": 1.15}})
    req("PATCH", f"/api/projects/{p['id']}", {
        "params": {
            "speed": 1.05, "pitch": 1, "volume": 1.2, "fade_in": 0.5, "fade_out": 0.5,
            "ducking_mode": "curve",
            "ducking_curve": [
                {"pct": 0, "level": 0.1}, {"pct": 40, "level": 0.7},
                {"pct": 60, "level": 0.7}, {"pct": 100, "level": 0.1},
            ],
        },
    })

    # 11. 渲染成品
    r = req("POST", f"/api/projects/{p['id']}/render")
    s = wait_job(r["job_id"])
    check("渲染成品（闪避曲线）", s["status"] == "success", f"({s['message']})")
    if s["status"] != "success":
        print("FAILED:", str(s.get("result", {}).get("traceback", ""))[-1000:])
        sys.exit(1)

    # 12. 成品信息
    detail = req("GET", f"/api/projects/{p['id']}")
    check("成品路径/时长", bool(detail["final_path"]) and detail["duration"] > 0,
          f"({detail['final_path']}, {detail['duration']}s)")
    pk = req("GET", f"/api/audio/{detail['final_path']}/peaks")
    check("成品波形", len(pk["peaks"]) > 100)

    # 13. 分章 ZIP
    z = req("POST", f"/api/projects/{p['id']}/chapters/export", {"format": "cue", "split": True})
    check("分章 ZIP", bool(z.get("file")), f"({z.get('file')})")
    if z.get("file"):
        import urllib.parse

        zr = urllib.request.urlopen(BASE + "/api/audio/" + urllib.parse.quote(z["file"]), timeout=120)
        zdata = zr.read()
        check("ZIP 可下载", zdata[:2] == b"PK", f"({len(zdata)}B)")

    # 清理
    req("DELETE", f"/api/projects/{p['id']}")
    req("DELETE", f"/api/documents/{doc['id']}")
    req("DELETE", f"/api/voices/{v1['id']}")
    req("DELETE", f"/api/voices/{v2['id']}")

    print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

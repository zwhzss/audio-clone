"""group_units / snap_cuts 的纯函数测试（不跑模型、不碰 GPU）。

覆盖的是「分片不合理」的核心承诺：刀口只落在引号已闭合的句末，词和对话不会被劈开。
"""
import re
import sys

sys.path.insert(0, ".")
from app.slicing import Group, _is_final, _open_depth, group_units, snap_cuts  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("OK  " if cond else "FAIL") + " " + name + ((" | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def U(text, start, end):
    return {"text": text, "start": start, "end": end, "chars": []}


def squash(s):
    return re.sub(r"\s+", "", s or "")


DIALOG = [
    U("周铁山一抱拳：", 0.0, 1.0),
    U("“朋友，", 1.0, 2.0),
    U("在下赵猛，", 2.0, 3.0),
    U("路过宝地，", 3.0, 4.0),
    U("讨碗酒喝。”", 4.0, 5.5),
    U("赵猛一抖缰绳。", 5.5, 7.0),
    U("半年过去，", 7.0, 8.2),
    U("他眼珠一转，", 8.2, 9.0),
    U("虚晃一刀，", 9.0, 10.0),
    U("拨马便走。", 10.0, 11.4),
]

print("=== 1. 引号未闭合处不得断段（把上限压到 12 字逼它断）===")
gs = group_units(DIALOG, "para", max_chars=12, max_sec=100.0, min_piece=0.0)
for g in gs:
    print("   [%6.2f-%6.2f] %s" % (g.start, g.end, g.text))
check("每片引号都成对", all(_open_depth(g.text) == 0 for g in gs),
      [g.text for g in gs if _open_depth(g.text)])
check("对话没被劈开（“朋友，不单独成片）", all(g.text != "“朋友，" for g in gs))
check("刀口只落在标点后（绝不劈词）",
      all(g.text and g.text[-1] in "。！？!?…”』」）)》" or g.text[-1] in "，、,;：" for g in gs),
      [g.text[-3:] for g in gs if g.text and g.text[-1] not in "。！？…，、；”"])
check("非句末收尾的片都带 cap_comma 标记",
      all(_is_final(g.text) or "cap_comma" in (g.flags or "") for g in gs),
      [(g.text[-6:], g.flags) for g in gs if not _is_final(g.text)])
check("拼回去 == 全部识别文本（无损）",
      squash("".join(g.text for g in gs)) == squash("".join(u["text"] for u in DIALOG)))
check("边界单调且不重叠",
      all(gs[i].end <= gs[i + 1].start + 0.001 for i in range(len(gs) - 1)))
check("首尾覆盖整段", abs(gs[0].start - 0.0) < 0.001 and abs(gs[-1].end - 11.4) < 0.001)

print("\n=== 2. 句子模式 vs 段落模式 ===")
sent = group_units(DIALOG, "sentence", max_chars=300, max_sec=30.0, min_piece=0.0)
para = group_units(DIALOG, "para", max_chars=300, max_sec=30.0, min_piece=0.0)
print("   句模式 %d 片 %s" % (len(sent), [g.text for g in sent]))
print("   段模式 %d 片 %s" % (len(para), [g.text for g in para]))
check("句模式片数 >= 段模式", len(sent) >= len(para), (len(sent), len(para)))
check("段模式在 300 字内合成一片（整段完整）", len(para) == 1 and len(para[0].text) >= 40,
      [len(g.text) for g in para])
check("句模式每片都是完整一句（句末标点，可跟收尾引号）",
      all(_is_final(g.text) for g in sent), [g.text[-4:] for g in sent])

print("\n=== 3. 时长上限触发断段 ===")
long_u = [U("第%d句说得很长一点。" % i, i * 5.0, i * 5.0 + 4.5) for i in range(10)]
g30 = group_units(long_u, "para", max_chars=300, max_sec=12.0, min_piece=0.0)
check("max_sec=12 会切成多片", len(g30) >= 4, len(g30))
check("每片时长不超上限的 1.3 倍+一个从句", all(g.end - g.start <= 12.0 * 1.3 + 4.6 for g in g30),
      [round(g.end - g.start, 1) for g in g30])
check("上限逼出的断点仍在标点后", all(_is_final(g.text) for g in g30), [g.text[-5:] for g in g30])

print("\n=== 4. 碎块并入邻片 ===")
frag = [U("好。", 0.0, 0.4), U("这一句正常长度够长了。", 0.4, 3.0), U("短。", 3.0, 3.3)]
gm = group_units(frag, "sentence", max_chars=300, max_sec=30.0, min_piece=1.0)
check("0.4s 的碎块被并进邻片", len(gm) == 1 and gm[0].text == "好。这一句正常长度够长了。短。",
      [g.text for g in gm])

print("\n=== 5. 兜底标记 ===")
noend = [U("全程没有句末标点", 0.0, 1.0), U("一直往下说", 1.0, 2.0)]
gn = group_units(noend, "para", max_chars=300, max_sec=30.0, min_piece=0.0)
check("无句末标点时标 no_break", gn and "no_break" in gn[0].flags, gn[0].flags if gn else "无片")
openq = [U("他说：“打开的引号没有关闭", 0.0, 1.0), U("后面还是没关", 1.0, 2.0)]
gq = group_units(openq, "para", max_chars=300, max_sec=30.0, min_piece=0.0)
check("引号不闭合标 quote", gq and "quote" in gq[0].flags, gq[0].flags if gq else "无片")

print("\n=== 6. 刀口吸附到静音中点 ===")
grp = [Group("前一句说完。", 0.0, 5.0), Group("后一句接着说。", 5.0, 9.0)]
sil = [(4.8, 5.4), (8.9, 9.5)]
sn = snap_cuts(grp, sil, total=9.5, window=0.4)
check("边界吸附到静音中点 5.10", abs(sn[0].end - 5.1) < 0.001, sn[0].end)
check("两片共享同一刀口", abs(sn[1].start - sn[0].end) < 0.001)
check("首片起点不动、末片终点收敛到总长", abs(sn[0].start) < 1e-6 and sn[1].end <= 9.5)
sn2 = snap_cuts(grp, [], total=9.5, window=0.4)
check("没有静音时保留标点位置", abs(sn2[0].end - 5.0) < 0.06, sn2[0].end)
# 相邻两个刀口都往同一段静音吸，不能重叠
grp3 = [Group("甲。", 0.0, 5.0), Group("乙。", 5.0, 5.2), Group("丙。", 5.2, 9.0)]
sn3 = snap_cuts(grp3, [(4.9, 5.3)], total=9.0, window=0.4)
check("吸附后的刀口严格递增", sn3[0].end < sn3[1].end, (sn3[0].end, sn3[1].end))

print("\n结果: %s" % ("全部通过" if not fails else "有 %d 项不合格: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)

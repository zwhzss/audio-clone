import re
import sys
sys.path.insert(0, ".")
from app.text_utils import chunk_text, split_sentences

OPEN = "“‘「『（(《"
CLOSE = "”’」』）)》"
fails = []


def check(name, text, max_chars=300):
    cs = chunk_text(text, max_chars)
    j = re.sub(r"\s+", "", "".join(cs))
    ref = re.sub(r"\s+", "", text)
    has_word = bool(re.search(r"[\w\u3400-\u9fff]", text))
    bad = []
    if j != ref and has_word:
        bad.append("有损：拼回去与原文不一致\n        得到=%r\n        原文=%r" % (j, ref))
    if not has_word and cs:
        bad.append("纯标点输入不应产生分片: %r" % cs)
    for c in cs:
        if not re.search(r"[\w\u3400-\u9fff]", c):
            bad.append("纯标点分片 %r" % c)
        if c[0] in "”』」）》" and has_word and re.sub(r"\s+", "", text).count("”") <= text.count("“"):
            bad.append("以收尾引号开头（原稿引号是成对的）%r" % c[:8])
        if c[-1] in "“‘（《":
            bad.append("以开启引号结尾 %r" % c[-8:])
        for o, cl in (("“", "”"), ("‘", "’")):
            # 只在原稿本身成对时要求分片内也成对
            if text.count(o) == text.count(cl) and c.count(o) != c.count(cl):
                bad.append("引号不成对(%d%s/%d%s): %r" % (c.count(o), o, c.count(cl), cl, c[:40]))
        if len(c) > max_chars and c.count("“") != c.count("”"):
            bad.append("超 max_chars 且引号残缺: %d" % len(c))
    status = "OK " if not bad else "FAIL"
    print("%s %-10s %d 字 -> %d 片  长度%s" % (status, name, len(text), len(cs), [len(c) for c in cs]))
    for b in bad:
        print("      -", b)
    if bad:
        fails.append(name)
    return cs


print("=== 用户的《夜闯聚贤楼》")
TEXT = open("REDACTED_TMP/test_script.txt", encoding="utf-8").read().strip()
cs = check("评书", TEXT)
for i, c in enumerate(cs, 1):
    print("   #%2d(%2d字) %s" % (i, len(c), c))

print("\n=== 关键对话是否保持完整")
for probe in ("“站住！什么人？”", "“哼，讨酒喝？我看你是讨打吧！”",
              "“朋友，别动手。我赵猛走南闯北，讲究一个‘理’字。",
              "“那就别怪我铁掌无情！”"):
    hit = [c for c in cs if probe in c]
    print("   %-34s -> %s" % (probe[:32], "在片 #%s" % [cs.index(h) + 1 for h in hit] if hit else "*** 被切碎了 ***"))
    if not hit:
        fails.append("残缺:" + probe)

print("\n=== 边界用例")
check("嵌套引号", "他说：“你先走，‘小心路滑’，听见没？”她点点头。")
check("同段多句对话", "他喊了一声：“站住！什么人？”赵猛一抱拳：“讨碗酒喝。”那人冷笑：“去吧。”")
check("引号内多句", "赵猛微微一笑：“朋友，别动手。我走南闯北，讲究一个‘理’字。要是非要动手——”（此处一拍醒木）“那就别怪我铁掌无情！”")
check("连续省略号", "他走了……谁也没拦住……唉。")
check("引号后括号", "赵猛微微一笑：“那就别怪我铁掌无情！”（醒木一拍）众人皆惊。")
check("半角引号", 'He said: "Stop! Who goes there?" Nobody answered. Then he left.')
check("英文缩写", "I don't know. He said it's fine, and walked away.")
check("小数不切", "模型 v2.5 很好用。这次跑出了 3.5 分的成绩。")
check("中英混排", "此人姓赵名猛，江湖人称 Iron Palm。他来到聚贤楼下，抬头一看。")
check("未闭合引号", "他说：“今天一定要拿下这件事，然后转身就走了。剩下半句没说完。”")
check("孤立收尾引号", "”他转身就走。第二天又来了。")
check("超长单句", "他" + "推开房门一句话也没说就走了，" * 12)
check("纯逗号段", "，，，")
check("空文本", "   \n\n  ")
check("诗歌分行", "床前明月光，\n疑是地上霜。\n举头望明月，\n低头思故乡。")
check("多段落", "第一段结束。第二段开始。\n\n第二段第二句！第三段？第三段尾。")

print("\n=== 期望的分片数：chunk_text 按自然段，split_sentences 按句子")
# chunk_text 现在一段（<=max_chars）就是一片；句子级由 split_sentences 负责
SRC = {
    "同段多句对话": "他喊了一声：“站住！什么人？”赵猛一抱拳：“讨碗酒喝。”那人冷笑：“去吧。”",
    "引号内多句": "赵猛微微一笑：“朋友，别动手。我走南闯北，讲究一个‘理’字。要是非要动手——”（此处一拍醒木）“那就别怪我铁掌无情！”",
    "半角引号": 'He said: "Stop! Who goes there?" Nobody answered. Then he left.',
    "英文缩写": "I don't know. He said it's fine, and walked away.",
    "小数不切": "模型 v2.5 很好用。这次跑出了 3.5 分的成绩。",
}
CHUNK_EXPECT = {  # 单段 -> 1 片
    "同段多句对话": 1, "引号内多句": 1, "半角引号": 1, "英文缩写": 1, "小数不切": 1,
}
SENT_EXPECT = {   # 句子级口径不变
    "同段多句对话": 3, "引号内多句": 1, "半角引号": 3, "英文缩写": 2, "小数不切": 2,
}
for name in SRC:
    nc = len(chunk_text(SRC[name]))
    ns = len(split_sentences(SRC[name]))
    ok = nc == CHUNK_EXPECT[name] and ns == SENT_EXPECT[name]
    print("   %-8s 分片 期望%d 实得%d | 分句 期望%d 实得%d  %s" % (
        name, CHUNK_EXPECT[name], nc, SENT_EXPECT[name], ns, "OK" if ok else "*** 不符 ***"))
    if not ok:
        fails.append("片数:" + name)

print("\n=== 自然段优先专项")
PARA = "甲说完走了。\n\n乙接着说：“站住！什么人？”他也走了。\n\n丙道：“好吧。”"
pc = chunk_text(PARA)
print("   3 个自然段 -> %d 片 长度%s" % (len(pc), [len(x) for x in pc]))
if len(pc) != 3:
    fails.append("自然段数")
if pc and "“站住！什么人？”" not in pc[1]:
    fails.append("自然段内引号被打散")
# 超长段必须退回按句拆，且不能劈开引号
LONG = ("他说：“第一句话很长很长很长很长很长很长。”" * 8) + "旁人应了一声。"
lc = chunk_text(LONG, 120)
print("   超长段(%d字) mc=120 -> %d 片 长度%s 引号成对=%s" % (
    len(LONG), len(lc), [len(x) for x in lc],
    all(x.count("“") == x.count("”") for x in lc)))
if len(lc) < 2:
    fails.append("超长段未拆分")
if not all(x.count("“") == x.count("”") for x in lc):
    fails.append("超长段拆开后引号残缺")
if re.sub(r"\s+", "", "".join(lc)) != re.sub(r"\s+", "", LONG):
    fails.append("超长段有损")

print("\n=== max_chars 敏感性（同一文稿）")
for mc in (60, 120, 300):
    check("mc=%d" % mc, TEXT, mc)

print("\n结果: %s" % ("全部通过" if not fails else "有 %d 项不合格: %s" % (len(fails), sorted(set(fails)))))

"""文稿处理：分句/分段（供 TTS 与项目使用）。"""
from __future__ import annotations

import re

# 句末标点：只有「不在引号内」时才作为断句点
_SENT_END = "。！？；!?;"
_ELLIPSIS = "…"


def _is_end(ch: str, text: str, idx_after: int) -> bool:
    """ASCII 句点也要断句（整段英文挤成一句会触发模型提前 EOS 吞字），
    但小数 `3.5` 中间的点不算句末。"""
    if ch in _SENT_END or ch == _ELLIPSIS:
        return True
    if ch == ".":
        prev = text[idx_after - 2] if idx_after >= 2 else ""
        nxt = text[idx_after] if idx_after < len(text) else ""
        return not (prev.isdigit() and nxt.isdigit())
    return False

# 句末标点之后可以安全吸收的收尾符号（引号/括号/书名号等）
_CLOSE = "”’\"」』）】》"
# 成对符号：开启 → 闭合。开启后引号内的 。！？ 不断句。
_PAIRS = {"“": "”", "‘": "’", "《": "》", "「": "」", "『": "』", "(": ")", "（": "）"}
_CLOSER_OF = {v: k for k, v in _PAIRS.items()}
_WORD = re.compile(r"[\w\u3400-\u9fff]")


def split_sentences(text: str) -> list[str]:
    """按句子切分，但**引号内的句末标点不断句**。

    评书/小说文稿里 `“站住！什么人？”` 若按 。！？ 硬切会得到两个残缺分片
    （`“站住！` 和 `什么人？”`），合成时各自被当成一句话，读起来断裂且引号不成对。

    规则：
    1. 用栈跟踪成对符号（“ ‘ 《 「 『 ( （），栈非空时句末标点不断句；
    2. 闭合符号及其后连续的闭合符号一起吞进本句，杜绝只含 `”` 的碎片；
    3. 引号内以句末标点结束、紧接着闭合，才算真正的句尾（同段多句对话必须靠这条断开）；
    4. ASCII 双引号靠出现次数的奇偶判开合；
    5. 纯标点碎片不单独成句，并入下一句。
    """
    out: list[str] = []
    buf = ""
    stack: list[str] = []
    dquote = 0
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal buf
        s = buf.strip()
        buf = ""
        if s and _WORD.search(s):
            out.append(s)
        elif s:
            buf = s                    # 无实义内容则并入下一句

    def swallow_spaces() -> None:
        nonlocal buf, i
        while i < n and text[i] in " \t\n":
            buf += text[i]
            i += 1

    while i < n:
        ch = text[i]
        i += 1

        if ch == '"':
            dquote += 1
            if dquote % 2 == 1:        # 奇数次出现 = 开启引号
                stack.append('"')
                buf += ch
                continue

        if ch in _PAIRS:
            stack.append(_PAIRS[ch])
            buf += ch
            continue

        if ch in _CLOSE:
            pre_tail = buf[-1] if buf else ""
            if stack and ch == stack[-1]:
                stack.pop()
            buf += ch
            while i < n and text[i] in _CLOSE:      # `”）` `！」` 之类连续闭合
                nxt = text[i]
                if stack and nxt == stack[-1]:
                    stack.pop()
                buf += nxt
                i += 1
            if not stack and pre_tail in _SENT_END + _ELLIPSIS + ".":
                swallow_spaces()
                flush()
            continue

        buf += ch
        if ch in _ELLIPSIS:
            while i < n and text[i] == _ELLIPSIS:
                buf += text[i]
                i += 1
        if stack or not _is_end(ch, text, i):
            continue
        swallow_spaces()
        flush()

    flush()
    return [s for s in out if _WORD.search(s)]


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 压缩多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def prepare_tts_text(text: str) -> str:
    """TTS 文本预处理：防止 GPT-SoVITS 在逗号/句号切分后吞掉句首字。

    关键修复：在逗号/句号后、下一个字之前插入一个极短停顿符号，
    让模型在切分点后有"喘息"，不会吃掉句首字。
    同时保留诗歌换行（模型会利用换行判断停顿）。
    """
    # 在句号/逗号后、非空字符前插入一个空格停顿（让模型在切分时不会吞字）
    text = re.sub(r"([。！？；!?;])\s*([^。\n])", r"\1 \2", text)
    # 诗歌换行保留（模型会利用换行判断停顿）
    return text.strip()


def _safe_cut_points(sentence: str) -> list[int]:
    """可以安全下刀的位置：引号深度为 0 时的 ，、; 之后。"""
    pts, depth = [], 0
    for i, ch in enumerate(sentence):
        if ch in _PAIRS:
            depth += 1
        elif ch in _CLOSE and depth:
            depth -= 1
        elif ch in "，、,;" and depth == 0:
            pts.append(i + 1)
    return pts


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    """单句超长时只在引号外的逗号处下刀；找不到安全点就整句保留。

    宁可某一片略超 max_chars，也不把 `“朋友，别动手。我赵猛……”` 从引号中间劈开
    ——那正是「分片缺半边引号」的来源。切片位置连续，不丢字。
    """
    if len(sentence) <= max_chars:
        return [sentence]
    pts = _safe_cut_points(sentence)
    parts, start = [], 0
    while len(sentence) - start > max_chars:
        cand = [p for p in pts if start < p - start <= max_chars]
        if not cand:
            break                      # 没有安全点：整句保留
        cut = max(cand)
        parts.append(sentence[start:cut].strip())
        start = cut
    tail = sentence[start:].strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def chunk_text(text: str, max_chars: int = 300) -> list[str]:
    """切片优先按**自然段**：一段（<= max_chars）就是一片，和文字版评书的分段一致。

    以前是一段一句一片，把 `一声令下，众喽啰蜂拥而上。周铁山回头一挥手：“护住镖车！”
    镖局众人“哗啦”一声…` 拆成三片，每片各自合成，语气和节奏在句间被切断。

    段落超过 max_chars 时退回按句子拆（引号内的 。！？ 不断句），
    单句再超限则用 _hard_split 只在引号外的逗号下刀。
    """
    text = normalize_text(text)
    if not text:
        return []
    chunks: list[str] = []
    for para in [p.strip() for p in text.split("\n") if p.strip()]:
        if len(para) <= max_chars:
            if _WORD.search(para):
                chunks.append(para)
            continue
        for s in split_sentences(para):
            chunks.extend(_hard_split(s, max_chars))
    return [c for c in chunks if c and _WORD.search(c)]

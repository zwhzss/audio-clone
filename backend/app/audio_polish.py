"""音频润色：EQ/混响/压缩/降噪/去齿音/响度标准化等专业声学处理（FFmpeg 滤镜链）。"""
from __future__ import annotations

from pathlib import Path

from .audio_utils import _run


def build_polish_filter(params: dict) -> tuple[str, bool]:
    """根据参数构建润色滤镜。

    返回 (滤镜串, 是否 filter_complex)。混响需要 asplit/amix 干湿混合 → complex 模式。
    滤镜顺序：降噪 → 滤波 → EQ → 去齿音 → 压缩 → 混响(dry/wet) → 响度标准化
    """
    pre: list[str] = []   # 混响前
    post: list[str] = []  # 混响后（响度）

    # 1. 降噪
    nr = float(params.get("noise_reduction", 0))
    if nr > 0:
        nf = max(0.01, nr / 100 * 0.5)
        pre.append(f"afftdn=nr={nf:.4f}:nf=-{int(nr / 3) + 20}")

    # 2. 高通/低通
    hp = float(params.get("highpass", 0))
    if hp > 20:
        pre.append(f"highpass=f={hp:.0f}")
    lp = float(params.get("lowpass", 0))
    if lp > 0:
        pre.append(f"lowpass=f={lp:.0f}")

    # 3. 5 段 EQ
    eq = params.get("eq") or {}
    eq_spec = [("low", 120, "bass"), ("mid_low", 500, "eq"), ("mid", 1500, "eq"), ("mid_high", 4000, "eq"), ("high", 9000, "treble")]
    for key, freq, kind in eq_spec:
        g = float(eq.get(key, 0))
        if abs(g) > 0.05:
            if kind == "bass":
                pre.append(f"bass=g={g:.2f}:f={freq}")
            elif kind == "treble":
                pre.append(f"treble=g={g:.2f}:f={freq}")
            else:
                pre.append(f"equalizer=f={freq}:width_type=o:width=1.0:g={g:.2f}")

    # 4. 去齿音
    ds = float(params.get("deesser", 0))
    if ds > 0:
        pre.append(f"deesser=i={min(1.0, ds / 100):.2f}:m={min(1.0, ds / 100 + 0.2):.2f}")

    # 5. 压缩
    comp = params.get("compressor") or {}
    if comp.get("enabled"):
        pre.append(
            f"acompressor=threshold={float(comp.get('threshold', -20)):.1f}dB"
            f":ratio={float(comp.get('ratio', 3)):.1f}"
            f":attack={float(comp.get('attack', 10)):.0f}"
            f":release={float(comp.get('release', 200)):.0f}"
            f":makeup={float(comp.get('makeup', 3)):.1f}dB"
        )

    # 6. 响度标准化（混响后）；loudness 为负值目标 LUFS（如 -16），0=关闭
    #    I 参数范围 [-70, -5]，前端可能传正值需修正
    lu = float(params.get("loudness", 0))
    if lu != 0:
        if lu > 0:
            lu = -lu                          # 前端可能传正值，自动转负
        lu = max(-70, min(-5, lu))            # 钳位到有效范围
        post.append(f"loudnorm=I={lu:.1f}:TP=-1.5:LRA=11")

    pre_str = ",".join(pre) if pre else "anull"
    post_str = ",".join(post) if post else "anull"

    # 7. 混响：dry/wet 干湿混合（asplit + amix，音量不塌陷）
    rv = float(params.get("reverb_amount", 0))
    if rv > 0:
        decay = float(params.get("reverb_decay", 1.5))
        ms = max(10, int(decay * 1000))
        d1 = max(10, min(90000, int(ms * 0.35)))
        d2 = max(10, min(90000, int(ms * 0.7)))
        d3 = max(10, min(90000, ms))
        wet_vol = min(0.9, rv / 100 * 0.9)   # 混响湿声音量（0~0.9）
        fc = (
            f"[0:a]{pre_str}[a];"
            f"[a]asplit=2[dry][wet];"
            f"[dry]volume=1.0[d];"
            f"[wet]aecho=1.0:0.35:{d1}|{d2}|{d3}:0.55|0.4|0.28,volume={wet_vol:.3f}[w];"
            f"[d][w]amix=inputs=2:duration=first:normalize=0[mix];"
            f"[mix]{post_str}[aout]"
        )
        return fc, True

    chain = pre + post
    return (",".join(chain) if chain else "anull"), False


def render_polish(
    src: str | Path,
    params: dict,
    out_path: str | Path,
    fmt: str = "mp3",
) -> str:
    """渲染润色音频。"""
    flt, is_complex = build_polish_filter(params)
    args = ["-i", str(src)]
    if is_complex:
        args += ["-filter_complex", flt, "-map", "[aout]"]
    else:
        args += ["-af", flt]
    if fmt == "mp3":
        args += ["-c:a", "libmp3lame", "-b:a", "192k"]
    else:
        args += ["-c:a", "pcm_s16le"]
    args.append(str(out_path))
    r = _run(args)
    if r.returncode != 0:
        raise RuntimeError(f"润色渲染失败: {r.stderr.decode('utf-8', 'ignore')[-800:]}")
    return str(out_path)

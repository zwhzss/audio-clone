"""GPT-SoVITS 微调训练流水线：切片 → ASR 标注 → 预处理(1a/1b/1c) → s2 训练 → s1 训练。

复用 GPT-SoVITS 官方脚本（tools/slice_audio.py、tools/asr/funasr_asr.py、
GPT_SoVITS/prepare_datasets/*.py、s2_train.py、s1_train.py），与 webui 逻辑一致。
产出：SoVITS_weights_v2/<exp>.pth（音色模型）+ GPT_weights_v2/<exp>.ckpt（语义模型）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

GPT_SOVITS_ROOT = Path("F:/GPT-SoVITS-main")
PYTHON = r"F:\envs\gpt_sovits\Scripts\python.exe"
EXP_ROOT = GPT_SOVITS_ROOT / "logs"
WEIGHTS_S2 = GPT_SOVITS_ROOT / "SoVITS_weights_v2"
WEIGHTS_S1 = GPT_SOVITS_ROOT / "GPT_weights_v2"

PRETRAINED_S2G = "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth"
PRETRAINED_S2D = "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2D2333k.pth"
PRETRAINED_S1 = "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"
BERT_DIR = "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large"
CNHUBERT_DIR = "GPT_SoVITS/pretrained_models/chinese-hubert-base"
SV_PATH = "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt"
S2_CONFIG = "GPT_SoVITS/configs/s2.json"
S1_CONFIG = "GPT_SoVITS/configs/s1longer-v2.yaml"

ProgressCb = callable = None  # type: ignore


def _env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    conda_bin = r"F:\envs\gpt_sovits\Library\bin"
    env.update({
        "PYTHONUTF8": "1",
        "HF_ENDPOINT": "https://hf-mirror.com",
        "PYTHONPATH": str(GPT_SOVITS_ROOT) + os.pathsep + str(GPT_SOVITS_ROOT / "GPT_SoVITS"),
        "GLOO_SOCKET_IFNAME": "WLAN",          # Windows gloo 选网卡
        "GLOO_DEVICE_TRANSPORT": "TCP",
        "MASTER_ADDR": "192.168.3.13",
        "PATH": conda_bin + os.pathsep + env.get("PATH", ""),
        "FFMPEG_BINARY": conda_bin + r"\ffmpeg.exe",
        # 关键修复：conda site-packages 对当前用户只读（BUILTIN\Users 仅 RX），
        # librosa 导入时 numba 会在其 __pycache__ 建缓存，写入被拒后 tempfile
        # 在 Windows 上 PermissionError 死循环（_mkstemp_inner continue 重试），
        # 表现为训练在 loaded pretrained 后 100% CPU 空转卡死。
        # 将 numba 缓存重定向到用户可写目录，彻底绕开。
        "NUMBA_CACHE_DIR": str(Path.home() / "AppData" / "Local" / "Temp" / "numba_cache_audio"),
    })
    if extra:
        env.update(extra)
    return env


def _decode_line(raw: bytes) -> str:
    """子进程输出智能解码：训练日志为 UTF-8（PYTHONUTF8=1），但 Windows
    系统错误文本（如 DLL/内存错误）常以 GBK 输出，UTF-8 解码会成乱码。
    启发式：两种编码都试，取 U+FFFD 替换更少者。"""
    line = raw.decode("utf-8", errors="replace")
    if "\ufffd" not in line:
        return line
    alt = raw.decode("gbk", errors="replace")
    if alt.count("\ufffd") < line.count("\ufffd"):
        return alt
    return line


def _run(args: list[str], env: dict, cwd: Path, update, msg: str, timeout: int = 7200) -> None:
    update(0, msg)
    proc = subprocess.Popen(
        args, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert proc.stdout is not None
    log_lines: list[str] = []
    for raw in proc.stdout:  # 逐行 bytes，智能解码（训练可能几小时，保持实时输出）
        line = _decode_line(raw).rstrip()
        if line:
            log_lines.append(line)
            if len(log_lines) > 50:
                log_lines.pop(0)
            update(None, line[-120:])
    proc.wait(timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(log_lines[-25:])
        hint = ""
        # 常见失败模式给出可操作提示（尤其是 Windows 内存/虚拟内存不足）
        low = tail.lower()
        if "dll load failed" in low or "页面文件" in tail or "out of memory" in low or "cannot allocate" in low:
            hint = (
                "\n\n[提示] 内存/虚拟内存不足导致训练进程崩溃：\n"
                "1. 训练前关闭其他占用内存的程序（浏览器、GPT-SoVITS API 服务、旧训练进程等）；\n"
                "2. 增大 Windows 虚拟内存：设置 → 系统 → 关于 → 高级系统设置 → 性能设置 → 高级 → 虚拟内存，\n"
                "   建议固定设为 32768~65536 MB，并优先放在空间充足的磁盘（当前 C 盘仅剩约 14GB）；\n"
                "3. 减小训练数据量或降低 batch_size（训练配置中 SoVITS batch_size 改为 2）。"
            )
        raise RuntimeError(f"{msg} 失败 (exit {proc.returncode})\n{tail[-1500:]}{hint}")


class GPTSoVITSTrainer:
    def __init__(self, exp_name: str, update: ProgressCb = None):
        self.exp_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", exp_name)[:40]
        self.exp_dir = EXP_ROOT / self.exp_name
        self.update = update or (lambda p, m: None)
        # 默认使用官方预训练模型
        self._pretrained_s2G = str(GPT_SOVITS_ROOT / PRETRAINED_S2G)
        self._pretrained_s2D = str(GPT_SOVITS_ROOT / PRETRAINED_S2D)
        self._pretrained_s1 = str(GPT_SOVITS_ROOT / PRETRAINED_S1)

    def override_pretrained(self, pretrained_s2G: str = "", pretrained_s2D: str = "", pretrained_s1: str = ""):
        """覆盖基础模型路径（迭代训练：从上一轮模型继续）。"""
        if pretrained_s2G:
            self._pretrained_s2G = pretrained_s2G
        if pretrained_s2D:
            self._pretrained_s2D = pretrained_s2D
        if pretrained_s1:
            self._pretrained_s1 = pretrained_s1

    # ---------- 0. 训练环境预检 ----------

    def preflight(self) -> None:
        """训练前用子进程提前 import 关键依赖，尽早暴露 DLL/内存问题。
        否则会在 s2 训练首个 batch 才崩溃（浪费数小时预处理）。"""
        probe = (
            "import sys\n"
            "mods = ['torch', 'librosa', 'scipy', 'soundfile', 'numpy', 'tqdm']\n"
            "for m in mods:\n"
            "    try:\n"
            "        __import__(m)\n"
            "    except Exception as e:\n"
            "        print(f'PREFLIGHT_FAIL: {m}: {e}', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "print('PREFLIGHT_OK')\n"
        )
        proc = subprocess.Popen(
            [str(PYTHON), "-c", probe], cwd=str(GPT_SOVITS_ROOT), env=_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None
        out_lines = [_decode_line(raw).rstrip() for raw in proc.stdout]
        proc.wait(timeout=300)
        out = "\n".join(out_lines)
        if proc.returncode != 0 or "PREFLIGHT_OK" not in out:
            raise RuntimeError(
                "训练环境预检失败：无法加载训练依赖（多为内存/虚拟内存不足或环境损坏）\n"
                f"{out.strip()}\n"
                "[提示] 请关闭其他占用内存的程序（浏览器、GPT-SoVITS API 服务、旧训练进程），\n"
                "并在 Windows 中增大虚拟内存（页面文件）后重试；若仍失败请检查 conda 环境 gpt_sovits 是否完整。"
            )

    # ---------- 1. 切片 ----------

    def slice_audio(self, input_dir: Path, exp_dir: Path | None = None, threshold: int = -40) -> Path:
        exp = exp_dir or self.exp_dir
        out = exp / "1-slice"
        out.mkdir(parents=True, exist_ok=True)
        _run(
            [str(PYTHON), "tools/slice_audio.py", str(input_dir), str(out),
             str(threshold), "4000", "300", "10", "500", "0.9", "0.25", "0", "1"],
            _env(), GPT_SOVITS_ROOT, self.update, "音频切片",
        )
        files = list(out.glob("*.wav"))
        if not files:
            raise RuntimeError("切片后没有生成有效音频，请检查输入音频是否有声音")
        return out

    # ---------- 2. ASR 自动标注 ----------

    def asr_label(self, wav_dir: Path, speaker: str = "speaker0", exp_dir: Path | None = None) -> Path:
        exp = exp_dir or self.exp_dir
        _run(
            [str(PYTHON), "tools/asr/funasr_asr.py", "-i", str(wav_dir), "-o", str(exp), "-l", "zh"],
            _env(), GPT_SOVITS_ROOT, self.update, "ASR 文本标注（首次需下载模型）",
            timeout=10800,
        )
        src = exp / f"{wav_dir.name}.list"
        if not src.exists():
            # 回退：用输入目录名
            cands = list(exp.glob("*.list"))
            if not cands:
                raise RuntimeError("ASR 未生成标注文件")
            src = cands[0]
        dst = exp / "1-name2text.txt"
        lines = []
        for line in src.read_text(encoding="utf-8").strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 4 and parts[3].strip():
                lines.append(f"{parts[0]}|{speaker}|{parts[2]}|{parts[3]}")
        if not lines:
            raise RuntimeError("ASR 标注结果为空（音频可能无人声）")
        dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return dst

    # ---------- 3. 预处理 ----------

    def _preprocess_env(self, exp_dir: Path | None = None) -> dict:
        exp = exp_dir or self.exp_dir
        return {
            "inp_text": str(exp / "1-name2text.txt"),
            "inp_wav_dir": str(exp / "1-slice"),
            "exp_name": self.exp_name,
            "opt_dir": str(exp),
            "i_part": "0",
            "all_parts": "1",
            "_CUDA_VISIBLE_DEVICES": "0",
            "is_half": "True",
            "version": "v2",
            "bert_pretrained_dir": str(GPT_SOVITS_ROOT / BERT_DIR),
            "cnhubert_base_dir": str(GPT_SOVITS_ROOT / CNHUBERT_DIR),
            "sv_path": str(GPT_SOVITS_ROOT / SV_PATH),
            "pretrained_s2G": str(GPT_SOVITS_ROOT / PRETRAINED_S2G),
            "s2config_path": str(GPT_SOVITS_ROOT / S2_CONFIG),
        }

    def preprocess(self, exp_dir: Path | None = None) -> None:
        exp = exp_dir or self.exp_dir
        env = _env(self._preprocess_env(exp))
        # 关键修复：1a/1b/1c 脚本对"输出文件已存在"会跳过处理（增量续跑设计）。
        # 若 exp 目录残留旧数据（切片/文本更新后）生成的特征，新训练数据永远无法
        # 进入特征提取，训练会静默使用旧数据。这里先把旧中间文件备份并清除，强制重处理。
        backup = exp / "preprocess_backup"
        for name in ("2-name2text-0.txt", "6-name2semantic-0.tsv"):
            p = exp / name
            if p.exists():
                backup.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(backup / name))
        for d in ("3-bert", "4-cnhubert", "5-wav32k"):
            dd = exp / d
            if dd.exists():
                bd = backup / d
                if bd.exists():
                    shutil.rmtree(bd)
                shutil.move(str(dd), str(bd))
        for script, name in (
            ("GPT_SoVITS/prepare_datasets/1-get-text.py", "文本音素处理(1a)"),
            ("GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py", "HuBERT 特征提取(1b)"),
            ("GPT_SoVITS/prepare_datasets/3-get-semantic.py", "语义 token 提取(1c)"),
        ):
            _run([str(PYTHON), "-s", script], env, GPT_SOVITS_ROOT, self.update, name, timeout=10800)
        # 合并多 part 输出（单卡只有 part-0）
        for part_name, merged in (
            ("2-name2text-0.txt", "2-name2text.txt"),
            ("6-name2semantic-0.tsv", "6-name2semantic.tsv"),
        ):
            src = exp / part_name
            if src.exists():
                shutil.copy2(src, exp / merged)
        if not (exp / "6-name2semantic.tsv").exists():
            raise RuntimeError("语义提取失败：未生成 6-name2semantic.tsv")

    def _stage_labeled_slices(self, exp_dir: Path, list_file: Path) -> Path:
        """把 train.list 引用的已标注切片原样放入 1-slice，并生成同名 1-name2text.txt。

        已有标注时必须跳过二次切片：slice_audio 会把一条用户切片再切成若干子片段，
        而标注只按文件名前缀取第一个匹配子片段，等于把整句文本配到只含半句的音频上。
        单田芳两次塌方的实测：训练音频中位仅 2.86s、最短 0.94s，34/345（9.9%）样本
        语速 >6.5 字/秒，最高 111 字 / 5.31s = 20.9 字/秒（正常中文约 4 字/秒）。
        """
        out = exp_dir / "1-slice"
        if out.exists():
            base = exp_dir / ("1-slice.stale-%s" % time.strftime("%Y%m%d%H%M%S"))
            stale = base
            i = 1
            while stale.exists():          # 同名目标会让 shutil.move 改成嵌套移动
                stale = Path("%s.%d" % (base, i)); i += 1
            shutil.move(str(out), str(stale))
            self.update(11, f"旧的派生切片缓存已移到 {stale.name}（可手动删除）")
        out.mkdir(parents=True, exist_ok=True)
        src_dir = list_file.parent
        rows, dropped = [], 0
        for line in list_file.read_text(encoding="utf-8").strip().splitlines():
            p = line.split("|")
            if len(p) < 4 or not p[0].strip().endswith(".wav") or not p[3].strip():
                dropped += 1
                continue
            f = src_dir / p[0]
            if not f.is_file():
                f = src_dir / "sliced" / p[0]
            if not f.is_file():
                dropped += 1
                continue
            shutil.copy2(f, out / p[0])
            rows.append("|".join(p))
        if not rows:
            raise RuntimeError("train.list 无可用条目：标注对应的音频全部缺失")
        (exp_dir / "1-name2text.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.update(12, f"已直接采用 {len(rows)} 条已标注切片"
                        + (f"（丢弃 {dropped} 条无效标注）" if dropped else ""))
        return out

    def _prepare_exp(self, exp_dir: Path, input_dir: Path, speaker: str) -> Path:
        """对指定实验目录执行「切片 → 标注(ASR或train.list) → 预处理」，产出训练特征。"""
        exp_dir.mkdir(parents=True, exist_ok=True)
        existing_list = input_dir / "train.list"
        if existing_list.exists() and existing_list.stat().st_size > 10:
            self.update(11, "train.list 已存在，跳过二次切片与 ASR 标注…")
            self._stage_labeled_slices(exp_dir, existing_list)
        else:
            wav_dir = self.slice_audio(input_dir, exp_dir)
            self.update(12, "切片完成，开始 ASR 标注…")
            self.asr_label(wav_dir, speaker, exp_dir)
        self.update(20, "标注完成，开始特征预处理…")
        self.preprocess(exp_dir)
        return exp_dir

    # ---------- 4. s2 训练（音色模型） ----------

    def train_s2(self, epochs: int = 10, data_exp_dir: Path | None = None) -> Path:
        data_exp = data_exp_dir or self.exp_dir
        cfg = json.loads((GPT_SOVITS_ROOT / S2_CONFIG).read_text(encoding="utf-8"))
        cfg["train"].update({
            "pretrained_s2G": self._pretrained_s2G,
            "pretrained_s2D": self._pretrained_s2D,
            "if_save_latest": True,
            "if_save_every_weights": True,   # 保存每轮权重到 save_weight_dir
            "save_every_epoch": 1,
            "gpu_numbers": "0",
            "epochs": epochs,
            "batch_size": 4,
        })
        cfg["data"]["exp_dir"] = str(data_exp)   # 数据源（音色训练用哪个 exp 的特征/音频）
        cfg["s2_ckpt_dir"] = str(self.exp_dir)   # SummaryWriter 日志目录
        cfg["save_weight_dir"] = str(WEIGHTS_S2)
        cfg["name"] = self.exp_name
        cfg["version"] = "v2"
        cfg["model"]["version"] = "v2"   # TextAudioSpeakerLoader 需要 model.version
        tmp_cfg = self.exp_dir / "tmp_s2.json"
        tmp_cfg.write_text(json.dumps(cfg), encoding="utf-8")
        # 关键：s2_train.py 的 checkpoint 保存位置是 hps.data.exp_dir/logs_s2_{version}，
        # 即数据源 exp 目录（独立源时是 __s2src），不是 self.exp_dir。
        # 必须先创建该目录并清理旧 checkpoint（残留会导致 resume 旧轮次/保存失败）。
        s2_ckpt_dir = data_exp / f"logs_s2_v2"
        s2_ckpt_dir.mkdir(parents=True, exist_ok=True)
        (self.exp_dir / "logs_s2_v2").mkdir(parents=True, exist_ok=True)  # 兼容主数据模式
        s2_ckpt_files = list(s2_ckpt_dir.glob("*.pth"))
        if s2_ckpt_files:
            backup_dir = s2_ckpt_dir / "ckpt_backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            for f in s2_ckpt_files:
                shutil.copy2(f, backup_dir / f.name)
                f.unlink(missing_ok=True)
            self.update(0, f"已备份旧 SoVITS checkpoint 到 {backup_dir}")
        WEIGHTS_S2.mkdir(parents=True, exist_ok=True)
        _run(
            [str(PYTHON), "-s", "GPT_SoVITS/s2_train.py", "--config", str(tmp_cfg)],
            _env(), GPT_SOVITS_ROOT, self.update, f"SoVITS 音色训练（{epochs} epoch）",
            timeout=36000,
        )
        WEIGHTS_S2.mkdir(parents=True, exist_ok=True)
        cands = sorted(WEIGHTS_S2.glob(f"{self.exp_name}*.pth"), key=lambda p: p.stat().st_mtime)
        if not cands:
            raise RuntimeError("s2 训练未产出模型文件")
        return cands[-1]

    # ---------- 5. s1 训练（语义模型） ----------

    def train_s1(self, epochs: int = 10, data_exp_dir: Path | None = None) -> Path:
        import yaml

        data_exp = data_exp_dir or self.exp_dir
        cfg = yaml.load((GPT_SOVITS_ROOT / S1_CONFIG).read_text(encoding="utf-8"), Loader=yaml.FullLoader)
        cfg["train"].update({
            "batch_size": 4,
            "epochs": epochs,
            "precision": "16-mixed",
            "save_every_n_epoch": 1,
            "if_save_every_weights": True,
            "if_save_latest": True,
            "if_dpo": False,
            "exp_name": self.exp_name,
            "half_weights_save_dir": str(WEIGHTS_S1),
        })
        cfg["pretrained_s1"] = self._pretrained_s1
        cfg["train_semantic_path"] = str(data_exp / "6-name2semantic.tsv")  # 数据源（语义 token）
        cfg["train_phoneme_path"] = str(data_exp / "2-name2text.txt")       # 数据源（音素）
        cfg["output_dir"] = str(self.exp_dir / "logs_s1")
        tmp_cfg = self.exp_dir / "tmp_s1.yaml"
        tmp_cfg.write_text(yaml.dump(cfg, default_flow_style=False), encoding="utf-8")
        (self.exp_dir / "logs_s1").mkdir(parents=True, exist_ok=True)
        # 备份并清理旧 checkpoint，防止残留的高 epoch checkpoint 与低 max_epochs 冲突
        ckpt_dir = self.exp_dir / "logs_s1" / "ckpt"
        if ckpt_dir.exists() and any(ckpt_dir.glob("*.ckpt")):
            backup_dir = self.exp_dir / "logs_s1" / "ckpt_backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            for f in ckpt_dir.glob("*.ckpt"):
                shutil.copy2(f, backup_dir / f.name)
                f.unlink(missing_ok=True)
            self.update(0, f"已备份旧 checkpoint 到 {backup_dir}")
        WEIGHTS_S1.mkdir(parents=True, exist_ok=True)
        # 备份 GPT_weights_v2 中同名历史权重：s1 训练每次生成固定文件名
        # {exp_name}-e{epoch}.ckpt，直接覆盖会静默冲掉上一轮/历史训练的同 epoch 模型
        #（如 8/17 的 e1/e2/e3 被 8/19 训练覆盖）。训练前先复制一份到 exp 目录留存。
        old_weights = sorted(WEIGHTS_S1.glob(f"{self.exp_name}-e*.ckpt"))
        if old_weights:
            wb = self.exp_dir / "logs_s1" / "weights_backup"
            wb.mkdir(parents=True, exist_ok=True)
            import shutil
            for f in old_weights:
                shutil.copy2(f, wb / f.name)
            self.update(0, f"已备份 {len(old_weights)} 个历史 GPT 权重到 {wb}")
        env = _env({"_CUDA_VISIBLE_DEVICES": "0", "hz": "25hz"})
        _run(
            [str(PYTHON), "-s", "GPT_SoVITS/s1_train.py", "--config_file", str(tmp_cfg)],
            env, GPT_SOVITS_ROOT, self.update, f"GPT 语义训练（{epochs} epoch）",
            timeout=36000,
        )
        WEIGHTS_S1.mkdir(parents=True, exist_ok=True)
        cands = sorted(WEIGHTS_S1.glob(f"{self.exp_name}*.ckpt"), key=lambda p: p.stat().st_mtime)
        if not cands:
            raise RuntimeError("s1 训练未产出模型文件")
        return cands[-1]

    # ---------- 6. 全流程 ----------

    def run(self, input_dir: Path, speaker: str, s2_epochs: int, s1_epochs: int,
            s1_input_dir: Path | None = None, s2_input_dir: Path | None = None) -> dict:
        """全流程训练。支持 s1/s2 使用不同数据源：
        - s1_input_dir / s2_input_dir 缺省 = input_dir（混合模式，与旧行为一致）
        - 若指定不同目录，各自独立切片+标注+预处理，产出对应 exp 特征；
          产物（SoVITS/GPT 权重）仍归属当前数字人（self.exp_name）。
        """
        s2_dir = Path(s2_input_dir) if s2_input_dir else (Path(input_dir) if input_dir else None)
        s1_dir = Path(s1_input_dir) if s1_input_dir else (Path(input_dir) if input_dir else None)
        if s1_dir is None or s2_dir is None:
            raise RuntimeError("训练缺少数据源：请为 s1/s2 配置训练数据")

        self.update(2, "检查训练环境（预加载依赖）…")
        self.preflight()
        self.update(3, "开始处理训练数据…")
        self.exp_dir.mkdir(parents=True, exist_ok=True)  # 主 exp = 产物容器（checkpoint/日志）

        # 数据源 exp 目录：主 exp 归当前数字人；独立数据源用固定后缀目录
        main_ref = Path(input_dir) if input_dir else None
        s2_exp = self.exp_dir if (main_ref and s2_dir == main_ref) else EXP_ROOT / f"{self.exp_name}__s2src"
        s1_exp = self.exp_dir if (main_ref and s1_dir == main_ref) else EXP_ROOT / f"{self.exp_name}__s1src"

        # 涉及的 exp（去重）逐一准备特征：切片 → 标注 → 预处理
        prepared: dict[Path, Path] = {}  # exp_dir -> 对应输入目录
        if s2_exp not in prepared:
            prepared[s2_exp] = s2_dir
        if s1_exp not in prepared:
            prepared[s1_exp] = s1_dir
        for exp, src in prepared.items():
            if exp == self.exp_dir:
                self.update(5, f"准备主训练数据（{src.name}）…")
            else:
                self.update(5, f"准备独立数据源（{src.name}）…")
            self._prepare_exp(exp, src, speaker)

        self.update(35, "预处理完成，开始音色模型训练…")
        s2_path = self.train_s2(s2_epochs, s2_exp)
        self.update(65, "音色模型完成，开始语义模型训练…")
        s1_path = self.train_s1(s1_epochs, s1_exp)
        self.update(98, "训练完成，整理模型…")
        # 复制到声音目录（自包含）
        return {
            "sovits_model": str(s2_path),
            "gpt_model": str(s1_path),
            "exp_dir": str(self.exp_dir),
            "s1_source": str(s1_dir),
            "s2_source": str(s2_dir),
        }

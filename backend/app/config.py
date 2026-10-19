"""全局配置：路径、存储目录、默认参数。"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

# 项目根目录（backend/ 的上一级）
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"

if load_dotenv is not None:
    load_dotenv(ROOT_DIR / "backend" / ".env")

# 存储目录结构（全部本地）
STORAGE = {
    "voices": DATA_DIR / "voices",      # 声音模型（参考音频 + 模型文件/配置）
    "uploads": DATA_DIR / "uploads",    # 用户上传的原始文件
    "drafts": DATA_DIR / "drafts",      # 初版音频（TTS 产物）
    "projects": DATA_DIR / "projects",  # 有声读物项目（分段 + 最终成品）
    "separation": DATA_DIR / "separation",  # 歌曲分离结果
    "slicing": DATA_DIR / "slicing",       # 音频分片任务（源音频 + 片段 + 标注）
    "polish": DATA_DIR / "polish",      # 音频润色（初稿 + 成品）
    "cache": DATA_DIR / "cache",        # 波形缓存等
}
for _d in STORAGE.values():
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "app.db"

# 允许上传的音频格式
ALLOWED_AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus"}
# 允许上传的文稿格式
ALLOWED_TEXT_EXT = {".txt", ".md", ".srt"}

# 默认导出/合成格式
EXPORT_FORMAT = "mp3"
EXPORT_BITRATE = "192k"
SAMPLE_RATE = 32000  # GPT-SoVITS 默认 32k，云引擎通常 32k/44.1k

# 任务队列：最多并行任务数（GPU 引擎单任务；云引擎可多并行）
MAX_CONCURRENT_LOCAL_JOBS = 1
MAX_CONCURRENT_CLOUD_JOBS = 3

# 单次 TTS 最大字符数（防止超长请求，分段处理）
MAX_TTS_CHUNK_CHARS = 300

# 数据库连接
DB_URL = f"sqlite:///{DB_PATH.as_posix()}"

# 环境变量（.env 或系统环境）
ENV = os.environ

def getenv_bool(key: str, default: bool = False) -> bool:
    v = ENV.get(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}

# GPT-SoVITS API 服务地址（用户自行启动其 api_v2 服务后在此填写）
GPT_SOVITS_URL = ENV.get("GPT_SOVITS_URL", "http://127.0.0.1:9880")
GPT_SOVITS_REF_DIR = ENV.get("GPT_SOVITS_REF_DIR", "")

# 合成后用 ASR 回读校对字数，缺字则换种子重试并自动回退官方语义模型。
# full=每句都校验（最准最慢）| smart=只校验短句与含引号的对话句（默认）| off=退回旧的时长判据。
# 设成 off 就再也发现不了「丢句首」——那种情况下总时长看着完全正常。
ASR_VERIFY_MODE = (ENV.get("ASR_VERIFY") or "smart").strip().lower()
if ASR_VERIFY_MODE in ("0", "false", "no"):
    ASR_VERIFY_MODE = "off"
elif ASR_VERIFY_MODE in ("1", "true", "yes", "on"):
    ASR_VERIFY_MODE = "full"
if ASR_VERIFY_MODE not in ("full", "smart", "off"):
    ASR_VERIFY_MODE = "smart"
GPT_SOVITS_ROOT = ENV.get("GPT_SOVITS_ROOT", "F:/GPT-SoVITS-main")

# 零样本克隆必须显式绑定官方底模：不绑定时 api_v2 会沿用上一次的权重，
# 而默认权重通常是某个微调模型，会让所有零样本音色都掺入那个人的音色。
_BASE_DIR = f"{GPT_SOVITS_ROOT}/GPT_SoVITS/pretrained_models/gsv-v2final-pretrained"
GPT_SOVITS_BASE_GPT = ENV.get("GPT_SOVITS_BASE_GPT", f"{_BASE_DIR}/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt")
GPT_SOVITS_BASE_SOVITS = ENV.get("GPT_SOVITS_BASE_SOVITS", f"{_BASE_DIR}/s2G2333k.pth")

# 云 API 密钥（也可在「设置」页填写，优先级：数据库设置 > .env）
MINIMAX_API_KEY = ENV.get("MINIMAX_API_KEY", "")
MINIMAX_GROUP_ID = ENV.get("MINIMAX_GROUP_ID", "")
FISH_API_KEY = ENV.get("FISH_API_KEY", "")
LLM_API_KEY = ENV.get("LLM_API_KEY", "")
LLM_BASE_URL = ENV.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = ENV.get("LLM_MODEL", "deepseek-chat")

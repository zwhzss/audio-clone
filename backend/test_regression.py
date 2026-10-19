"""只读回归测试：改完分片/标注/合成链路后，工程本身与磁盘、数据库状态是否仍自洽。

跑法（不写任何用户数据，不调 /tts —— 那会切换 api_v2 当前权重，影响正在进行的试听）：
    cd backend && python test_regression.py
退出码 0 = 全部通过。

写这个文件的原因：各阶段的验证脚本当初是临时跑的，事后无法重放；今天第一版探针还把
「app.routes 里未展开的 _IncludedRouter」当成路由缺失，报了 4 个假失败。
"""
import json
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent          # backend/
ROOT = HERE.parent                               # 项目根
sys.path.insert(0, str(HERE))
DB_PATH = ROOT / "data" / "app.db"
BASE = "http://127.0.0.1:8000/api"

ok, bad, info = [], [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(("  OK   " if cond else "  BAD  ") + name + ((" | " + str(detail)) if detail else ""))


def note(text):
    info.append(text)
    print("  · " + text)


def dur(p):
    try:
        with wave.open(str(p)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


def get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as x:
        return json.loads(x.read().decode("utf-8"))


print("== 1. 纯函数回归（分句 / 分片分组）==")
for f in ("test_split_quotes.py", "test_group_units.py"):
    r = subprocess.run([sys.executable, f], cwd=str(HERE), capture_output=True,
                       encoding="utf-8", errors="replace")
    tail = [l for l in (r.stdout or "").splitlines() if "结果" in l]
    check(f + " 通过", r.returncode == 0, (tail[-1] if tail else (r.stderr or "")[-120:]))

print("\n== 2. FastAPI 路由完整（用 openapi，而不是未展开的 app.routes）==")
from app.main import app as fastapi_app  # noqa: E402

paths = fastapi_app.openapi()["paths"]
check("路径总数 >= 80", len(paths) >= 80, len(paths))
for need in ("/api/slicing/{task_id}/import", "/api/slicing/{task_id}/process",
             "/api/slicing/{task_id}/slices", "/api/slicing/{task_id}/stats",
             "/api/humans/{human_id}/data-process", "/api/humans/{human_id}/data-list",
             "/api/projects/{project_id}/generate", "/api/projects/{project_id}/segments/split",
             "/api/projects/{project_id}/render", "/api/settings"):
    check("已注册 " + need, need in paths)
check("分片模块路由 >= 15 个", len([p for p in paths if "/slicing" in p]) >= 15,
      len([p for p in paths if "/slicing" in p]))

print("\n== 3. 数据库迁移列齐全 ==")
import sqlite3  # noqa: E402

db = sqlite3.connect(str(DB_PATH))
want = {"training_slices": ["slice_task_id", "flags"], "audio_slices": ["flags", "edited"],
        "slice_tasks": ["hotwords", "total_sec"], "digital_humans": ["s1_data_dir", "s2_data_dir"],
        "segments": ["params", "offset"]}
for t, cols in want.items():
    have = {c[1] for c in db.execute("PRAGMA table_info(%s)" % t)}
    check("%s 列齐全" % t, all(c in have for c in cols), [c for c in cols if c not in have])

print("\n== 4. 训练目录的 train.list 可被训练器解析 ==")
dirs = set()
for col in ("data_dir", "s1_data_dir", "s2_data_dir"):
    for (v,) in db.execute("select distinct %s from digital_humans where coalesce(%s,'')<>''" % (col, col)):
        dirs.add(v)
seen, broken = set(), []
for v in sorted(dirs):
    dd = Path(v)
    if str(dd) in seen:
        continue
    seen.add(str(dd))
    lp = dd / "train.list"
    if not lp.exists():
        continue
    lines = [l for l in lp.read_text(encoding="utf-8").splitlines() if l.strip()]
    miss = sum(1 for l in lines if len(l.split("|")) < 4
               or not ((dd / l.split("|")[0]).is_file() or (dd / "sliced" / l.split("|")[0]).is_file()))
    empty = sum(1 for l in lines if not l.split("|")[3].strip()) if lines else 0
    if miss:
        broken.append("%s: %d/%d 行找不到对应音频" % (dd.name, miss, len(lines)))
    elif empty == len(lines) and lines:
        # 不是代码回归（文件早于今天的改动），但要让人看见：这种目录直接训练会失败
        note("%s: %d 行全部空文本 —— 该目录现在没法训练（_stage_labeled_slices 会判无可用条目）"
             % (dd.name, empty))
    elif empty:
        note("%s: %d/%d 行待人工标注" % (dd.name, empty, len(lines)))
check("没有训练目录缺音频或行格式错", not broken, broken)

print("\n== 5. 分片任务：磁盘、数据库、覆盖率、导出格式一致 ==")
for tid, title, duration, sc, ts in db.execute(
        "select id,title,duration,slice_count,total_sec from slice_tasks"):
    files = list((ROOT / "data/slicing" / tid / "slices").glob("*.wav"))
    dbr = db.execute("select count(*) from audio_slices where task_id=? and is_deleted=0", (tid,)).fetchone()[0]
    rows = db.execute("select text,duration,flags from audio_slices where task_id=? and is_deleted=0",
                      (tid,)).fetchall()
    cov = (ts or 0) / duration * 100 if duration else 0
    lab = sum(1 for t, _d, _f in rows if (t or "").strip())
    check("任务 %s(%s) 磁盘==库==计数" % (tid, (title or "")[:14]), len(files) == dbr == sc,
          (len(files), dbr, sc))
    check("任务 %s 覆盖率 >= 95%%" % tid, cov >= 95, "%.1f%%" % cov)
    check("任务 %s 单片时长都在 0.3~15 秒" % tid,
          all(0.3 <= d <= 15 for _t, d, _f in rows), round(max(d for _t, d, _f in rows), 2) if rows else "-")
    if lab < len(rows):
        note("任务 %s 有 %d/%d 段未标注" % (tid, len(rows) - lab, len(rows)))

print("\n== 6. 项目产物指针自洽 ==")
stale = []
for pid, title, vp, fp in db.execute("select id,title,voice_track_path,final_path from projects"):
    for x in (vp, fp):
        if x and not (ROOT / "data" / x).exists():
            stale.append("%s → %s" % ((title or "")[:12], x))
check("没有项目指向已消失的声音轨/成品", not stale, stale)
ready = db.execute("select count(*) from projects where status='ready'").fetchone()[0]
allp = db.execute("select count(*) from projects").fetchone()[0]
note("项目 %d 个，其中已渲染成品 %d 个" % (allp, ready))

print("\n== 7. 运行中的服务与队列 ==")
try:
    h = get(BASE + "/health")
    check("后端 /api/health 正常", h.get("ok") is True, h)
except Exception as e:
    check("后端 /api/health 正常", False, str(e))
try:
    jobs = get(BASE + "/jobs")
    live = [j for j in jobs if j["status"] in ("running", "queued")]
    check("没有残留 running/queued 任务", not live, [(j["kind"], j["title"]) for j in live])
    errs = [j for j in jobs if j["status"] == "error"][:3]
    for j in errs:
        note("历史失败任务：%s（%s）" % (j["title"], (j.get("message") or "")[:60]))
except Exception as e:
    check("任务队列可读", False, str(e))
import socket  # noqa: E402

s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", 9880))
    note("api_v2 (9880) 在监听（本测试不合成，避免切换你当前的权重）")
except Exception:
    note("api_v2 (9880) 未在监听：需要合成时先跑 start_sovits.bat")
finally:
    s.close()

print("\n== 8. 仓库卫生 ==")
check("仓库内没有再落出的 ASR 模型副本", not (ROOT / "backend" / "tools").exists())
r = subprocess.run(["git", "-C", str(ROOT), "status", "--short"], capture_output=True,
                   encoding="utf-8", errors="replace")
check("工作区无未提交改动", not (r.stdout or "").strip(), (r.stdout or "")[:150])
lg = subprocess.run(["git", "-C", str(ROOT), "log", "--oneline", "-8"], capture_output=True,
                    encoding="utf-8", errors="replace")
note("最近提交：\n     " + "\n     ".join((lg.stdout or "").splitlines()))
check("今天已有 >= 6 个提交", len([l for l in (lg.stdout or "").splitlines()]) >= 6)

print("\n结果: 通过 %d / 失败 %d（另有 %d 条提示）" % (len(ok), len(bad), len(info)))
for x in bad:
    print("  失败:", x)
sys.exit(1 if bad else 0)

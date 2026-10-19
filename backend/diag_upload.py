"""诊断：数字人上传长音频流程。"""
import json
import pathlib
import urllib.request

BASE = "http://127.0.0.1:8000"

r = urllib.request.urlopen(BASE + "/api/humans")
humans = json.loads(r.read().decode())
print("数字人数:", len(humans))
for h in humans:
    print(f"  {h['id']} | {h['name']} | data_dir={h.get('data_dir')!r}")

if not humans:
    print("没有数字人")
    raise SystemExit

h = humans[0]
hid = h["id"]
dd = h.get("data_dir") or ""
print("data_dir:", repr(dd))
d = pathlib.Path(dd) if dd else None
if d:
    print("目录存在:", d.exists())
    if d.exists():
        print("目录内容:", [p.name for p in d.iterdir()])

# 测试上传一个模拟的 m4a 文件
print("\n=== 测试上传 ===")
fake_data = b"\x00" * 1000  # 假音频数据
boundary = "----diag"
parts = [
    ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="20260815_184426.m4a"\r\nContent-Type: audio/mp4\r\n\r\n').encode(),
    fake_data,
    ('\r\n--' + boundary + '--\r\n').encode(),
]
m = urllib.request.Request(BASE + f"/api/humans/{hid}/data-upload", data=b"".join(parts), method="POST")
m.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
try:
    with urllib.request.urlopen(m, timeout=120) as resp:
        result = json.loads(resp.read().decode())
    print("上传返回:", result)
except urllib.error.HTTPError as e:
    print("HTTP错误:", e.code, e.read().decode()[:500])
except Exception as e:
    print("异常:", type(e).__name__, str(e)[:500])

# 检查目录内容
if d and d.exists():
    print("\n上传后目录内容:", [p.name for p in d.iterdir()])

# 测试处理
print("\n=== 测试处理 ===")
try:
    r2 = urllib.request.Request(BASE + f"/api/humans/{hid}/data-process", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r2, timeout=300) as resp:
        result2 = json.loads(resp.read().decode())
    print("处理返回:", result2)
except urllib.error.HTTPError as e:
    print("HTTP错误:", e.code, e.read().decode()[:500])
except Exception as e:
    print("异常:", type(e).__name__, str(e)[:500])

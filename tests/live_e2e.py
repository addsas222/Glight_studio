# -*- coding: utf-8 -*-
"""对着**正在运行**的本地服务跑一遍全链路自检（图片 → 分析 → 渲染 → 导出 → 视频）。

与 tests/test_*.py 的分工：
  - test_*.py 直接调函数或走路由内省，快、可断点，但绕过了 HTTP 层；
  - 本脚本走真实 HTTP + 真实 ONNX 模型 + 真实 ffmpeg，用于交付/换机后的环境自检
    （**不是** pytest 用例，不会被 `pytest tests/` 收集）。

用法：
    python -m webui.desktop              # 或 uvicorn --port 8756 "webui.api:app"
    python tests/live_e2e.py [--port 8756]

退出码 0 = 全绿；非 0 = 有环节失败（失败点会打印原因）。
"""
import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def step(name: str):
    print(f"\n=== {name} ===")


def ok(msg: str):
    print(f"  OK  {msg}")


def bad(msg: str):
    FAILED.append(msg)
    print(f"  FAIL {msg}")


def check(cond: bool, msg: str):
    (ok if cond else bad)(msg)
    return cond


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, path: str, data=None, ctype: str | None = None,
             method: str = "GET", headers: dict | None = None):
        h = dict(headers or {})
        if ctype:
            h["Content-Type"] = ctype
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                body = r.read()
                return r.status, r.headers.get("content-type", ""), body
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("content-type", ""), e.read()

    def json(self, path: str, obj=None, method: str = "GET"):
        data = json.dumps(obj).encode() if obj is not None else None
        st, ct, body = self._req(path, data,
                                 "application/json" if obj is not None else None,
                                 method=("POST" if obj is not None else method))
        try:
            j = json.loads(body.decode("utf-8"))
        except Exception:
            j = {"_raw": body[:200].decode("utf-8", "replace")}
        return st, j

    def raw_post(self, path: str, payload: bytes, filename: str):
        st, ct, body = self._req(path, payload, "application/octet-stream",
                                 "POST", {"X-Filename": filename})
        try:
            j = json.loads(body.decode("utf-8"))
        except Exception:
            j = {"_raw": body[:200].decode("utf-8", "replace")}
        return st, j

    def blob(self, path: str):
        return self._req(path)


def b64_png_to_arr(b64: str) -> np.ndarray:
    im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    return np.asarray(im)


def normals_from_png(arr: np.ndarray) -> np.ndarray:
    """PNG 法线编码：0.5 = 零向量，线性映射到 -1~1。"""
    return arr.astype(np.float32) / 255.0 * 2.0 - 1.0


def brief(obj, limit: int = 220) -> str:
    """摘要化响应体：base64 字段只留长度，否则失败信息会被几十 KB 淹没。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and len(v) > 60:
                out[k] = f"<{len(v)} chars>"
            elif isinstance(v, (dict, list)):
                out[k] = brief(v, limit)
            else:
                out[k] = v
        s = json.dumps(out, ensure_ascii=False, default=str)
    else:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------- 图片链路

def check_image(c: Client, preset: dict, workdir: str):
    step("图片链路：上传 → 分析 → 拾取 → 渲染 → 导出")
    src = os.path.join(ROOT, "examples", "sample_input.png")
    with open(src, "rb") as f:
        payload = f.read()

    st, media = c.raw_post("/api/media", payload, "sample_input.png")
    if not check(st == 200 and "image_id" in media,
                 f"/api/media → {st} {brief(media)}"):
        return None
    iid = media["image_id"]
    ok(f"上传：{media['width']}x{media['height']} id={iid[:8]}…")

    st, an = c.json("/api/analyze", {"image_id": iid})
    if not check(st == 200 and an.get("depth_png_b64"),
                 f"/api/analyze → {st} {brief(an)}"):
        return iid
    ok(f"分析：后端 {an['backend']}，缓存命中 {an['from_cache']}")
    check(an["backend"] == "builtin",
          f"深度由内置 ONNX 模型产出（不是 simulate 降级）：{an['backend']}")
    check(bool(an.get("normal_png_b64")),
          "法线图非空（normal 模型或几何派生至少有一个可用）")

    d = b64_png_to_arr(an["depth_png_b64"])
    check(d.shape[:2] == (media["height"], media["width"]),
          f"深度图尺寸与源图一致：{d.shape[:2]} vs {media['height']}x{media['width']}")
    dv = d.astype(np.float32).mean(axis=2) / 255.0
    check(float(dv.std()) > 0.02,
          f"深度**有结构**（std={dv.std():.3f}，全平图 = 模型没跑）")
    # 方向：主体（中下）比四角近；取反会让这条失败
    h, w = dv.shape
    center = float(dv[h // 3: h * 2 // 3, w // 3: w * 2 // 3].mean())
    corners = float(np.mean([dv[:h // 6, :w // 6].mean(), dv[:h // 6, -w // 6:].mean(),
                             dv[-h // 6:, :w // 6].mean(), dv[-h // 6:, -w // 6:].mean()]))
    check(center > corners,
          f"深度方向（白=近）：中心 {center:.3f} > 四角 {corners:.3f}")

    if an.get("normal_png_b64"):
        n = normals_from_png(b64_png_to_arr(an["normal_png_b64"]))
        check(float(n[..., 2].mean()) > 0,
              f"法线 +z 朝观察者：z 均值 {n[..., 2].mean():.3f}")
        ln = np.linalg.norm(n, axis=-1)
        err = float(np.abs(ln - 1.0).max())
        # 阈值 0.02 而不是 1e-3：这是**解码后的 PNG**，8bit 每分量步长 1/255，
        # 单位向量的长度误差上限 ≈ 2√3/255 = 1.36e-2（实测 p99 8.6e-3 / max 1.07e-2）。
        # 定成 1e-3 会把量化误差误判成模型缺陷；定成 0.02 仍能抓住「压根没归一化」
        # （那种情况误差是 0.1 量级）。
        check(err < 0.02,
              f"法线单位长度在 8bit 量化容差内（最大偏差 {err:.2e} < 1.36e-2 上限）")

    st, prev = c.json("/api/depth-preview", {"image_id": iid})
    check(st == 200 and bool(prev.get("depth_png_b64")), f"/api/depth-preview → {st}")

    px, py = media["width"] // 2, int(media["height"] * 0.62)
    st, pk = c.json("/api/pick", {"image_id": iid, "x": px, "y": py})
    if check(st == 200, f"/api/pick → {st} {brief(pk)}"):
        vals = [pk.get(k) for k in ("dx", "dy", "dz", "nx", "ny", "nz")]
        check(all(isinstance(v, (int, float)) and abs(v) < 1e3 for v in vals),
              f"拾取返回有限向量：d=({pk['dx']:.2f},{pk['dy']:.2f},{pk['dz']:.2f})")

    st, rr = c.json("/api/render", {"image_id": iid, "params": preset})
    if not check(st == 200 and rr.get("png_b64"),
                 f"/api/render → {st} {brief(rr)}"):
        return iid
    out = b64_png_to_arr(rr["png_b64"])
    check((rr["width"], rr["height"]) == (media["width"], media["height"]),
          f"渲染尺寸：{rr['width']}x{rr['height']}，耗时 {rr['elapsed']:.2f}s，后端 {rr['backend']}")
    check(rr["backend"] == "builtin", f"渲染用的深度来自真实模型：{rr['backend']}")
    srcarr = np.asarray(Image.open(src).convert("RGB"))
    if out.shape == srcarr.shape:
        diff = float(np.abs(out.astype(np.int16) - srcarr.astype(np.int16)).mean())
        check(diff > 1.0, f"渲染结果**确实变了**（平均像素差 {diff:.2f}）")
    b64 = rr["png_b64"]
    with open(os.path.join(workdir, "render.png"), "wb") as f:
        f.write(base64.b64decode(b64))
    ok(f"渲染结果已存盘：{os.path.join(workdir, 'render.png')}")

    for fmt, magic in (("png", b"\x89PNG"), ("jpeg", b"\xff\xd8\xff")):
        st, ex = c.json("/api/export", {"image_id": iid, "params": preset,
                                        "format": fmt})
        if not check(st == 200 and ex.get("png_b64"),
                     f"/api/export {fmt} → {st} {brief(ex)}"):
            continue
        raw = base64.b64decode(ex["png_b64"])
        check(raw.startswith(magic),
              f"导出 {fmt} 魔数正确，文件名 {ex['filename']}，{len(raw) // 1024}KB")
    return iid


# ---------------------------------------------------------------- 视频链路

def make_test_clip(workdir: str) -> str:
    """用 ffmpeg 从示例图生成一段带运动的小视频（zoompan 缓慢推近）。"""
    out = os.path.join(workdir, "clip.mp4")
    src = os.path.join(ROOT, "examples", "sample_input.png")
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", src,
        "-vf", ("zoompan=z='1+0.03*on':d=1:"
                "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=256x256,fps=12"),
        "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return out


def probe(path: str) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])
    return json.loads(r.stdout)


def extract_frame(video: str, index: int) -> np.ndarray:
    """用 ffmpeg 抽一帧成 numpy（用于「成品≠源片」这类内容级比对）。"""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", video, "-vf", f"select=eq(n\\,{index})",
         "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(r.stderr[-400:].decode("utf-8", "replace"))
    return np.asarray(Image.open(io.BytesIO(r.stdout)).convert("RGB"))


def poll_job(c: Client, job_id: str, timeout: float = 1800.0) -> dict:
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        st, j = c.json(f"/api/video/job/{job_id}")
        if st != 200:
            raise RuntimeError(f"job 轮询失败 {st} {j}")
        line = f"{j['state']} {j['progress'] * 100:5.1f}% {j['message']}"
        if line != last:
            print(f"    {line}")
            last = line
        if j["state"] != "running":
            return j
        time.sleep(0.5)
    raise TimeoutError("任务超时")


def check_video(c: Client, preset: dict, workdir: str, mode: str):
    step(f"视频链路（{mode}）：上传 → 抽帧 → 关键帧 → 缩略图 → 处理 → 下载")
    clip = make_test_clip(workdir)
    p = probe(clip)
    vs = next(s for s in p["streams"] if s["codec_type"] == "video")
    src_frames = int(vs.get("nb_frames") or 0)
    ok(f"测试片源：{vs['width']}x{vs['height']} {vs.get('r_frame_rate')} "
       f"{float(p['format']['duration']):.2f}s {src_frames} 帧")

    with open(clip, "rb") as f:
        payload = f.read()
    st, up = c.raw_post("/api/video", payload, "clip.mp4")
    if not check(st == 200 and up.get("video_id"), f"/api/video → {st} {brief(up)}"):
        return
    vid = up["video_id"]
    info = up["info"]
    check(info["frame_count"] >= 20,
          f"探测：{info['width']}x{info['height']} {info['fps']} "
          f"{info['frame_count']} 帧 {info['duration']:.2f}s 编码 {info['codec']}")
    check(up["hints"]["over_recommended_size"] is False,
          f"尺寸未超建议值：{up['hints']}")

    st, fr = c.json("/api/video/frame", {"video_id": vid, "index": 5})
    check(st == 200 and fr.get("width") == info["width"],
          f"/api/video/frame → {st} {fr.get('width')}x{fr.get('height')}")

    st, kf = c.json("/api/video/keyframes", {"video_id": vid, "count": 4})
    check(st == 200 and kf["count"] >= 2,
          f"/api/video/keyframes → {kf.get('keyframes')}")
    keys = kf["keyframes"]

    st, _ct, th = c.blob(f"/api/video/thumbnails/{vid}?count=6&width=64")
    thj = json.loads(th.decode())
    check(st == 200 and thj["count"] >= 2,
          f"/api/video/thumbnails → {thj['count']} 张 {thj['width']}x{thj['height']}")

    st, pk = c.json("/api/video/pick",
                    {"video_id": vid, "index": keys[len(keys) // 2],
                     "x": info["width"] // 2, "y": int(info["height"] * 0.6)})
    check(st == 200, f"/api/video/pick → {st} {brief(pk)}")

    body = {"video_id": vid, "params": preset, "mode": mode,
            "keyframe_count": 4 if mode == "keyframe" else 8,
            "smooth": 0.6}
    st, pr = c.json("/api/video/process", body)
    if not check(st == 202 and pr.get("job_id"),
                 f"/api/video/process → {st} {brief(pr)}"):
        return
    t0 = time.time()
    job = poll_job(c, pr["job_id"])
    dt = time.time() - t0
    if not check(job["state"] == "done",
                 f"任务结束：{job['state']} {job.get('error')}"):
        return
    res = job["result"] or {}
    ok(f"处理完成 {dt:.1f}s：{brief(res)}")

    st, ct, out = c.blob(f"/api/video/result/{vid}")
    if not check(st == 200 and out[:4] not in (b"", b"\x00\x00\x00\x00"),
                 f"/api/video/result → {st} {len(out)} 字节"):
        return
    dst = os.path.join(workdir, f"video_{mode}.mp4")
    with open(dst, "wb") as f:
        f.write(out)
    po = probe(dst)
    vo = next(s for s in po["streams"] if s["codec_type"] == "video")
    ovf = int(vo.get("nb_frames") or 0)
    odur = float(po["format"]["duration"])
    check(vo["width"] == info["width"] and vo["height"] == info["height"],
          f"成品分辨率 {vo['width']}x{vo['height']}，{ovf} 帧 / {odur:.2f}s "
          f"（源 {src_frames} 帧 / {info['duration']:.2f}s）")
    check(ovf >= src_frames - 2, f"成品帧数不缺：{ovf} >= {src_frames}")
    check(os.path.getsize(dst) > 2000, f"成品 {os.path.getsize(dst) // 1024}KB → {dst}")
    # 内容级比对：成品必须**真的打光**了，不能只是把源片重新封了一遍
    k = min(12, max(ovf - 1, 0))
    try:
        a, b = extract_frame(clip, k), extract_frame(dst, k)
        if a.shape == b.shape:
            diff = float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())
            check(diff > 1.0, f"第 {k} 帧确实被重打光（平均像素差 {diff:.2f}）")
    except Exception as e:
        bad(f"抽帧比对失败：{e}")
    return vid


def check_strokes(c: Client, preset: dict, workdir: str, vid: str):
    step("逐帧光绘：预览 → 导出")
    strokes = [{"type": "glow", "x": 0.5, "y": 0.4, "radius": 0.25,
                "intensity": 0.9, "kelvin": 4200, "softness": 0.6,
                "blend": "screen"}]
    st, pv = c.json("/api/strokes/preview",
                    {"video_id": vid, "index": 3, "strokes": strokes})
    if not check(st == 200 and pv.get("png_b64"),
                 f"/api/strokes/preview → {st} {brief(pv)}"):
        return
    arr = b64_png_to_arr(pv["png_b64"])
    base = None
    st2, fr = c.json("/api/video/frame", {"video_id": vid, "index": 3})
    if st2 == 200:
        base = b64_png_to_arr(fr["png_b64"]).astype(np.int16)
    if base is not None and base.shape == arr.shape:
        diff = float(np.abs(arr.astype(np.int16) - base).mean())
        check(diff > 0.5, f"光绘**真的画上去了**（平均像素差 {diff:.2f}）")

    st, ex = c.json("/api/strokes/export",
                    {"video_id": vid, "params": preset, "track": {"3": strokes}})
    if not check(st == 202 and ex.get("job_id"),
                 f"/api/strokes/export → {st} {brief(ex)}"):
        return
    job = poll_job(c, ex["job_id"])
    if not check(job["state"] == "done", f"光绘导出：{job['state']} {job.get('error')}"):
        return
    st, ct, out = c.blob(f"/api/strokes/result/{ex['job_id']}")
    check(st == 200 and len(out) > 2000, f"光绘成品 {len(out) // 1024}KB")
    if st == 200 and len(out) > 2000:
        dst = os.path.join(workdir, "strokes.mp4")
        with open(dst, "wb") as f:
            f.write(out)
        po = probe(dst)
        vo = next(s for s in po["streams"] if s["codec_type"] == "video")
        ok(f"光绘视频 {vo['width']}x{vo['height']} "
           f"{int(vo.get('nb_frames') or 0)} 帧 → {dst}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8756)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--skip-video", action="store_true")
    ap.add_argument("--mode", default="keyframe",
                    choices=["keyframe", "perframe", "both"])
    args = ap.parse_args()

    c = Client(f"http://{args.host}:{args.port}")
    workdir = os.path.join(ROOT, "_e2e_out")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)

    step("服务与模型")
    try:
        st, state = c.json("/api/state")
    except Exception as e:
        print(f"  无法连接 {c.base}：{e}\n  请先启动服务：python -m webui.desktop")
        return 2
    check(st == 200, f"/api/state → {st}")
    check(bool(state.get("model_ready")), "深度模型已就绪 model_ready=True")
    check(bool(state.get("normal_model_ready")),
          "法线模型已就绪 normal_model_ready=True（未装则由深度几何派生）")
    check(bool(state.get("ffmpeg")), "ffmpeg 可用（视频链路前提）")
    ok(f"版本 {state.get('version')}，预设 {state.get('preset_names')}")
    preset = state["presets"][0]["params"]
    print(f"  用预设「{state['presets'][0]['name']}」渲染，"
          f"{len(preset['lights'])} 盏灯，环境 {preset['ambient_intensity']}")

    check_image(c, preset, workdir)

    if not args.skip_video:
        modes = ["keyframe", "perframe"] if args.mode == "both" else [args.mode]
        vid = None
        for m in modes:
            vid = check_video(c, preset, workdir, m) or vid
        if vid:
            check_strokes(c, preset, workdir, vid)

    step("结果")
    if FAILED:
        print(f"  {len(FAILED)} 项失败：")
        for f in FAILED:
            print(f"    - {f}")
        print(f"  产物目录：{workdir}")
        return 1
    print(f"  全部通过，产物目录：{workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

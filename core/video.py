# -*- coding: utf-8 -*-
"""视频读写与光照处理（本地离线）。

视频 I/O 通过**随包分发的 ffmpeg 二进制**（依赖 imageio-ffmpeg 提供的静态
构建）完成，既不依赖系统安装的 ffmpeg，也不依赖 OpenCV：
  - 读帧：`ffmpeg -i in -f rawvideo -pix_fmt rgb24 -` 流式管道，逐帧 numpy
  - 写帧：`ffmpeg -f rawvideo ... -i - -i 原片 -map 0:v -map 1:a? -c:a copy`
          直接把原音轨复制过去，只重编码视频
  - 元数据：解析 `ffmpeg -i` 的 stderr（该发行版不含 ffprobe）

两种视频处理模式（对应规格）：
  - keyframe（默认）：仅对**关键帧**做 AI 分析，得到关键帧深度；
    其余帧的深度由相邻关键帧线性插值并在时间轴上做 EMA 平滑，避免闪烁。
    光照参数全片统一，因此画面风格稳定、速度快。适合加查动画。
  - perframe（高质量）：**每一帧**单独做 AI 分析，深度随画面变化，
    光照因此随画面动态变化。速度与帧数成正比。

所有 AI 结果仍走 core.cache 的内容哈希缓存，关键帧重复分析不会重算。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from .ai_backend import AIBackendConfig, AIPreprocessor, encode_png
from .cache import AICache
from .render import render
from .types import RenderParams

# 建议范围（超出仅提示，不阻断；硬上限用于防止内存/时间失控）
REC_MAX_SIDE = 1920            # 建议 ≤1080p（长边 1920）
REC_MAX_DURATION = 300.0       # 建议 ≤5 分钟
HARD_MAX_SIDE = 4096
HARD_MAX_DURATION = 1800.0     # 30 分钟

VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm")

_FFMPEG_CACHE: Optional[str] = None


class VideoToolingError(RuntimeError):
    """视频工具链不可用（缺少 ffmpeg 二进制）。"""


# ---------------------------------------------------------------- ffmpeg 定位


def _frozen_ffmpeg() -> str:
    """在打包产物中定位随包分发的 ffmpeg。

    PyInstaller 把 spec 里 `binaries=[(src, ".")]` 的条目放到：
      - onefile：解包目录 sys._MEIPASS
      - onedir ：可执行文件同级的 _internal 目录
    imageio-ffmpeg 的查找逻辑并未覆盖这两种情形，因此这里显式兜底，
    否则打包后视频功能会在用户机上整块失效。
    """
    if not getattr(sys, "frozen", False):
        return ""
    cands: List[str] = []
    mei = getattr(sys, "_MEIPASS", "")
    if mei:
        cands.append(mei)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    cands += [exe_dir, os.path.join(exe_dir, "_internal")]

    for d in cands:
        if not os.path.isdir(d):
            continue
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for name in entries:
            low = name.lower()
            if low.startswith("ffmpeg") and not low.endswith((".txt", ".json")):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    return p
    return ""


def ffmpeg_exe() -> str:
    """返回可用的 ffmpeg 可执行文件路径。

    顺序：环境变量 HLS_FFMPEG → 打包内置（frozen）→ imageio-ffmpeg 随包
    二进制。找到后同步设置 IMAGEIO_FFMPEG_EXE，便于第三方库复用同一份。
    """
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE

    env = os.environ.get("HLS_FFMPEG", "").strip()
    if env and os.path.isfile(env):
        _FFMPEG_CACHE = env
        os.environ.setdefault("IMAGEIO_FFMPEG_EXE", env)
        return _FFMPEG_CACHE

    frozen = _frozen_ffmpeg()
    if frozen:
        _FFMPEG_CACHE = frozen
        os.environ.setdefault("IMAGEIO_FFMPEG_EXE", frozen)
        return _FFMPEG_CACHE

    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            _FFMPEG_CACHE = exe
            return _FFMPEG_CACHE
    except Exception:
        pass

    raise VideoToolingError(
        "未找到可用的 ffmpeg。请安装随包依赖：pip install imageio-ffmpeg"
        "（约 80MB，含静态 ffmpeg，无需系统安装），"
        "或用环境变量 HLS_FFMPEG 指定 ffmpeg 可执行文件路径。")


def has_ffmpeg() -> bool:
    try:
        ffmpeg_exe()
        return True
    except VideoToolingError:
        return False


def _run(args: Sequence[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg_exe(), *args],
                          stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,
                          **kw)


# ---------------------------------------------------------------- 元数据


@dataclass
class VideoInfo:
    """视频元数据。"""
    path: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    frame_count: int = 0
    codec: str = ""
    has_audio: bool = False
    container: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def max_side(self) -> int:
        return max(self.width, self.height)

    def hints(self) -> dict:
        """规格提示：高分辨率/长视频建议启用云端 AI。"""
        return {
            "over_recommended_size": self.max_side > REC_MAX_SIDE,
            "over_recommended_duration": self.duration > REC_MAX_DURATION,
            "cloud_suggested": (self.max_side > 1600
                                or self.duration > REC_MAX_DURATION),
        }


_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_RE_VIDEO = re.compile(
    r"Stream #0:(\d+).*?Video:\s*([A-Za-z0-9_\-]+).*?(\d{2,5})x(\d{2,5})")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:fps|tbr)")
_RE_AUDIO = re.compile(r"Stream #0:\d+.*?Audio:")


def probe(path: str) -> VideoInfo:
    """读取视频元数据。缺 ffprobe，故解析 `ffmpeg -i` 的 stderr。

    只读容器头：**不指定输出文件**，ffmpeg 打印完 Duration/Stream 后以退出码 1
    报 "At least one output file must be specified"。退出码 1 属正常。
    若带上 `-f null -`（常见的写法）ffmpeg 会把整段视频解码一遍，
    5 分钟 1080p 就要多花几十秒，纯属浪费——本函数因此不那样调用。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"视频文件不存在：{path}")

    r = _run(["-hide_banner", "-i", path])
    err = r.stderr.decode("utf-8", "ignore")

    info = VideoInfo(path=path, container=os.path.splitext(path)[1].lower().lstrip("."))

    m = _RE_DURATION.search(err)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        info.duration = h * 3600 + mi * 60 + s

    for line in err.splitlines():
        if "Stream mapping:" in line or line.startswith("Output #0"):
            break
        if "Stream #0:" in line and "Video:" in line:
            mv = _RE_VIDEO.search(line)
            if mv:
                info.codec = mv.group(2)
                info.width, info.height = int(mv.group(3)), int(mv.group(4))
            mf = _RE_FPS.search(line)
            if mf:
                info.fps = float(mf.group(1))
        elif _RE_AUDIO.search(line):
            info.has_audio = True

    if info.fps <= 0:
        info.fps = 30.0
    if info.duration <= 0:
        info.duration = 0.0
    info.frame_count = int(round(info.duration * info.fps)) if info.duration else 0

    if info.width == 0 or info.height == 0:
        raise VideoToolingError(
            f"无法解析视频信息（可能是不支持的格式或文件损坏）：{path}\n"
            + err.strip().splitlines()[-1][:200] if err else path)
    return info


# ---------------------------------------------------------------- 读帧


def frame_size(info: VideoInfo) -> int:
    return info.width * info.height * 3


def iter_frames(path: str, info: Optional[VideoInfo] = None,
                limit: Optional[int] = None,
                scale_to: Optional[Tuple[int, int]] = None,
                stride: int = 1,
                on_frame: Optional[Callable[[int], None]] = None
                ) -> Iterator[np.ndarray]:
    """流式逐帧读取为 RGB uint8（HxWx3）。

    scale_to: (w, h) 可选的缩放，用于低分辨率扫描。
    stride>1: 只解码每第 stride 帧（由 ffmpeg select 过滤器完成），
              用于长视频的快速扫描——避免为抽样而解码全部帧。
    """
    if info is None:
        info = probe(path)
    w, h = (scale_to if scale_to else (info.width, info.height))
    vf: List[str] = []
    if stride and stride > 1:
        # 逗号在滤镜参数内需转义，否则会被当成滤镜链分隔符
        vf.append("select=not(mod(n\\,%d))" % int(stride))
    if scale_to:
        vf.append(f"scale={w}:{h}")
    args = ["-v", "error", "-i", path]
    if vf:
        args += ["-vf", ",".join(vf), "-vsync", "0"]
    args += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    proc = subprocess.Popen([ffmpeg_exe(), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = w * h * 3
    idx = 0
    try:
        while limit is None or idx < limit:
            buf = proc.stdout.read(n)
            if not buf or len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            idx += 1
            if on_frame:
                on_frame(idx)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()


def read_frame(path: str, index: int, info: Optional[VideoInfo] = None) -> np.ndarray:
    """读取指定序号的单帧（用于逐帧检查）。"""
    if info is None:
        info = probe(path)
    if index < 0:
        index = max(info.frame_count + index, 0)
    fps = info.fps or 30.0
    ts = index / fps
    args = ["-v", "error", "-ss", f"{ts:.6f}", "-i", path,
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = _run(args)
    n = frame_size(info)
    if len(r.stdout) < n:
        raise IndexError(f"帧序号 {index} 超出范围（共约 {info.frame_count} 帧）")
    return np.frombuffer(r.stdout[:n], np.uint8).reshape(info.height, info.width, 3)


# ---------------------------------------------------------------- 写视频


_CODEC_BY_EXT = {
    ".mp4": ("libx264", ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]),
    ".m4v": ("libx264", ["-pix_fmt", "yuv420p"]),
    ".mov": ("libx264", ["-pix_fmt", "yuv420p"]),
    ".mkv": ("libx264", ["-pix_fmt", "yuv420p"]),
    ".avi": ("mpeg4", ["-qscale:v", "3"]),
    # WebM 容器只接受 VP8/VP9/AV1 视频与 Opus/Vorbis 音频，
    # 因此既不能沿用 libx264，也不能用 -c:a copy 直通 AAC。
    # VP9 默认 -deadline good/cpu-used 0 在 1080p 只有个位数帧率，会直接
    # 拖爆视频耗时指标；这里改用实时档并开启多线程。
    ".webm": ("libvpx-vp9", ["-b:v", "0", "-crf", "32", "-deadline", "realtime",
                             "-cpu-used", "5", "-row-mt", "1",
                             "-pix_fmt", "yuv420p"]),
}

# 需要重新编码音轨的容器（无法直通源音频）
_AUDIO_REENCODE = {".webm": ["libopus", "-b:a", "128k"]}


class VideoWriter:
    """流式视频写入器：逐帧写入，自动复制源音频。

    用法：
        with VideoWriter(out, info, audio_from=src) as w:
            for frame in frames:
                w.write(frame)
    """

    def __init__(self, out_path: str, width: int, height: int, fps: float,
                 audio_from: Optional[str] = None):
        self.out_path = out_path
        self.width, self.height = int(width), int(height)
        self.fps = float(fps) if fps and fps > 0 else 30.0
        self.audio_from = audio_from
        self._proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "VideoWriter":
        os.makedirs(os.path.dirname(os.path.abspath(self.out_path)) or ".", exist_ok=True)
        ext = os.path.splitext(self.out_path)[1].lower()
        codec, extra = _CODEC_BY_EXT.get(ext, _CODEC_BY_EXT[".mp4"])

        args = ["-y", "-v", "error",
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s", f"{self.width}x{self.height}",
                "-r", f"{self.fps:.6f}", "-i", "-"]
        if self.audio_from:
            args += ["-i", self.audio_from,
                     "-map", "0:v:0", "-map", "1:a?", "-shortest"]
            if ext in _AUDIO_REENCODE:
                args += ["-c:a", *_AUDIO_REENCODE[ext]]
            else:
                args += ["-c:a", "copy"]
        args += ["-c:v", codec, *extra, self.out_path]

        self._proc = subprocess.Popen([ffmpeg_exe(), *args],
                                      stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
        return self

    def write(self, frame: np.ndarray) -> None:
        if frame.shape[0] != self.height or frame.shape[1] != self.width:
            frame = np.asarray(Image.fromarray(frame).resize(
                (self.width, self.height), Image.BILINEAR))
        self._proc.stdin.write(np.ascontiguousarray(frame, np.uint8).tobytes())

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._proc is None:
            return
        try:
            if exc_type is None:
                self._proc.stdin.close()
            else:
                self._proc.kill()
        except Exception:
            pass
        err = self._proc.stderr.read()
        self._proc.wait()
        if exc_type is None and self._proc.returncode != 0:
            msg = err.decode("utf-8", "ignore").strip()[-400:]
            raise RuntimeError(f"视频写出失败：{msg}")


# ---------------------------------------------------------------- 关键帧


def select_keyframes(path: str, info: Optional[VideoInfo] = None,
                     count: int = 8, scene_threshold: float = 0.10,
                     scan_width: int = 64,
                     max_scan: int = 1200) -> List[int]:
    """选取关键帧：场景切换点优先，其余按均匀分布补齐。

    做法：把整片缩到 64px 宽读成灰度，逐帧求平均绝对差，取显著峰值作为场景
    边界；再用均匀采样补足到 count 个。始终包含首帧与末帧。

    **长视频按步长抽样**：若总帧数超过 max_scan，则只解码每第 stride 帧
    （stride 由总帧数推出），抽样序号再映射回真实帧号。这样上限只约束扫描
    开销，不会把覆盖范围截断在前若干分钟——早期实现直接从头截断扫描，
    导致 30 分钟片子末尾 20 分钟全部复用同一帧深度。
    """
    if info is None:
        info = probe(path)
    total_frames = info.frame_count
    if total_frames <= 0:
        return [0]

    sw = max(int(scan_width), 16)
    sh = max(int(round(sw * info.height / max(info.width, 1))), 16)

    stride = max(1, total_frames // max(int(max_scan), 1))
    sig: List[np.ndarray] = []
    for f in iter_frames(path, info, scale_to=(sw, sh), stride=stride):
        g = f.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
        sig.append(g.reshape(-1) / 255.0)
    if not sig:
        return [0]

    # 抽样序号 k 对应真实帧号 k*stride；末帧用 total-1 兜底
    def _real(k: int) -> int:
        return int(min(k * stride, total_frames - 1))

    samples = len(sig)
    if samples <= count:
        return sorted({_real(k) for k in range(samples)})

    diffs = np.array([float(np.abs(sig[i] - sig[i - 1]).mean())
                      for i in range(1, samples)])
    peaks = [i + 1 for i, d in enumerate(diffs) if d >= scene_threshold]
    peaks.sort(key=lambda i: -diffs[i - 1])

    chosen: List[int] = []

    def _add(k: int) -> None:
        k = int(min(max(k, 0), samples - 1))
        if k not in chosen:
            chosen.append(k)

    _add(0)
    for k in peaks:
        if len(chosen) >= count:
            break
        _add(k)

    step = max(samples // count, 1)
    for n in range(1, count):
        if len(chosen) >= count:
            break
        _add(n * step)
    _add(samples - 1)

    real = sorted({_real(k) for k in chosen})
    # 末帧必须入选，保证选出的关键帧覆盖到片尾
    if real and real[-1] != total_frames - 1:
        real[-1] = total_frames - 1
    return real if count > 1 else real[:1]


# ---------------------------------------------------------------- 深度传播


def _depth_stream(path: str, info: VideoInfo, mode: str,
                  pre: AIPreprocessor, key_idx: Sequence[int],
                  depth_keys: Optional[np.ndarray], smooth: float,
                  force_backend: Optional[str],
                  progress: Callable[[float, str], None],
                  cancel: Optional[Callable[[], bool]],
                  total: int):
    """逐帧产出深度，**不物化全片深度**。

    深度图是全帧分辨率（1080p 单帧约 8MB float32），若先把所有帧的深度
    堆成数组会耗尽内存：1080p30-10s（300 帧）约 2.5GB，5 分钟约 75GB。
    因此这里改为流式：关键帧模式只保留 K 个关键帧的深度（几 MB），
    渲染时按帧号现算线性插值；EMA 平滑只保留上一帧的累加值。

    yield (frame_index, frame_rgb, depth) —— frame_rgb 为该帧 RGB uint8，
    depth 为 HxW float32。把解码出的帧一并带出，渲染阶段无需二次解码。
    """
    acc: Optional[np.ndarray] = None
    key_idx = list(key_idx)

    if mode == "keyframe":
        k = len(key_idx)
        keys = np.asarray(key_idx, np.float64)
        # 单次顺序解码：渲染本来就需要像素，深度在循环内就近插值。
        # 全流程只有「解码一遍 → 渲染一遍 → 编码一遍」。
        for i, frame in enumerate(iter_frames(path, info)):
            if cancel and cancel():
                raise _Cancelled()
            # 定位 i 落在哪一段关键帧区间，现算插值权重
            j = int(np.searchsorted(keys, i, side="right") - 1)
            j = min(max(j, 0), k - 1)
            if j >= k - 1:
                d = depth_keys[k - 1]
            else:
                a, b = key_idx[j], key_idx[j + 1]
                w = (i - a) / max(b - a, 1)
                d = depth_keys[j] * (1.0 - w) + depth_keys[j + 1] * w
            if smooth > 0:
                acc = d.copy() if acc is None else (
                    smooth * d + (1.0 - smooth) * acc)
                d = acc
            yield i, frame, d
        return

    # 逐帧模式：边分析边产出，同样不累积
    for i, frame in enumerate(iter_frames(path, info)):
        if cancel and cancel():
            raise _Cancelled()
        res = pre.get_depth(frame, encode_png(frame), force_backend)
        d = res["depth"]
        if smooth > 0:
            acc = d.copy() if acc is None else (smooth * d + (1.0 - smooth) * acc)
            d = acc
        if i % 5 == 0 or i == total - 1:
            progress(0.02 + 0.60 * (i + 1) / max(total, 1),
                     f"逐帧 AI 分析并渲染 {i + 1}/{total}")
        yield i, frame, d


# ---------------------------------------------------------------- 主处理


def process_video(input_path: str, output_path: str, params: RenderParams,
                  config: Optional[AIBackendConfig] = None,
                  mode: str = "keyframe", keyframe_count: int = 8,
                  progress: Optional[Callable[[float, str], None]] = None,
                  cancel: Optional[Callable[[], bool]] = None,
                  smooth: float = 0.6,
                  force_backend: Optional[str] = None) -> dict:
    """处理整段视频并写出同格式视频。

    mode:
      'keyframe' —— 关键帧分析 + 深度传播（快，光照全片统一）
      'perframe' —— 逐帧分析（慢，光照随画面动态变化）
    cancel: 返回 True 则中止，已写出的部分文件会被删除。
    """
    progress = progress or (lambda p, m: None)
    if mode not in ("keyframe", "perframe"):
        raise ValueError(f"未知视频处理模式：{mode}")

    info = probe(input_path)
    if info.max_side > HARD_MAX_SIDE:
        raise ValueError(f"视频分辨率 {info.width}x{info.height} 超出上限"
                         f"（长边 ≤{HARD_MAX_SIDE}）")
    if info.duration > HARD_MAX_DURATION:
        raise ValueError(f"视频时长 {info.duration:.0f}s 超出上限"
                         f"（≤{HARD_MAX_DURATION}s）")

    cfg = config or AIBackendConfig()
    pre = AIPreprocessor(cfg, AICache())

    total = info.frame_count

    # ---- 阶段 1：关键帧 AI 分析（逐帧模式不做预分析）----
    keys: List[int] = []
    depth_keys: Optional[np.ndarray] = None
    if mode == "keyframe":
        keys = select_keyframes(input_path, info, count=keyframe_count)
        collected: List[np.ndarray] = []
        for i, fi in enumerate(keys):
            if cancel and cancel():
                raise _Cancelled()
            progress(0.02 + 0.20 * i / max(len(keys), 1),
                     f"关键帧 AI 分析 {i + 1}/{len(keys)}（第 {fi} 帧）")
            frame = read_frame(input_path, fi, info)
            res = pre.get_depth(frame, encode_png(frame), force_backend)
            collected.append(res["depth"])
        depth_keys = np.stack(collected)      # 仅 K 帧（通常 8 帧），内存可忽略
        backend = "keyframe×%d" % len(keys)
    else:
        backend = "perframe"

    if cancel and cancel():
        raise _Cancelled()

    # ---- 阶段 2：流式逐帧「取深度 → 渲染 → 写出」----
    progress(0.66 if mode == "keyframe" else 0.02, "逐帧渲染并写出视频")
    ext = os.path.splitext(output_path)[1] or ".mp4"
    tmp_out = output_path + ".part" + ext
    written = 0
    try:
        with VideoWriter(tmp_out, info.width, info.height, info.fps,
                         audio_from=input_path if info.has_audio else None) as w:
            for i, frame, depth in _depth_stream(
                    input_path, info, mode, pre, keys, depth_keys,
                    smooth, force_backend, progress, cancel, total):
                out = render(frame, depth, params, None, None)
                w.write(out)
                written += 1
                if i % 3 == 0 or i == total - 1:
                    progress(0.66 + 0.32 * (i + 1) / max(total, 1),
                             f"渲染 {i + 1}/{total}")
    except _Cancelled:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        raise
    except Exception:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        raise

    os.replace(tmp_out, output_path)
    progress(1.0, "完成")
    return {"output": output_path, "frames": written, "mode": mode,
            "backend": backend, "info": info.to_dict(),
            "keyframes": keys if mode == "keyframe" else None}


class _Cancelled(Exception):
    """内部取消信号。"""

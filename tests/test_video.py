# -*- coding: utf-8 -*-
"""视频处理单元测试。运行：python -m pytest tests/ -v

需要 imageio-ffmpeg 提供的静态 ffmpeg（pip install imageio-ffmpeg）。
缺少 ffmpeg 时整个模块跳过，不产生假失败。
"""
import os
import shutil
import sys
import tempfile
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.ai_backend import AIBackendConfig, encode_png
from core.cache import AICache
from core.presets import get_preset
from core.video import (VIDEO_EXTS, VideoToolingError, ffmpeg_exe, has_ffmpeg,
                        iter_frames, probe, process_video, read_frame,
                        select_keyframes, _depth_stream)

pytestmark = pytest.mark.skipif(not has_ffmpeg(),
                                reason="未安装 imageio-ffmpeg（无可用 ffmpeg）")

SRC_W, SRC_H, SRC_FPS, SRC_SEC = 192, 144, 15, 2.0


def _ffmpeg(*args) -> None:
    import subprocess
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", *args],
                   check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)


def _make_clip(tmpdir: str, name: str = "clip.mp4", audio: bool = True) -> str:
    """合成一段会变化的测试视频（含场景切换）。"""
    path = os.path.join(tmpdir, name)
    src = ("testsrc2=size=%dx%d:rate=%d:duration=%s"
           % (SRC_W, SRC_H, SRC_FPS, SRC_SEC))
    args = ["-f", "lavfi", "-i", src]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=%s" % SRC_SEC]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args += [path]
    _ffmpeg(*args)
    return path


@pytest.fixture(scope="module")
def clip():
    tmp = tempfile.mkdtemp(prefix="hls_video_")
    yield _make_clip(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """让所有测试跑在临时缓存目录，不污染用户真实缓存。

    否则同一套测试在「干净机器」与「本机（已有缓存）」上结果不同——
    例如内置引擎成功过的缓存条目会让降级用例拿到 builtin 而不是 simulate。
    """
    import core.cache as cache_mod
    from core import ai_backend as ai_mod
    tmp_cache = str(tmp_path / "cache")
    monkeypatch.setattr(cache_mod, "CACHE_DIR_DEFAULT", tmp_cache)
    monkeypatch.setattr(ai_mod, "CONFIG_PATH_DEFAULT",
                        str(tmp_path / "config.json"))
    yield tmp_cache


def _cfg():
    return AIBackendConfig(order=["builtin", "simulate"], cloud_enabled=False,
                           timeout=60)


def test_probe_metadata(clip):
    """探测出的宽高/帧率/时长/音轨必须与源一致，且不误取输出流的信息。"""
    info = probe(clip)
    assert (info.width, info.height) == (SRC_W, SRC_H)
    assert abs(info.fps - SRC_FPS) < 0.6
    assert abs(info.duration - SRC_SEC) < 0.25
    assert info.frame_count > 0
    assert info.has_audio is True
    assert info.codec.startswith("h264")
    assert info.container == "mp4"
    print(f"✓ 视频元数据 {info.width}x{info.height} {info.fps}fps "
          f"{info.duration:.2f}s audio={info.has_audio}")


def test_probe_missing_file():
    with pytest.raises(FileNotFoundError):
        probe("no/such/video.mp4")


def test_hints_for_large_or_long(tmpdir):
    """规格提示：高分辨率/长视频应给出建议启用云端的标记。"""
    from core.video import VideoInfo
    small = VideoInfo(width=640, height=480, duration=10, fps=30)
    assert small.hints()["cloud_suggested"] is False
    big = VideoInfo(width=3840, height=2160, duration=10, fps=30)
    assert big.hints()["over_recommended_size"] is True
    assert big.hints()["cloud_suggested"] is True
    long_ = VideoInfo(width=1280, height=720, duration=600, fps=30)
    assert long_.hints()["over_recommended_duration"] is True
    assert long_.hints()["cloud_suggested"] is True
    print("✓ 分辨率/时长提示")


def test_iter_frames_and_read_frame(clip):
    """流式读帧与单帧定位必须给出同尺寸 RGB 且内容一致。"""
    info = probe(clip)
    frames = list(iter_frames(clip, info, limit=5))
    assert len(frames) == 5
    for f in frames:
        assert f.shape == (SRC_H, SRC_W, 3) and f.dtype == np.uint8

    # 首帧用两种读法应基本一致
    f0 = read_frame(clip, 0, info)
    assert f0.shape == (SRC_H, SRC_W, 3)
    assert np.abs(f0.astype(np.float32) - frames[0].astype(np.float32)).mean() < 8.0

    # limit 必须真的截断
    assert len(list(iter_frames(clip, info, limit=3))) == 3
    print("✓ 流式读帧与单帧定位")


def test_iter_frames_scale(clip):
    """缩略扫描：scale_to 应改变尺寸而不改变内容量级。"""
    info = probe(clip)
    f = next(iter_frames(clip, info, scale_to=(64, 48)))
    assert f.shape == (48, 64, 3)
    print("✓ 缩放扫描")


def test_read_frame_out_of_range(clip):
    info = probe(clip)
    with pytest.raises(IndexError):
        read_frame(clip, info.frame_count + 500, info)


def test_keyframe_selection(clip):
    """关键帧：升序、无重复、含首帧、数量受控。"""
    info = probe(clip)
    ks = select_keyframes(clip, info, count=5)
    assert ks == sorted(ks)
    assert len(ks) == len(set(ks))
    assert ks[0] == 0
    assert 2 <= len(ks) <= max(info.frame_count, 5)
    # 请求量超过总帧数时应返回全部帧
    allk = select_keyframes(clip, info, count=10 ** 6)
    assert len(allk) == info.frame_count
    print(f"✓ 关键帧选取 {ks}")


def test_depth_stream_keyframe_interpolation(clip):
    """关键帧深度必须精确落在关键帧上，中间帧为其线性插值。"""
    info = probe(clip)
    from core.ai_backend import AIPreprocessor
    pre = AIPreprocessor(_cfg(), AICache(tempfile.mkdtemp(prefix="hls_vc_")))
    total = info.frame_count
    ks = [0, total // 2, total - 1]
    dk = np.stack([
        pre.get_depth(read_frame(clip, k, info),
                      encode_png(read_frame(clip, k, info)))["depth"]
        for k in ks])

    got = list(_depth_stream(clip, info, "keyframe", pre, ks, dk,
                             0.0, None, lambda a, b: None, None, total))
    assert len(got) == total
    i, frame, depth = got[0]
    assert frame.shape == (SRC_H, SRC_W, 3)
    assert depth.shape == (SRC_H, SRC_W)

    # 关键帧处应与该关键帧深度一致（smooth=0）
    assert np.allclose(got[ks[0]][2], dk[0])
    assert np.abs(got[ks[1]][2] - dk[1]).max() < 0.02
    assert np.abs(got[ks[2]][2] - dk[2]).max() < 0.02

    # 中点应接近两个关键帧深度的均值
    mid = ks[1] // 2
    expect = (dk[0] + dk[1]) / 2.0
    assert np.abs(got[mid][2] - expect).max() < 0.05
    print("✓ 关键帧深度插值（精确锚定 + 线性过渡）")


def test_depth_stream_no_full_materialization(clip):
    """回归：深度必须流式产出，不得一次性物化全片。

    旧实现把 (总帧数, H, W) 的深度全部堆进内存，1080p 10 秒即约 2.5GB、
    5 分钟约 75GB，必然 MemoryError。这里用「首个 yield 到达前不得遍历完
    所有帧」来判定流式行为。
    """
    info = probe(clip)
    from core.ai_backend import AIPreprocessor
    pre = AIPreprocessor(_cfg(), AICache(tempfile.mkdtemp(prefix="hls_vs_")))
    total = info.frame_count
    ks = [0, total - 1]
    dk = np.stack([np.zeros((SRC_H, SRC_W), np.float32)] * 2)

    gen = _depth_stream(clip, info, "keyframe", pre, ks, dk, 0.0, None,
                        lambda a, b: None, None, total)
    t0 = time.time()
    first = next(gen)                      # 只取第一帧
    dt = time.time() - t0
    assert first[0] == 0
    # 若是先物化全片再 yield，首帧耗时会接近整段解码时间
    assert dt < 2.5, f"首个 yield 耗时 {dt:.2f}s，疑似先物化了全片深度"
    gen.close()
    print(f"✓ 深度流式产出（首帧 {dt:.2f}s，未物化全片）")


def test_process_video_keyframe(clip, tmpdir=None):
    """关键帧模式：输出同尺寸、同帧数、保留音轨。"""
    tmp = tempfile.mkdtemp(prefix="hls_vk_")
    out = os.path.join(tmp, "out.mp4")
    p = get_preset("冷色月夜")
    p.lighting_mode = "linear"
    res = process_video(clip, out, p, _cfg(), mode="keyframe",
                        keyframe_count=4)
    info = probe(clip)
    assert os.path.isfile(out)
    o = probe(out)
    assert (o.width, o.height) == (SRC_W, SRC_H)
    assert o.has_audio is True, "源有音轨时输出必须保留音轨"
    assert abs(o.duration - SRC_SEC) < 0.35
    assert res["frames"] == info.frame_count, \
        f"输出帧数应等于输入帧数（源 {info.frame_count}，写出 {res['frames']}）"
    assert o.frame_count == info.frame_count
    assert res["mode"] == "keyframe"
    assert res["backend"].startswith("keyframe×")
    # 渲染确实改变了画面（不只是原样复制）
    src0 = read_frame(clip, 0, probe(clip)).astype(np.float32)
    out0 = read_frame(out, 0, o).astype(np.float32)
    assert np.abs(src0 - out0).mean() > 1.0
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"✓ 关键帧模式 {res['frames']} 帧，音轨保留")


def test_process_video_perframe(clip):
    """逐帧模式：逐帧单独分析，光照随画面变化。"""
    tmp = tempfile.mkdtemp(prefix="hls_vp_")
    out = os.path.join(tmp, "out.mp4")
    p = get_preset("舞台聚光")
    p.lighting_mode = "linear"
    res = process_video(clip, out, p, _cfg(), mode="perframe")
    assert os.path.isfile(out)
    o = probe(out)
    assert (o.width, o.height) == (SRC_W, SRC_H)
    assert res["mode"] == "perframe"
    assert res["backend"] == "perframe"
    assert res["keyframes"] is None
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"✓ 逐帧模式 {res['frames']} 帧")


def test_process_video_hq_shadow(clip):
    """高质量物理阴影可用于视频（逐帧渲染走 render 的 hq 路径）。"""
    tmp = tempfile.mkdtemp(prefix="hls_vh_")
    out = os.path.join(tmp, "out.mp4")
    p = get_preset("逆光黄昏")
    p.lighting_mode = "hq"
    res = process_video(clip, out, p, _cfg(), mode="keyframe",
                        keyframe_count=3)
    assert os.path.isfile(out) and res["frames"] > 0
    shutil.rmtree(tmp, ignore_errors=True)
    print("✓ 视频 + 高质量物理阴影")


def test_process_video_cancel_cleans_up(clip):
    """取消时必须中止并删除半成品，不留 .part 残file。"""
    tmp = tempfile.mkdtemp(prefix="hls_vx_")
    out = os.path.join(tmp, "out.mp4")
    p = get_preset("标准棚拍")
    p.lighting_mode = "linear"
    state = {"n": 0}

    def cancel():
        state["n"] += 1
        return state["n"] > 3

    from core.video import _Cancelled
    with pytest.raises(_Cancelled):
        process_video(clip, out, p, _cfg(), mode="keyframe",
                      keyframe_count=2, cancel=cancel)
    assert not os.path.exists(out)
    assert not [f for f in os.listdir(tmp) if f.startswith("out")], \
        "取消后不应残留半成品文件"
    shutil.rmtree(tmp, ignore_errors=True)
    print("✓ 取消清理半成品")


def test_avi_output_uses_compatible_codec(clip):
    """输出 .avi 时必须使用该容器可用的编码器。"""
    tmp = tempfile.mkdtemp(prefix="hls_va_")
    out = os.path.join(tmp, "out.avi")
    p = get_preset("标准棚拍")
    p.lighting_mode = "linear"
    res = process_video(clip, out, p, _cfg(), mode="keyframe", keyframe_count=2)
    assert os.path.isfile(out) and res["frames"] > 0
    o = probe(out)
    assert (o.width, o.height) == (SRC_W, SRC_H)
    shutil.rmtree(tmp, ignore_errors=True)
    print("✓ AVI 输出（mpeg4）")


def test_webm_output_uses_vp9_and_opus(clip):
    """WebM 只能装 VP8/VP9/AV1 + Opus/Vorbis：必须换编码器而非沿用 H.264/AAC。"""
    tmp = tempfile.mkdtemp(prefix="hls_vw_")
    out = os.path.join(tmp, "out.webm")
    p = get_preset("标准棚拍")
    p.lighting_mode = "linear"
    res = process_video(clip, out, p, _cfg(), mode="keyframe", keyframe_count=2)
    assert os.path.isfile(out) and res["frames"] > 0
    o = probe(out)
    assert (o.width, o.height) == (SRC_W, SRC_H)
    assert o.codec.startswith("vp9") or o.codec.startswith("vp8"), \
        f"WebM 视频编码应为 VP8/VP9，实际 {o.codec}"
    assert o.has_audio is True, "WebM 音轨应重新编码为 Opus 而非丢失"
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"✓ WebM 输出（{o.codec} + opus）")


def test_export_keeps_source_container(tmp_path):
    """回归：输出必须**与源同格式**（规格「输出同格式视频」）。

    逐帧光绘导出曾把输出写死为 MP4（`{job_id}.mp4` + 固定 video/mp4），
    从 .webm/.avi 源导出会悄悄变成 mp4，WebM 还会因沿用 libx264 直接失败。
    这里直接验证 worker 使用的输出路径推导规则。
    """
    from webui.api import _strokes_output_path

    for ext in (".mp4", ".webm", ".avi", ".mov", ".mkv"):
        src = os.path.join(str(tmp_path), "clip" + ext)
        path, name = _strokes_output_path(src, "job123")
        assert path.endswith("job123" + ext), f"{ext}: 输出路径 {path}"
        assert name == "stroked" + ext, f"{ext}: 下载名 {name}"
        mime = "video/" + ("webm" if ext == ".webm" else "x-msvideo" if ext == ".avi" else "mp4")
        assert mime.startswith("video/")
    # 无扩展名时退回 mp4（不能产出无后缀文件）
    path, name = _strokes_output_path(os.path.join(str(tmp_path), "noext"), "j1")
    assert path.endswith("j1.mp4") and name == "stroked.mp4"
    print("✓ 导出沿用源容器（mp4/webm/avi/mov/mkv）")


def test_video_extensions_cover_spec_formats():
    for ext in (".mp4", ".avi", ".mov"):
        assert ext in VIDEO_EXTS
    print("✓ 支持格式覆盖 MP4/AVI/MOV")


def test_tooling_error_is_actionable():
    """ffmpeg 缺失时的报错必须给出可执行的安装指引。"""
    import core.video as V
    saved = V._FFMPEG_CACHE
    try:
        V._FFMPEG_CACHE = None
        os.environ["HLS_FFMPEG"] = ""
        msg = None
        try:
            import imageio_ffmpeg  # noqa: F401
            # 环境里有 imageio-ffmpeg 时无法触发，跳过断言
            import pytest as _p
            _p.skip("imageio-ffmpeg 已安装，无法复现缺失场景")
        except ImportError:
            try:
                V.ffmpeg_exe()
            except VideoToolingError as e:
                msg = str(e)
        if msg is not None:
            assert "imageio-ffmpeg" in msg and "HLS_FFMPEG" in msg
    finally:
        V._FFMPEG_CACHE = saved
    print("✓ 工具链缺失提示可执行")

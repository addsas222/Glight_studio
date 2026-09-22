# -*- coding: utf-8 -*-
"""凌日光影棚 —— GIMP 2.10+ 插件（hls_light.py）

安装（Windows）：
  复制本文件到  C:\\Users\\<用户名>\\AppData\\Roaming\\GIMP\\2.10\\plug-ins\\
  （或 GIMP 安装目录\\lib\\gimp\\2.0\\plug-ins\\），重启 GIMP。
安装（Linux/macOS）：
  ~/.config/GIMP/2.10/plug-ins/   并确保本文件有可执行权限（chmod +x）。

使用：菜单 滤镜 → 凌日光影棚 → 智能光照增强…
结果作为新图层添加到当前图像。

实现说明：GIMP 2.10 内置 Python 为 Python 2 且无 Qt，因此插件通过
独立进程调用本项目的渲染管线（desktop/server 共享 core 引擎）：
  1. 把当前图层导出为临时 PNG；
  2. 调用 hls_cli.py（与本插件同目录，依赖项目源码或打包后的 exe）；
  3. 读取结果 PNG，作为新图层载回。
依赖：本插件目录下需存在 hls 核心包（horizon_light_studio 仓库整体）
     或打包版 hls_cli.exe。
"""

import os
import subprocess
import sys
import tempfile

from gimpfu import *  # noqa: F401,F403  GIMP 2.10 常规插件头

# 项目根目录查找顺序：插件同目录 → 环境变量 HLS_HOME → 打包 exe
_here = os.path.dirname(os.path.abspath(__file__))


def _find_runner():
    """返回 (cmd_list, cwd) 用于执行渲染。"""
    exe = os.path.join(_here, "hls_cli.exe")
    if os.path.isfile(exe):
        return [exe], _here
    home = os.environ.get("HLS_HOME", os.path.dirname(_here))
    cli = os.path.join(home, "hls_cli.py")
    if os.path.isfile(cli):
        py = sys.executable
        return [py, cli], home
    return None, None


def hls_light_enhance(image, drawable, preset, shadow_mode, quality,
                        ambient, exposure, progress_bar=True):
    img = image
    # 1) 导出当前图层
    tmpdir = tempfile.mkdtemp(prefix="hls_gimp_")
    src = os.path.join(tmpdir, "input.png")
    dst = os.path.join(tmpdir, "output.png")
    pdb.file_png_save(img, drawable, src, "input.png", 0, 9, 1, 1, 1, 1, 1)

    # 2) 调用渲染 CLI
    runner, cwd = _find_runner()
    if runner is None:
        pdb.gimp_message("未找到渲染核心：请将 horizon_light_studio 项目源码与"
                         "本插件放在一起，或安装 hls_cli.exe 到插件目录。")
        return
    cmd = runner + [
        "--preset", preset,
        "--shadow", shadow_mode,
        "--lighting", ("hq" if quality == "hq" else "linear"),
        "--ambient", str(ambient),
        "--exposure", str(exposure),
        src, dst,
    ]
    # GIMP 2.10 的插件环境是 Python 2.7：communicate() 没有 timeout 参数，
    # 直接调用会抛 TypeError 并被当成“超时”，导致永远拿不到结果图层。
    # 因此把输出重定向到临时文件后自行轮询（同时避免 PIPE 缓冲区死锁）。
    startupinfo = None
    if os.name == "nt":
        import subprocess as _s
        startupinfo = _s.STARTUPINFO()
        startupinfo.dwFlags |= _s.STARTF_USESHOWWINDOW
    out_f = open(os.path.join(tmpdir, "stdout.log"), "w+b")
    err_f = open(os.path.join(tmpdir, "stderr.log"), "w+b")
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=out_f, stderr=err_f,
                            startupinfo=startupinfo)
    import time as _time
    deadline = _time.time() + 120
    while proc.poll() is None and _time.time() < deadline:
        _time.sleep(0.2)
    timed_out = proc.poll() is None
    if timed_out:
        proc.kill()
        proc.wait()
    out_f.close()
    err_f.close()
    if timed_out:
        pdb.gimp_message("渲染超时（>120s），已取消。")
        return
    if proc.returncode != 0 or not os.path.isfile(dst):
        try:
            err = open(os.path.join(tmpdir, "stderr.log"), "rb").read()
            msg = err.decode("utf-8", "ignore")[-600:]
        except (IOError, OSError):
            msg = ""
        pdb.gimp_message("渲染失败：\n" + (msg or "未产生输出"))
        return

    # 3) 载回为新图层
    layer = pdb.gimp_file_load_layer(img, dst)
    layer_name = "智能光照增强 (%s/%s)" % (preset, quality)
    pdb.gimp_item_set_name(layer, layer_name)
    pdb.gimp_image_insert_layer(img, layer, None, 0)
    pdb.gimp_image_set_active_layer(img, layer)
    pdb.gimp_displays_flush()
    try:
        os.remove(src)
        os.remove(dst)
        os.rmdir(tmpdir)
    except OSError:
        pass


register(
    "python-fu-hls-light-enhance",
    "加查俱乐部风格智能光照增强：AI 深度估计 + 多光源物理渲染，"
    "结果输出为新图层。",
    "AI 深度（本地/云端双轨）+ 多光源物理光照 + 2.5D 阴影投射",
    "Horizon Light Studio", "MIT", "2026",
    "<Image>/Filters/凌日光影棚/智能光照增强…",
    "RGB*, GRAY*",
    [
        (PF_RADIO, "preset", "预设场景", "标准棚拍",
         ("标准棚拍", "逆光黄昏", "冷色月夜", "舞台聚光", "霓虹氛围")),
        (PF_RADIO, "shadow_mode", "阴影锐度", "soft",
         (("soft", "soft"), ("hard", "hard"))),
        (PF_RADIO, "quality", "性能模式", "linear",
         (("linear", "快速预览"), ("hq", "高质量物理阴影"))),
        (PF_SPINNER, "ambient", "环境光强度", 0.45, (0, 2, 0.05)),
        (PF_SPINNER, "exposure", "整体曝光", 1.0, (0.2, 2.5, 0.05)),
    ],
    [],
    hls_light_enhance,
    display_name="智能光照增强…"
)

main()

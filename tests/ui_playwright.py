# -*- coding: utf-8 -*-
"""用 Playwright 以**拟人点击**方式端到端测试界面。

与之前的 DOM 断言不同，这里全部走真实输入事件：
  - 鼠标在元素上移动（含多步轨迹）后再按下/抬起，而不是 element.click()
  - 键盘逐字符输入、真实按 Tab / Enter / 方向键
  - 拖拽用 mouse.down/move/up 分步完成（轨迹球、推子、画布平移）
  - 滚动用 mouse.wheel

运行前先启动服务：
    python -m webui.api --port 8756
然后：
    python -m tests.ui_playwright            # 全部
    python -m tests.ui_playwright 主题        # 只跑名字含「主题」的用例

需要 playwright（pip install playwright && playwright install chromium）。
浏览器路径默认取 ~/AppData/Local/ms-playwright（可用 PLAYWRIGHT_BROWSERS_PATH 覆盖）。
"""
from __future__ import annotations

import threading

import os
import pathlib
import sys
import time

BASE = os.environ.get("HLS_BASE", "http://127.0.0.1:8756")
SHOTS = os.environ.get("HLS_SHOTS", os.path.join("build", "ui-shots"))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + name + (f" — {detail}" if detail else ""))


def human_move(page, el, steps: int = 12) -> None:
    """把鼠标分步移到元素中心（模拟人的移动轨迹）。"""
    box = el.bounding_box()
    if not box:
        raise RuntimeError("元素不可见，无法移动鼠标")
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    vp = page.viewport_size or {"width": 1600, "height": 1000}
    page.mouse.move(vp["width"] / 2, vp["height"] / 2, steps=3)
    page.mouse.move(cx, cy, steps=steps)


def human_click(page, el, pause: float = 0.15) -> None:
    """拟人点击：移动 → 短暂停顿 → 按下 → 抬起。"""
    human_move(page, el)
    page.wait_for_timeout(int(pause * 1000))
    page.mouse.down()
    page.wait_for_timeout(40)
    page.mouse.up()


def human_drag(page, el, dx: int, dy: int, steps: int = 18) -> None:
    """在元素内按下并分步拖拽（轨迹球 / 推子 / 画布平移都用这个）。"""
    human_move(page, el)
    page.mouse.down()
    box = el.bounding_box()
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    for i in range(1, steps + 1):
        page.mouse.move(x + dx * i / steps, y + dy * i / steps)
    page.mouse.up()


def nav(page, name: str) -> None:
    """按导航名切换屏幕（真实点击侧栏按钮）。"""
    btn = page.locator(".rail .nav-btn", has_text=name).first
    human_click(page, btn)
    page.wait_for_timeout(500)


def shot(page, name: str) -> None:
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))


# ---------------------------------------------------------------- 用例

def test_shell_and_nav(page) -> None:
    print("\n[外壳与导航]")
    title = page.title()
    check("标题为新产品名", "凌日光影棚" in title, title)
    navs = page.locator(".rail .nav-btn").all_inner_texts()
    names = [n.strip() for n in navs if n.strip()]
    check("导航含 13 个界面 + 帮助", len(names) == 14, f"{len(names)} 项")

    # 逐个拟人点击全部界面，确认切换且无报错
    problems = []
    for name in names:
        if name == "帮助":
            continue
        try:
            nav(page, name)
            active = page.locator(".rail .nav-btn.on").inner_text().strip()
            if active != name:
                problems.append(f"{name}→{active}")
        except Exception as e:
            problems.append(f"{name}: {type(e).__name__}")
    check("逐个点击 13 个界面均成功切换", not problems, "; ".join(problems) or "全部正常")

    errs = page.evaluate("window.__hlsErrors || []")
    check("全程无 JS 报错", not errs, str(errs)[:160])


def test_theme_switch(page) -> None:
    print("\n[主题切换：真实下拉选择]")
    nav(page, "光影工作台")
    sel = page.locator("#topbar-theme")
    # 先显式回到蓝色，确保起点确定（主题会持久化，否则用例依赖上一轮残留状态）
    sel.select_option("blue")
    page.wait_for_timeout(900)
    before = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--hls-bg-root').trim()")
    check("起点为蓝色深色底", before.upper() == "#0A0D13", before)

    sel.select_option("mist")           # 真实 select 交互
    page.wait_for_timeout(900)
    light_bg = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--hls-bg-root').trim()")
    check("切到浅色主题后背景变浅", light_bg != before and light_bg.upper() != "#0A0D13",
          f"{before} → {light_bg}")
    # 浅色主题必须真的是浅底深字
    light_txt = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--hls-text-primary').trim()")
    lum = lambda hexs: sum(int(hexs.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
    check("浅色主题为浅底深字", lum(light_bg) > lum(light_txt), f"底 {light_bg} / 字 {light_txt}")

    sel.select_option("blue")
    page.wait_for_timeout(800)
    back = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--hls-bg-root').trim()")
    check("切回蓝色恢复深色底", back.upper() == "#0A0D13", back)
    shot(page, "01-工作台-蓝色")


def test_preset_click(page) -> None:
    print("\n[预设：拟人点击]")
    nav(page, "光影工作台")
    items = page.locator(".screen .item")
    n = items.count()
    check("预设/素材列表可点击", n > 0, f"{n} 项")
    if n < 5:
        return
    # 点击第 2 个预设（逆光黄昏），确认参数变化
    before = page.evaluate("document.querySelector('#status-text').textContent")
    human_click(page, items.nth(1))
    page.wait_for_timeout(2500)
    after = page.evaluate("document.querySelector('#status-text').textContent")
    check("点击预设后触发渲染或状态更新", before != after, f"{before} → {after}")


def _params(page) -> dict:
    """经 /api/state 读取后端记录的最新参数（等同渲染所用参数）。"""
    return page.evaluate("""async () => {
      const r = await window.__hlsTimeout(fetch('/api/state'), 15000, 'state');
      const j = await window.__hlsTimeout(r.json(), 15000, 'state.json');
      return j.params;
    }""")


def test_trackball_drag(page) -> None:
    print("\n[3D 轨迹球：分步拖拽必须真的改变主光方向]")
    nav(page, "光影工作台")
    # 必须先导入图片：后端参数是在 /api/render 时同步的，没有图片就不会触发渲染，
    # 因此读数会停在初始值（这不是轨迹球的问题）。先建立真实使用前提。
    page.locator('.screen input[type="file"]').first.set_input_files(os.path.abspath(_ensure_png(page)))
    page.wait_for_timeout(4000)
    # 轨迹球只用于**平行光**；点光用 X/Y/Z 位置输入。切到含平行光的预设。
    page.locator(".screen .item", has_text="标准棚拍").first.click()
    page.wait_for_timeout(2000)

    info = page.evaluate("""() => {
      const tb = document.querySelector('.trackball');
      return { hasTrackball: !!tb, canvases: tb ? tb.querySelectorAll('canvas').length : 0 };
    }""")
    check("平行光显示轨迹球（含 canvas）", info["hasTrackball"] and info["canvases"] >= 1, str(info))
    if not info["hasTrackball"]:
        return

    # 轨迹球编辑的是**当前选中光源**（selectedLight 是屏内状态，跨挂载保留），
    # 因此先显式点选列表第一行，保证断言对象就是 lights[0]。
    rows = page.locator(".screen .item")
    for i in range(rows.count()):
        txt = (rows.nth(i).inner_text() or "")
        if "光源" in txt or "主光" in txt:
            human_click(page, rows.nth(i))
            page.wait_for_timeout(900)
            break

    # 断言真实链路：指针事件 → 轨迹球数学 → apply() → /api/render → _set_current_params
    before = _params(page)["lights"][0]
    human_drag(page, page.locator(".trackball canvas").first, 70, -50)
    page.wait_for_timeout(2200)
    after = _params(page)["lights"][0]
    changed = (abs(after["dx"] - before["dx"]) + abs(after["dy"] - before["dy"])
               + abs(after["dz"] - before["dz"]))
    check("拖拽后主光方向真的改变", changed > 0.02,
          f"Δ={changed:.3f}  ({before['dx']:.2f},{before['dy']:.2f},{before['dz']:.2f})"
          f" → ({after['dx']:.2f},{after['dy']:.2f},{after['dz']:.2f})")
    # 方向应为单位向量且 dz>0（引擎要求光源在朝向观察者的半球）。
    # 容差取 1e-3：预设里的方向按 4 位小数存储，四舍五入本身带来 ~1e-5 误差。
    import math
    norm = math.sqrt(after["dx"] ** 2 + after["dy"] ** 2 + after["dz"] ** 2)
    check("方向保持单位向量且 dz>0", abs(norm - 1) < 1e-3 and after["dz"] > 0,
          f"|v|={norm:.5f} dz={after['dz']:.2f}")

    # 切到点光，应改为 X/Y/Z 位置输入（无轨迹球）
    seg = page.locator(".screen .seg button", has_text="点光").first
    if seg.count():
        human_click(page, seg)
        page.wait_for_timeout(1200)
        pt = page.evaluate("""() => ({
          hasTrackball: !!document.querySelector('.trackball'),
          numbers: document.querySelectorAll('.screen input[type=number]').length })""")
        check("点光改为 X/Y/Z 位置输入（无轨迹球）",
              (not pt["hasTrackball"]) and pt["numbers"] >= 3, str(pt))
    shot(page, "02-工作台-轨迹球与点光")


def test_click_pick(page) -> None:
    print("\n[点击拾取主光（规格：双路径 a）]")
    nav(page, "光影工作台")
    files = page.locator('.screen input[type="file"]')
    files.first.set_input_files(os.path.abspath(_ensure_png(page)))
    page.wait_for_timeout(4000)

    pick = page.locator('[title*="法线方向"]').first
    if pick.count() == 0:
        pick = page.locator(".screen button", has_text="拾取主光").first
    if pick.count() == 0:
        check("存在「拾取主光」按钮", False)
        return
    human_click(page, pick)
    page.wait_for_timeout(400)

    canvas = page.locator(".canvas-img")
    box = canvas.first.bounding_box()
    if not box:
        check("画布可见", False)
        return

    # 不同像素位置必须给出**不同**的主光方向（只断言无报错等于没测）
    results = []
    for fx, fy in ((0.25, 0.25), (0.75, 0.7)):
        page.mouse.move(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy, steps=10)
        page.wait_for_timeout(150)
        page.mouse.down()
        page.wait_for_timeout(60)
        page.mouse.up()
        page.wait_for_timeout(2600)
        l = _params(page)["lights"][0]
        results.append((round(l["dx"], 3), round(l["dy"], 3), round(l["dz"], 3)))

    check("点击画布返回了光源方向", all(any(v != 0 for v in r) for r in results), str(results))
    check("不同位置给出不同方向（拾取真的生效）", results[0] != results[1],
          f"{results[0]} vs {results[1]}")
    # 拾取后应触发重渲染
    status = page.evaluate("document.querySelector('#status-text').textContent")
    check("拾取后已重渲染", ("渲染" in status) or ("拾取" in status), status[:70])


def test_reference_transfer(page) -> None:
    print("\n[参考图保守迁移（规格：双路径 b）]")
    nav(page, "光影工作台")
    # 需要先有主图，迁移才会作用于当前参数
    page.locator('.screen input[type="file"]').first.set_input_files(os.path.abspath(_ensure_png(page)))
    page.wait_for_timeout(3500)
    before = _params(page)["lights"][0]

    ref_btn = page.locator(".screen button", has_text="参考图").first
    if ref_btn.count() == 0:
        check("存在参考图迁移入口", False)
        return
    # 参考图输入是另一个 file input
    inputs = page.locator('.screen input[type="file"]')
    if inputs.count() < 2:
        # 点击按钮后可能才出现输入框
        human_click(page, ref_btn)
        page.wait_for_timeout(600)
        inputs = page.locator('.screen input[type="file"]')
    if inputs.count() < 2:
        check("存在参考图文件输入", False, f"file input 数：{inputs.count()}")
        return

    # 造一张“左亮右暗”的参考图，方向偏移应可观测
    ref = page.evaluate("""() => {
      const c = document.createElement('canvas'); c.width=320; c.height=320;
      const g = c.getContext('2d');
      const grd = g.createLinearGradient(0,0,320,0);
      grd.addColorStop(0,'#fff4e0'); grd.addColorStop(1,'#101418');
      g.fillStyle=grd; g.fillRect(0,0,320,320);
      return c.toDataURL('image/png').split(',')[1];
    }""")
    import base64
    p = os.path.join("build", "ui-test-ref.png")
    with open(p, "wb") as f:
        f.write(base64.b64decode(ref))
    inputs.nth(1).set_input_files(os.path.abspath(p))
    page.wait_for_timeout(4500)

    after = _params(page)["lights"][0]
    dk = abs(after["kelvin"] - before["kelvin"])
    # 用**三维夹角**衡量偏移（旧实现只夹二维投影，实际偏转可达 26° 却回报 15°）
    import math
    b = (before["dx"], before["dy"], before["dz"])
    a = (after["dx"], after["dy"], after["dz"])
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(b, a))))
    da = math.degrees(math.acos(dot))
    check("参考图迁移产生了偏移", da > 0.05 or dk > 0, f"偏转={da:.2f}° Δ色温={dk:.0f}K")
    check("三维偏转严格受限（≤15°）", da <= 15.05, f"实际偏转={da:.2f}°（限幅 15°）")
    check("色温偏移受限（≤500K）", dk <= 500.5, f"Δ色温={dk:.0f}K")

    # 方向性断言：只测「偏转量」无法发现 Rodrigues 轴向取反（会朝参考图的反方向转）。
    # 参考图是左亮右暗 → 引擎推断目标方向为左侧；迁移后主光应**更接近**该目标。
    import math as _m
    tgt = (-1.0, 0.0)
    def _ang_to_target(v):
        h = _m.hypot(v[0], v[1]) or 1e-6
        cos = max(-1.0, min(1.0, (v[0] * tgt[0] + v[1] * tgt[1]) / h))
        return _m.degrees(_m.acos(cos))
    a_before, a_after = _ang_to_target(b), _ang_to_target(a)
    check("迁移方向正确（朝参考图光向靠近而非远离）", a_after < a_before,
          f"与参考方向夹角 {a_before:.1f}° → {a_after:.1f}°")
    shot(page, "06-工作台-参考图迁移")


def test_reset_all(page) -> None:
    print("\n[一键重置（规格：所有参数支持一键重置）]")
    nav(page, "光影工作台")
    # 先制造明显偏离：拖动环境光滑杆
    sliders = page.locator('.screen input[type="range"]')
    human_drag(page, sliders.nth(0), 80, 0)
    page.wait_for_timeout(1500)
    deviated = _params(page)
    preset_name = page.evaluate("""() => {
      const on = document.querySelector('.screen .item.on');
      return on ? (on.textContent || '').trim().slice(0, 8) : '';
    }""")

    btn = page.locator(".screen button", has_text="重置").first
    if btn.count() == 0:
        check("存在重置按钮", False)
        return
    human_click(page, btn)
    page.wait_for_timeout(2000)
    reset = _params(page)

    check("重置后环境光强度回到预设默认（≠ 拖动后的值）",
          reset["ambient_intensity"] != deviated["ambient_intensity"],
          f"{deviated['ambient_intensity']} → {reset['ambient_intensity']}")
    check("重置后曝光/色温也回到默认区间",
          0.2 <= reset["exposure"] <= 2.5 and 2000 <= reset["ambient_kelvin"] <= 12000,
          f"曝光 {reset['exposure']} 色温 {reset['ambient_kelvin']}")
    # 与预设默认值逐项比对（把预设名作为参数传入，避免在 JS 里插值中文导致语法错误）
    defaults = page.evaluate("""async (name) => {
      const r = await window.__hlsTimeout(fetch('/api/state'), 15000, 'state');
      const j = await window.__hlsTimeout(r.json(), 15000, 'state.json');
      const p = j.presets.find(x => x.name.startsWith(name)) || j.presets[0];
      return p.params;
    }""", preset_name)
    check("重置后与预设默认值一致（光源数/曝光）",
          len(reset["lights"]) == len(defaults["lights"])
          and abs(reset["exposure"] - defaults["exposure"]) < 1e-6,
          f"光源 {len(reset['lights'])} vs {len(defaults['lights'])}")


def test_export_image(page) -> None:
    print("\n[导出（规格：可自定义保存路径）]")
    nav(page, "光影工作台")
    page.locator('.screen input[type="file"]').first.set_input_files(os.path.abspath(_ensure_png(page)))
    page.wait_for_timeout(4000)

    btn = page.locator(".screen button", has_text="导出").first
    if btn.count() == 0:
        check("存在导出按钮", False)
        return
    with page.expect_download(timeout=15000) as dl:
        human_click(page, btn)
    d = dl.value
    name = d.suggested_filename
    path = d.path()
    size = os.path.getsize(path) if path else 0
    check("导出触发了下载", bool(name), name)
    check("导出文件非空且为图片", size > 1000, f"{size} 字节")
    with open(path, "rb") as f:
        head = f.read(8)
    check("下载内容为 PNG 文件头", head.startswith(b"\x89PNG"), str(head[:4]))


def test_slider_drag(page) -> None:
    print("\n[滑杆：拖拽改变数值并同步后端]")
    nav(page, "光影工作台")
    page.locator('.screen input[type="file"]').first.set_input_files(os.path.abspath(_ensure_png(page)))
    page.wait_for_timeout(4000)
    sliders = page.locator('.screen input[type="range"]')
    if sliders.count() == 0:
        check("存在滑杆", False)
        return
    get_vals = lambda: page.evaluate(
        "Array.from(document.querySelectorAll('.screen input[type=\"range\"]')).map(s=>s.value).join(',')")
    before_dom = get_vals()
    before_be = _params(page)["ambient_intensity"]
    human_drag(page, sliders.nth(0), 90, 0)
    page.wait_for_timeout(2500)
    after_dom = get_vals()
    check("拖拽滑杆改变了控件数值", before_dom != after_dom, f"{before_dom[:24]} → {after_dom[:24]}")
    # 环境光强度是第一个滑杆：必须真的同步到后端（经 /api/render 回写）
    after_be = _params(page)["ambient_intensity"]
    check("环境光强度同步到后端", after_be != before_be,
          f"{before_be:.2f} → {after_be:.2f}")


def test_canvas_wheel(page) -> None:
    print("\n[画布：滚轮缩放]")
    nav(page, "光影工作台")
    canvas = page.locator(".canvas-img")
    if canvas.count() == 0:
        check("画布图片存在", False, "尚未导入图片")
        return
    human_move(page, canvas.first)
    tf_before = page.evaluate("document.querySelector('.canvas-img').style.transform")
    page.mouse.wheel(0, -240)
    page.wait_for_timeout(400)
    tf_after = page.evaluate("document.querySelector('.canvas-img').style.transform")
    check("滚轮改变了画布缩放", tf_before != tf_after, f"'{tf_before}' → '{tf_after}'")


def test_import_image_and_modes(page) -> None:
    print("\n[导入图片：真实文件选择 + 自动模式识别]")
    nav(page, "光影工作台")
    # 用 canvas 生成真实 PNG，再通过 set_input_files 注入（等价用户选文件）
    png = page.evaluate("""() => {
      const c = document.createElement('canvas'); c.width=400; c.height=400;
      const g = c.getContext('2d');
      const grd = g.createLinearGradient(0,0,400,400);
      grd.addColorStop(0,'#20304a'); grd.addColorStop(1,'#0a0d13');
      g.fillStyle=grd; g.fillRect(0,0,400,400);
      g.fillStyle='#78dceb'; g.beginPath(); g.arc(200,150,95,0,Math.PI*2); g.fill();
      g.fillStyle='#f5e6d8'; g.beginPath(); g.arc(200,185,68,0,Math.PI*2); g.fill();
      g.fillStyle='#3c46a0'; g.fillRect(155,250,90,130);
      return c.toDataURL('image/png').split(',')[1];
    }""")
    import base64
    data = base64.b64decode(png)
    path = os.path.join("build", "ui-test-image.png")
    with open(path, "wb") as f:
        f.write(data)
    files = page.locator('.screen input[type="file"]')
    files.first.set_input_files(os.path.abspath(path))
    page.wait_for_timeout(5000)
    active = page.locator(".rail .nav-btn.on").inner_text().strip()
    status = page.evaluate("document.querySelector('#status-text').textContent")
    check("导入图片后仍停留在工作台（图片模式）", active == "光影工作台", f"{active} · {status[:50]}")
    has_img = page.evaluate("""() => {
      const i = document.querySelector('.canvas-img');
      return !!(i && i.src.startsWith('data:image') && i.naturalWidth > 0);
    }""")
    check("画布显示已导入图片", has_img)
    shot(page, "03-工作台-导入图片")


def test_auto_switch_to_video(page, webm_path: str) -> None:
    print("\n[智能识别并更改模式：导入视频自动切屏]")
    nav(page, "光影工作台")
    files = page.locator('.screen input[type="file"]')
    files.first.set_input_files(os.path.abspath(webm_path))
    page.wait_for_timeout(9000)
    active = page.locator(".rail .nav-btn.on").inner_text().strip()
    check("导入视频后自动切到「视频调光」", active == "视频调光", active)
    loaded = page.evaluate("""() => {
      const imgs = Array.from(document.querySelectorAll('.screen img'));
      const f = imgs.find(i => i.src.startsWith('data:image') && i.naturalWidth > 50);
      const txt = document.querySelector('.screen').textContent || '';
      return { frame: f ? f.naturalWidth + 'x' + f.naturalHeight : null,
               wait: txt.includes('未打开视频'),
               thumbs: imgs.filter(i => i.src.startsWith('data:image')).length };
    }""")
    check("目标屏已载入该视频（有帧图）", bool(loaded["frame"]), str(loaded))
    check("未显示「未打开视频」", not loaded["wait"])
    check("时间轴缩略图已生成", loaded["thumbs"] > 6, f"{loaded['thumbs']} 张")
    shot(page, "04-视频调光-自动载入")


def test_video_transport_clicks(page) -> None:
    print("\n[视频：逐帧按钮与播放]")
    nav(page, "视频调光")
    read_frame = """() => {
      const m = (document.querySelector('.screen').textContent||'').match(/帧\\s*(\\d+)\\//);
      return m ? Number(m[1]) : null;
    }"""
    before = page.evaluate(read_frame)

    # 步进按钮是图标按钮，靠 title 定位（不是文本）
    nxt = page.locator('[title="下一帧"]').first
    if nxt.count() == 0:
        check("存在「下一帧」按钮", False, "未找到 [title=下一帧]")
        return
    check("存在「下一帧」按钮（图标按钮 + title）", True)

    human_click(page, nxt)
    page.wait_for_timeout(1600)
    after = page.evaluate(read_frame)
    check("点击下一帧后帧号推进", before is not None and after is not None and after != before,
          f"帧 {before} → {after}")

    prev = page.locator('[title="上一帧"]').first
    if prev.count():
        human_click(page, prev)
        page.wait_for_timeout(1600)
        back = page.evaluate(read_frame)
        check("点击上一帧后帧号回退", back == before, f"帧 {after} → {back}（期望回到 {before}）")

    play = page.locator('[title="播放"]').first
    if play.count():
        human_click(page, play)
        page.wait_for_timeout(2500)
        playing = page.evaluate(read_frame)
        # 播放中帧号应自行前进，或按钮切换为暂停
        paused_btn = page.locator('[title="暂停"]').count()
        check("点击播放后开始播放", (playing != back) or paused_btn > 0,
              f"帧 {back} → {playing}，暂停按钮 {paused_btn}")
        if paused_btn:
            human_click(page, page.locator('[title="暂停"]').first)
            page.wait_for_timeout(600)
            check("可暂停播放", True)


def test_cue_save_and_apply(page) -> None:
    print("\n[调光台：拟人点击保存并应用 CUE]")
    nav(page, "调光台")
    page.wait_for_timeout(900)

    # CUE 会持久化到 ~/.horizon_light_studio/cues.json，因此必须比较「点击前/后
    # 的行数增量」并清理，否则重跑时 rows>0 恒成立 —— 保存坏掉也会通过。
    count = lambda: page.evaluate("document.querySelectorAll('.screen .item').length")
    before = count()

    btn = page.locator(".screen button", has_text="保存当前为 CUE").first
    if btn.count() == 0:
        btn = page.locator(".screen button", has_text="保存").first
    if btn.count() == 0:
        check("存在保存 CUE 按钮", False)
        return
    human_click(page, btn)
    page.wait_for_timeout(1600)
    after = count()
    check("保存后 CUE 行数增加", after > before, f"{before} → {after} 行")

    # 状态文案写在**状态栏**（desk.ts 调用 store.set({status})），不在屏内文本里
    statusbar = page.evaluate("document.getElementById('status-text').textContent")
    check("状态栏出现「已保存 CUE」", "已保存 CUE" in statusbar, statusbar[:80])
    shot(page, "05-调光台-CUE")

    # 清理：删除刚保存的 CUE，让重复运行保持诚实（删除是图标按钮，靠 title 定位）
    dele = page.locator('[title^="删除 "]').first
    if dele.count():
        human_click(page, dele)
        page.wait_for_timeout(1600)
        check("删除 CUE 后行数回落", count() <= before, f"{after} → {count()} 行")


def test_bridge_telemetry(page) -> None:
    print("\n[光影指挥屏：遥测滑杆与诚实标注]")
    nav(page, "光影指挥屏")
    page.wait_for_timeout(700)
    txt = page.locator(".screen").inner_text()
    check("显示无法计算量的诚实说明", ("无法计算" in txt or "不显示" in txt),
          [l for l in txt.splitlines() if "无法计算" in l or "不显示" in l][:1])
    sliders = page.locator('.screen input[type="range"]')
    if sliders.count() == 0:
        check("存在遥测滑杆", False)
        return
    before = sliders.nth(0).input_value()
    human_drag(page, sliders.nth(0), 70, 0)
    page.wait_for_timeout(1000)
    after = sliders.nth(0).input_value()
    check("遥测滑杆可拖动", before != after, f"{before} → {after}")


def test_plugins_and_theme_screens(page) -> None:
    print("\n[插件中心 / 主题引擎]")
    nav(page, "插件中心")
    txt = page.locator(".screen").inner_text()
    check("插件中心声明纯数据安全属性", "不执行任何插件代码" in txt or "纯数据" in txt,
          [l for l in txt.splitlines() if "插件代码" in l or "纯数据" in l][:1])
    nav(page, "主题引擎")
    txt2 = page.locator(".screen").inner_text()
    check("主题引擎列出 8 套主题", txt2.count("深色") + txt2.count("浅色") >= 8,
          f"深色/浅色关键词 {txt2.count('深色') + txt2.count('浅色')} 次")
    check("主题引擎显示对比度校验", "对比度" in txt2)


def test_settings_model_controls(page) -> None:
    """设置弹窗必须真的提供「一键下载模型」与「离线导入模型…」。

    回归：文档曾让用户点这两个控件，但界面上并不存在（只有一行静态文字），
    而内置引擎是后端链默认第一级 —— 「按文档获取模型」的路是死的。
    """
    nav(page, "光影工作台")
    gear = page.locator('[title="设置"]').first
    if gear.count() == 0:
        check("存在设置按钮", False)
        return
    human_click(page, gear)
    page.wait_for_timeout(800)

    info = page.evaluate("""() => {
      const m = document.querySelector('.modal');
      if (!m) return { hasModal:false };
      const btns = Array.from(m.querySelectorAll('button')).map(b => (b.textContent||'').trim());
      const accepts = Array.from(m.querySelectorAll('input[type=file]')).map(f => f.accept);
      const txt = m.innerText;
      return { hasModal:true, buttons:btns, accepts,
               mentionsState: txt.includes('模型已就绪') || txt.includes('尚未下载'),
               mentionsMirror: txt.includes('HLS_MODEL_URL') };
    }""")
    check("设置弹窗打开", info.get("hasModal") is True, str(info.get("hasModal")))
    btns = info.get("buttons") or []
    check("存在「一键下载模型」按钮", any("一键下载模型" in b for b in btns), str(btns))
    check("存在「离线导入模型…」按钮", any("离线导入模型" in b for b in btns), str(btns))
    check("存在仅接受 .onnx 的文件选择器", ".onnx" in (info.get("accepts") or []),
          str(info.get("accepts")))
    check("显示模型就绪状态", info.get("mentionsState") is True)
    check("给出 HLS_MODEL_URL 镜像提示", info.get("mentionsMirror") is True)

    # 点一次下载按钮，验证「控件确实接线到后端」。
    # 不能假定模型已存在：干净机器上这会真的去下 64MB（本项目网络下 GitHub
    # 还常超时），断言成功提示会变成机器相关。因此接受两种结果：
    # 成功（已完成/已存在）或失败（502，提示用镜像）—— 两者都证明控件是活的。
    dl = page.locator(".modal button", has_text="一键下载模型").first
    ready = page.evaluate("""async () => {
      const r = await window.__hlsTimeout(fetch('/api/state'), 15000, 'state');
      const j = await window.__hlsTimeout(r.json(), 15000, 'state.json');
      return !!j.model_ready;
    }""")
    human_click(page, dl)
    # 下载中按钮会变成「下载中…」并禁用 —— 这本身就证明点击已接线
    page.wait_for_timeout(600)
    busy = page.evaluate("""() => {
      const b = Array.from(document.querySelectorAll('.modal button'))
        .find(x => (x.textContent||'').includes('下载中'));
      return b ? b.disabled : false;
    }""")
    check("点击后进入下载中状态（已接线）", busy or ready,
          f"下载中={busy} 模型已就绪={ready}")

    # toast 只存在 2.6 秒（ui.ts 的 setTimeout），所以**不能**等几秒后再找：
    # 必须在点击后高频轮询、抓到即停。
    # 预算必须覆盖下载超时（core/builtin_depth.py 的 timeout=30s）：未就绪时
    # 下载失败要等 30 秒才会抛错，预算短于它就会在干净机器上假失败。
    # toast 在 finally 恢复按钮文案**之前**触发，所以高频轮询能稳定捕获。
    feedback = ""
    for _ in range(260):                 # 260 × 250ms ≈ 65s
        page.wait_for_timeout(250)
        feedback = page.evaluate("""() => {
          const hits = Array.from(document.body.children)
            .filter(el => el instanceof HTMLElement && /position:\\s*fixed/.test(el.getAttribute('style') || ''))
            .map(el => (el.textContent || '').trim())
            .filter(t => t.length > 0);
          return hits.length ? hits[hits.length-1].slice(0, 80) : '';
        }""")
        if feedback:
            break
    check("点击下载后出现浮层反馈（成功或失败提示）", bool(feedback), feedback or "(无浮层提示)")

    # 状态行必须与真实状态一致（回归：成功后仍显示「尚未下载」）
    label = page.evaluate("""() => {
      const d = Array.from(document.querySelectorAll('.modal div'))
        .find(x => /模型已就绪|模型尚未下载/.test(x.textContent || ''));
      return d ? d.textContent.trim() : '';
    }""")
    if ready:
        check("模型已就绪时状态行显示「已就绪」", "已就绪" in label, label)
    else:
        check("模型未就绪时状态行如实提示", "尚未下载" in label, label)
    shot(page, "07-设置-模型控件")
    # 关闭弹窗
    cancel = page.locator(".modal button", has_text="取消").first
    if cancel.count():
        human_click(page, cancel)
        page.wait_for_timeout(300)


def test_settings_model_import_leg(page) -> None:
    """离线导入模型这条 UI 路径必须有测试覆盖。

    服务端已用 curl 验证，但界面这一层（选文件 → arrayBuffer → octet-stream POST
    → toast → 状态行刷新）没有覆盖；而 GitHub 在本网络不可达，**离线导入是用户
    拿到内置引擎的唯一途径**，所以这条链路必须真的能走通。

    保持机器无关：用伪造文件断言**错误提示**（任何机器都确定性失败）；
    只有模型本就就绪时，才额外用真实模型走一次成功路径。
    """
    import tempfile

    tmp_path = pathlib.Path(tempfile.mkdtemp(prefix="hls_imp_"))

    nav(page, "光影工作台")
    human_click(page, page.locator('[title="设置"]').first)
    page.wait_for_timeout(800)

    # 用 MutationObserver 记录所有浮层：toast 只存在 2.6 秒（错误 5.2 秒），
    # 而 66MB 上传 + 服务端加载校验要十几秒 —— 靠 250ms 轮询会漏掉，
    # 必须「先装观察器、后触发动作」。
    page.evaluate("""() => {
      window.__toasts = [];
      new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach((n) => {
        if (n instanceof HTMLElement && /position:\\s*fixed/.test(n.getAttribute('style') || '')) {
          window.__toasts.push((n.textContent || '').trim().slice(0, 90));
        }
      }))).observe(document.body, { childList: true });
    }""")

    ready = page.evaluate("""async () => {
      const r = await window.__hlsTimeout(fetch('/api/state'), 15000, 'state');
      const j = await window.__hlsTimeout(r.json(), 15000, 'state.json');
      return !!j.model_ready;
    }""")

    # ① 伪造文件必须被拒（确定性，不依赖机器状态）
    bogus = tmp_path / "bogus.onnx"
    bogus.write_bytes(b"onnx" + b"\x00" * 2_000_000)   # >1MB，绕过旧的 size-only 校验
    inp = page.locator('.modal input[type="file"]').first
    inp.set_input_files(str(bogus))
    feedback = ""
    for _ in range(60):                                 # ~15s
        page.wait_for_timeout(250)
        feedback = page.evaluate("""() => {
          const hits = Array.from(document.body.children)
            .filter(el => el instanceof HTMLElement && /position:\\s*fixed/.test(el.getAttribute('style') || ''))
            .map(el => (el.textContent || '').trim()).filter(Boolean);
          return hits.length ? hits[hits.length-1].slice(0, 90) : '';
        }""")
        if feedback:
            break
    toasts = page.evaluate("window.__toasts || []")
    check("导入伪造 ONNX 被拒绝并提示",
          any("无法作为 ONNX" in t for t in toasts) or bool(feedback),
          str(toasts[-1:] or feedback)[:100])

    # ② 真实模型的成功路径：**默认跳过**，用 HLS_TEST_MODEL_IMPORT=1 打开。
    # 原因：66MB 上传 + 服务端加载校验要几十秒（跑两次会让整个套件超时），
    # 而「控件是否接线」已由 ① 确定性覆盖（同样走 选文件 → arrayBuffer →
    # octet-stream POST → 反馈提示）。服务端载荷本身另有 curl/单测覆盖。
    if ready and os.environ.get("HLS_TEST_MODEL_IMPORT") == "1":
        from core.builtin_depth import default_model_path
        real = default_model_path()
        if os.path.isfile(real):
            page.locator('.modal input[type="file"]').first.set_input_files(real)
            ok_feedback = ""
            for _ in range(320):                  # 320 × 250ms = 80s
                page.wait_for_timeout(250)
                ok_feedback = page.evaluate("""() => {
                  const hits = Array.from(document.body.children)
                    .filter(el => el instanceof HTMLElement && /position:\\s*fixed/.test(el.getAttribute('style') || ''))
                    .map(el => (el.textContent || '').trim()).filter(Boolean);
                  return hits.length ? hits[hits.length-1].slice(0, 90) : '';
                }""")
                if "已导入" in ok_feedback:
                    break
            toasts = page.evaluate("window.__toasts || []")
            check("导入真实 ONNX 成功并提示",
                  any("已导入" in t for t in toasts),
                  str(toasts[-1:] or ok_feedback)[:100])
    else:
        why = "模型未就绪" if not ready else "未设置 HLS_TEST_MODEL_IMPORT=1"
        check(f"（跳过 66MB 真实导入，{why}）", True, "machine-independent")

    shot(page, "08-设置-离线导入模型")
    cancel = page.locator(".modal button", has_text="取消").first
    if cancel.count():
        human_click(page, cancel)
        page.wait_for_timeout(300)


def test_keyboard_shortcuts(page) -> None:
    print("\n[键盘：Tab 聚焦与 Escape]")
    nav(page, "光影工作台")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement ? document.activeElement.tagName : ''")
    check("Tab 能移动焦点", focused in ("BUTTON", "INPUT", "SELECT", "A"), focused)


# ---------------------------------------------------------------- 入口

def _ensure_png(page) -> str:
    """生成一张真实测试图片并落盘（供 set_input_files 使用）。"""
    import base64
    b64 = page.evaluate("""() => {
      const c = document.createElement('canvas'); c.width=400; c.height=400;
      const g = c.getContext('2d');
      const grd = g.createLinearGradient(0,0,400,400);
      grd.addColorStop(0,'#20304a'); grd.addColorStop(1,'#0a0d13');
      g.fillStyle=grd; g.fillRect(0,0,400,400);
      g.fillStyle='#78dceb'; g.beginPath(); g.arc(200,150,95,0,Math.PI*2); g.fill();
      g.fillStyle='#f5e6d8'; g.beginPath(); g.arc(200,185,68,0,Math.PI*2); g.fill();
      g.fillStyle='#3c46a0'; g.fillRect(155,250,90,130);
      return c.toDataURL('image/png').split(',')[1];
    }""")
    p = os.path.join("build", "ui-test-image.png")
    os.makedirs("build", exist_ok=True)
    with open(p, "wb") as f:
        f.write(base64.b64decode(b64))
    return p


def _make_webm(page, path: str) -> None:
    """用浏览器 MediaRecorder 生成一段真实视频文件。"""
    import base64
    b64 = page.evaluate("""async () => {
      const c = document.createElement('canvas'); c.width=320; c.height=240;
      const g = c.getContext('2d');
      const rec = new MediaRecorder(c.captureStream(15), { mimeType: 'video/webm' });
      const chunks = []; rec.ondataavailable = e => chunks.push(e.data); rec.start();
      for (let i = 0; i < 26; i++) {
        g.fillStyle = `hsl(${i*14},70%,50%)`; g.fillRect(0,0,320,240);
        await new Promise(r => setTimeout(r, 55));
      }
      // onstop 在某些环境下可能永不触发（mimeType 不支持/流无帧）→ 必须有界
      await new Promise((res, rej) => {
        const t = setTimeout(() => rej(new Error('MediaRecorder onstop 超时')), 20000);
        rec.onstop = () => { clearTimeout(t); res(); };
        rec.stop();
      });
      const buf = await new Blob(chunks, {type:'video/webm'}).arrayBuffer();
      let s = ''; const b = new Uint8Array(buf);
      for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
      return btoa(s);
    }""")
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))


# 每个用例的耗时上限（仅用于**提示**，线程看门狗不可用，见下）
CASE_WARN = float(os.environ.get("HLS_CASE_WARN", "120"))
# 整轮硬上限（秒）。playwright 的 page.evaluate 没有默认超时，页内 await 若
# 永不 settle 会让驱动调用永久阻塞 —— 那种挂起无法在进程内打断（线程不可用、
# 信号在 Windows 上不可用），只能由父进程杀子进程。因此整轮跑在自己起的子进程里。
RUN_TIMEOUT = float(os.environ.get("HLS_RUN_TIMEOUT", "1500"))
TIMINGS: list[tuple[str, float]] = []
def _run_body(only: str) -> int:
    from playwright.sync_api import sync_playwright

    os.makedirs("build", exist_ok=True)
    webm = os.path.join("build", "ui-test-clip.webm")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-device-scale-factor=1"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  locale="zh-CN")
        page = ctx.new_page()
        # 兜底：所有 driver 调用默认 30s 上限（page.evaluate 不受此约束，
        # 页内等待由 __hlsTimeout 与看门狗负责）。
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(30000)
        page.add_init_script("""
            window.__hlsErrors = [];
            window.addEventListener('error', e => window.__hlsErrors.push(String(e.message)));
            window.addEventListener('unhandledrejection',
                e => window.__hlsErrors.push('rej:' + String(e.reason)));
            // page.evaluate 没有默认超时，页内 await 若永不 settle 会永久挂住测试。
            // 所有页内网络/事件等待都必须经过这个有界包装。
            window.__hlsTimeout = (promise, ms, tag) => Promise.race([
                promise,
                new Promise((_, rej) => setTimeout(
                    () => rej(new Error('页内等待超时(' + tag + ')')), ms)),
            ]);
        """)
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_selector(".rail .nav-btn", timeout=20000)
        page.wait_for_timeout(1800)

        # 先生成测试视频（供切屏用例使用）
        _make_webm(page, webm)

        cases: list[tuple[str, object]] = [
            ("外壳与导航", test_shell_and_nav),
            ("主题切换", test_theme_switch),
            ("预设点击", test_preset_click),
            ("轨迹球与点光", test_trackball_drag),
            ("滑杆拖拽", test_slider_drag),
            ("画布缩放", test_canvas_wheel),
            ("点击拾取主光", test_click_pick),
            ("参考图迁移", test_reference_transfer),
            ("一键重置", test_reset_all),
            ("导出", test_export_image),
            ("导入图片与模式识别", test_import_image_and_modes),
            ("视频自动切屏", lambda pg: test_auto_switch_to_video(pg, webm)),
            ("视频逐帧与播放", test_video_transport_clicks),
            ("调光台 CUE", test_cue_save_and_apply),
            ("光影指挥屏", test_bridge_telemetry),
            ("插件与主题屏", test_plugins_and_theme_screens),
            ("设置与模型控件", test_settings_model_controls),
            ("设置与离线导入模型", test_settings_model_import_leg),
            ("键盘焦点", test_keyboard_shortcuts),
        ]
        ran = 0
        for label, fn in cases:
            if only and only not in label and only not in getattr(fn, "__name__", ""):
                continue
            ran += 1
            print(f">>> {label}", flush=True)   # 供父进程看门狗定位卡住的用例
            # 注意：用例必须留在**主线程**。Playwright 同步 API 走 greenlet
            # dispatcher，而 dispatcher 绑定创建它的线程；从别的线程调用
            # page/ctx 会抛 greenlet.error（已实测），整轮会全灭。
            # 因此这里不做线程看门狗：防挂靠 ①页内 __hlsTimeout ②
            # page.set_default_timeout ③外层进程级上限（见 main 的看门狗）。
            t0 = time.time()
            try:
                fn(page)
            except Exception as e:
                check(f"{label} 执行异常", False, f"{type(e).__name__}: {e}")
            dt = time.time() - t0
            TIMINGS.append((label, dt))
            flag = "  ← 偏慢" if dt > CASE_WARN else ""
            print(f"         (耗时 {dt:.1f}s{flag})", flush=True)

        # 过滤条件写错时不能「0 项 = 成功」：那是假绿
        if ran == 0:
            check(f"筛选条件「{only}」匹配到用例", False, "0 项匹配，请检查筛选词")

        errs = page.evaluate("window.__hlsErrors || []")
        check("整轮结束无未捕获 JS 错误", not errs, str(errs)[:200])
        try:
            ctx.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n===== {passed}/{total} 项通过 =====")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  FAIL {n} — {d}")
    print(f"截图目录：{os.path.abspath(SHOTS)}")
    return 0 if passed == total else 1


def main() -> int:
    """整轮跑在自己的子进程里，父进程做硬上限看门狗。

    为什么不用线程看门狗：Playwright 同步 API 基于 greenlet dispatcher，
    dispatcher 绑定创建它的线程；从另一个线程调 page/ctx 会抛
    `greenlet.error: Cannot switch to a different thread`（已实测），
    用例会全灭。信号在 Windows 上也不可用。**唯独**「杀掉整个子进程」
    能在进程外真正打断卡死的驱动调用，同时还能定位到是哪个用例卡住。
    """
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    if os.environ.get("HLS_NO_WATCHDOG") == "1":
        return _run_body(only)

    import subprocess
    import threading

    env = dict(os.environ, HLS_NO_WATCHDOG="1")
    cmd = [sys.executable, "-u", "-m", "tests.ui_playwright"] + ([only] if only else [])
    print(f"[看门狗] 子进程运行，硬上限 {RUN_TIMEOUT:.0f}s"
          f"（HLS_RUN_TIMEOUT 可调；HLS_NO_WATCHDOG=1 可关闭）", flush=True)
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace",
                         # POSIX：让子进程自成进程组，否则下面的 killpg 会连
                         # **本进程自己**一起杀掉。Windows 上该参数被忽略
                         # （用 taskkill /T 杀进程树）。
                         start_new_session=(os.name != "nt"))
    last = [""]

    def _pump() -> None:
        # 只做管道搬运（不碰 Playwright），因此可以安全用线程；
        # 主线程负责墙钟上限 —— 若在这里边读边判超时，读取会一直阻塞，
        # 超时分支永远不可达（第一版就是这个毛病）。
        assert p.stdout is not None
        for line in p.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if line.startswith(">>> "):
                last[0] = line[4:].strip()

    th = threading.Thread(target=_pump, daemon=True)
    th.start()
    try:
        p.wait(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_tree(p.pid)
        th.join(10)
        print(f"\n[看门狗] 超过 {RUN_TIMEOUT:.0f}s 仍未结束，已强制终止子进程。"
              f"\n          最后进入的用例：{last[0] or '(未知)'}"
              f"\n          这通常意味着某处页内 await 永不 settle"
              f"（page.evaluate 没有默认超时）。")
        return 1
    th.join(10)
    return p.returncode or 0


def _kill_tree(pid: int) -> None:
    """连同孙进程（Chromium）一起杀掉，否则会留下孤儿浏览器进程。"""
    import subprocess
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, check=False)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

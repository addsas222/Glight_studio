/**
 * 前端入口：注册全部屏幕后启动外壳。
 *
 * 屏幕模块各自导出 createXxxScreen(): Screen，这里集中注册，
 * 导航顺序与 Pen 设计稿一致（A 组为主，B 组独有部分追加在后）。
 */
import "./styles.css";
import { registerScreen, startApp } from "./shell";

import { createWorkbenchScreen } from "./screens/workbench";
import { createAutoLightScreen } from "./screens/autolight";
import { createProModeScreen } from "./screens/promode";
import { createVideoScreen } from "./screens/video";
import { createPluginsScreen } from "./screens/plugins";
import { createThemeScreen } from "./screens/theme";
import { createStarMapScreen } from "./screens/starmap";
import { createHarvestScreen } from "./screens/harvest";
import { createFramePaintScreen } from "./screens/framepaint";
import { createPrevisScreen } from "./screens/previs";
import { createTerminalScreen } from "./screens/terminal";
import { createDeskScreen } from "./screens/desk";
import { createBridgeScreen } from "./screens/bridge";

registerScreen(createWorkbenchScreen());
registerScreen(createAutoLightScreen());
registerScreen(createProModeScreen());
registerScreen(createVideoScreen());
registerScreen(createFramePaintScreen());
registerScreen(createStarMapScreen());
registerScreen(createHarvestScreen());
registerScreen(createPrevisScreen());
registerScreen(createTerminalScreen());
registerScreen(createDeskScreen());
registerScreen(createBridgeScreen());
registerScreen(createPluginsScreen());
registerScreen(createThemeScreen());

startApp().catch((e) => {
  const app = document.getElementById("app");
  if (app) {
    app.textContent = "";
    const box = document.createElement("div");
    box.style.padding = "24px";
    box.textContent = `界面启动失败：${e instanceof Error ? e.message : String(e)}`;
    app.append(box);
  }
});

# narciFarm

一台 PC 管理 N 台安卓设备的开源设备农场：多路投屏、群控广播、场景编排、定时调度、批量操作。
面向**自有设备 + 自有/授权应用**的测试与自动化场景（真机回归、兼容性测试、批量安装与巡检等）。

📖 **完整使用手册：[docs/使用手册.md](docs/使用手册.md)**（安装启动 / 设备接入 / 群控与录制 / 场景步骤参考 / FAQ / API 速查）

> ⚠️ 本项目定位是设备农场管理工具，**不包含**也不接受任何平台定向脚本、账号批量注册、
> 流量造假或反检测/设备指纹伪装功能。请仅将本工具用于你拥有或获得授权的设备与应用。

## 架构

```
浏览器 (static/，无构建依赖)
 ├─ 设备总览   快照轮询 /api/devices/{serial}/screenshot.png
 ├─ 实时投屏   WS /ws/mirror/{serial}  ← H.264(AVCC) → WebCodecs 硬解 → Canvas
 └─ 交互输入   归一化坐标 JSON → 服务端按各设备分辨率换算
                ├─ 主控设备: scrcpy 控制通道注入（低延迟）
                └─ 其他设备: adb shell input（群控广播）
FastAPI 后端 (narci/)
 ├─ adb 控制面   发现/注册表(SQLite)/分组/批量/控制台/场景执行
 ├─ scrcpy 客户端 scrcpy-server v5.0.1 推送 + 隧道 + 流解析 + 注入
 ├─ 场景引擎     步骤编排(loop 5 层嵌套) + 多设备并行 + 可中断
 └─ 调度器       interval(跑完等 N 秒) / daily(每天 HH:MM)
```

## 功能对照（真开源项目功能并集）

| 功能 | 来源项目 |
|---|---|
| scrcpy 协议客户端（H.264 流 + 注入 + 剪贴板） | scrcpy |
| 多设备同屏、群控 MASTER 广播（点击/滑动/按键/文本同步到多台，坐标自动换算） | QtScrcpy |
| 设备注册表、状态心跳、分组管理、Web 管理台 | DeviceFarmer/STF |
| 设备自动发现（USB/`adb devices` + 局域网 5555 端口扫描连接）、断线状态跟踪 | adb-device-farm |
| 动作录制 → 步骤、场景 JSON 导入导出、N 秒间隔调度 | Evelin |
| 视觉节点自动化的可维护替代：`tap_text`（uiautomator dump 按文本点击） | uiautomator2/Airtest 思路 |
| stay awake 停留唤醒、批量截图/安装/推送/Shell | adb-device-farm / Evelin |
| 快照轮询降级模式（WebCodecs 不可用时自动切换） | ws-scrcpy 思路 |

## 快速开始

```bat
:: 1) 依赖（本机已验证: Python 3.11 via py 启动器, adb 1.0.41）
py -3 -m pip install -r requirements.txt

:: 2) scrcpy-server（已在 assets/ 内；缺失时重新下载）
py -3 scripts\fetch_scrcpy_server.py

:: 3) 启动（或直接双击 run.bat）
py -3 -m uvicorn narci.main:app --host 127.0.0.1 --port 8555

:: 4) 打开 http://127.0.0.1:8555
```

手机开启 USB 调试插入即可出现在列表；WiFi 设备可 `adb connect ip:5555` 或用「扫描局域网」。

## 构建与发布（打 tag 自动云端编译）

推送到 GitHub 后，打 tag 即触发 [.github/workflows/release.yml](.github/workflows/release.yml)：

```bash
git tag v0.1.0
git push origin main v0.1.0
```

两个并行 Job：

| Job | Runner | 产物 | 说明 |
|---|---|---|---|
| `build-pc-exe` | windows-latest | `narciFarm-windows-x64.exe` | PyInstaller 单文件，内置 adb + scrcpy-server + 前端；打包后自动冒烟测试（启动 exe、探测 `/api/settings`） |
| `build-android-server` | ubuntu-latest | `scrcpy-server-5.0.1` | 从 [scrcpy v5.0.1](https://github.com/Genymobile/scrcpy) 源码无 Gradle 构建（JDK17 + android-36 + build-tools 36.0.0），与官方预编译 jar 同版本可互换 |

两个产物自动挂载到该 tag 的 GitHub Release；手动触发（workflow_dispatch）只产出 Actions Artifact 不发 Release。

**本机打包**：

```bash
:: 可选：把 adb 三件套放进 build_stage/adb/ 一起打包（否则运行时用系统 PATH 的 adb）
py -3 -m PyInstaller narciFarm.spec --noconfirm   # 产物 dist/narciFarm.exe
dist\narciFarm.exe [--port 8555] [--no-browser]
```

exe 的可写数据（数据库/截图/上传缓存）在 exe 同级目录的 `data/`、`captures/`。


## 使用要点

- **选中目标**：左侧勾选设备；分组可批量管理勾选集合。
- **投屏**：总览页点击快照打开投屏（WebCodecs 解码，主控走 scrcpy 控制通道注入）。
- **群控广播**：投屏窗口勾选「广播到选中设备」后，点击/滑动/按键/文本同步到所有勾选设备。
- **录制**：投屏窗口勾选「录制动作」，操作将作为步骤追加到场景编辑器，保存即可复放。
- **场景**：支持 tap/swipe/text/key/back/home/launch/stop_app/shell/wait/screenshot/tap_text/loop（5 层嵌套），
  坐标用 `x_pct/y_pct`（0~1 归一化）则与分辨率无关。
- **调度**：interval = 每次跑完等 N 秒再触发（Evelin 的 N-th-run 语义）；daily = 每天 HH:MM。
- **快照降级**：浏览器不支持 WebCodecs 或投屏失败时自动切换 1.5s 快照轮询，功能不受影响。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NARCI_ADB` | PATH 中的 adb / `C:\tools\platform-tools\adb.exe` | adb 路径 |
| `NARCI_PORT` | 8555 | Web 端口 |
| `NARCI_MAX_SIZE` | 1280 | 投屏长边上限（带宽权衡） |
| `NARCI_BITRATE` / `NARCI_FPS` | 8Mbps / 30 | 投屏码率/帧率 |
| `NARCI_SERVER_VERSION` | 5.0.1 | scrcpy-server 版本（协议严格匹配） |
| `NARCI_POSTHOG_KEY` | 空（不启用） | PostHog 项目 key；配置后顶栏出现「使用分析」勾选框，**用户勾选后才**录制会话回放 |
| `NARCI_POSTHOG_HOST` | `https://us.i.posthog.com` | PostHog 上报域名（EU Cloud 填 `https://eu.i.posthog.com`） |

## 测试与已知边界

- 单元测试：`py -3 -m pytest tests/ -q`（协议编解码逐字节对照 scrcpy v5.0.1 源码，19 项）。
- 冒烟测试：HTTP 全路由、WS 事件流、分组/场景/调度 CRUD、非法输入 400 均已验证。
- **未验证**：真机端到端投屏与注入（当前无设备连接）。首次接真机时如遇问题，优先检查：
  1. `adb devices` 中设备为 `device` 状态（非 unauthorized）；
  2. 部分厂商需关闭「USB 安卓隐私保护/安全输入」才允许注入；
  3. scrcpy-server 版本必须为 5.0.1（协议随版本变化，见 `refs/scrcpy/doc/develop.md`）。

## 第三方组件与许可

| 组件 | 许可证 | 用途 |
|---|---|---|
| [scrcpy-server v5.0.1](https://github.com/Genymobile/scrcpy) (assets/) | Apache-2.0 | 设备端投屏服务（Genymobile ©） |
| [FastAPI](https://github.com/fastapi/fastapi) / uvicorn | MIT / BSD-3 | Web 框架 |
| [posthog-js](https://github.com/PostHog/posthog-js) 1.438.24（`static/vendor/posthog-js/`） | Apache-2.0 AND MIT | 会话回放 / 使用分析（默认关闭，需用户同意；输入框与日志文本遮罩，投屏画面不录制）
  更新：`npm install posthog-js@<版本> && npm run vendor:posthog` |
| 协议实现参考 | — | `refs/scrcpy/` 为参考用浅克隆（已 gitignore，勿随项目分发） |

narciFarm 自身代码采用 Apache-2.0。

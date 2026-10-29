# narciFarm 设备托管 DPC

narciFarm 的「设备托管（Device Owner）」功能需要设备上有一个 **DPC（设备策略控制器）应用**。
本目录提供一份最小可用的参考 DPC，实现 narciFarm 的广播契约。

> **状态说明**：本 DPC 源码未在本环境编译验证（本地没有 Android SDK）。
> 代码按标准 Android DevicePolicyManager API 编写，但请自行构建并测试。
> 若不想自建，也可以改用 Google 的 **TestDPC**（见下文）。

## 构建

```bash
# 需要 Android SDK + JDK 17
cd dpc/narci_dpc
./gradlew assembleDebug        # 或 Windows: gradlew.bat assembleDebug
# 产物：app/build/outputs/apk/debug/app-debug.apk
```

也可以直接用 Android Studio 打开 `dpc/narci_dpc` 目录构建。

## 设为 Device Owner

设备需满足：**已恢复出厂、未登录任何账号、未设置过 Device Owner**。

```bash
adb install app-debug.apk

# component = 包名/DeviceAdminReceiver 全限定名
adb shell dpm set-device-owner io.narcifarm.dpc/.NarciDeviceAdminReceiver

# 验证
adb shell dumpsys device_policy | head -20
```

设置成功后，narciFarm 的「批量操作 → 设备托管」面板会显示 `已托管：io.narcifarm.dpc`，
即可下发 Kiosk 锁定、禁用相机、禁止恢复出厂、远程锁屏/擦除等命令。

也可用 narciFarm 面板自动完成这一步（填入组件名 → 点「设置」）。

## 广播契约

narciFarm 通过 `io.narcifarm.POLICY` 广播下发命令，DPC 监听该 action：

| command | 参数 | 行为 |
|---|---|---|
| `lock_now` | — | 远程锁屏 |
| `wipe` | — | 远程擦除（不可恢复） |
| `set_kiosk` | `package` | 锁定为单应用 Kiosk |
| `clear_kiosk` | — | 解除 Kiosk |
| `suspend_app` / `unsuspend_app` | `package` | 停用 / 启用应用 |
| `set_factory_reset_allowed` | `allowed` (bool) | 允许 / 禁止恢复出厂 |
| `set_camera_disabled` | `disabled` (bool) | 禁用 / 启用相机 |
| `set_screen_capture_disabled` | `disabled` (bool) | 禁用 / 启用截屏 |
| `set_wifi` | `ssid`, `password`?, `hidden` (bool) | 配置 Wi-Fi（免用户确认） |
| `set_always_on_vpn` | `package`（空=关闭） | 设置始终开启的 VPN |
| `install_ca_cert` | `path`, `alias`? | 安装 CA 证书（path 为设备上的证书文件） |
| `clear_ca_certs` | `path` | 卸载指定 CA 证书（需同一证书文件） |

参数通过 `--es`（字符串）、`--ez`（布尔）与 `--ei`（整数）传递。

## 不想自建？用 TestDPC

Google 提供的 [TestDPC](https://github.com/googlesamples/android-testdpc) 也能作为 Device Owner：

```bash
adb install TestDPC.apk
adb shell dpm set-device-owner com.afwsamples.testdpc/.DeviceAdminReceiver
```

但 TestDPC 使用自己的广播协议，**narciFarm 的策略命令面板不能直接驱动它**——
此时 narciFarm 的「读取托管状态」仍然可用（能识别出 Device Owner 已设置）。

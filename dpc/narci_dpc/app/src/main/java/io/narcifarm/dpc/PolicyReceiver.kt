package io.narcifarm.dpc

import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiConfiguration
import android.net.wifi.WifiManager
import android.os.UserManager
import android.util.Log
import android.net.wifi.WifiConfiguration.KeyMgmt

/**
 * 策略命令接收器：实现 narciFarm 的广播契约（io.narcifarm.POLICY）。
 *
 * 命令与参数：
 *   lock_now                        -> 无参数
 *   wipe                            -> 无参数（远程擦除，不可恢复）
 *   set_kiosk                       -> package（锁定为单应用 Kiosk）
 *   clear_kiosk                     -> 无参数
 *   suspend_app / unsuspend_app     -> package
 *   set_factory_reset_allowed       -> allowed(true/false)
 *   set_camera_disabled             -> disabled(true/false)
 *   set_screen_capture_disabled     -> disabled(true/false)
 *
 * 这些接口大多要求本应用是 Device Owner；若权限不足会在 logcat 抛 SecurityException，
 * 由 narciFarm 侧记录为设备失败。
 */
class PolicyReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val dpm = context.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
        val admin = ComponentName(context, NarciDeviceAdminReceiver::class.java)
        val command = intent.getStringExtra("command") ?: run {
            Log.w(TAG, "缺少 command extra")
            return
        }
        Log.i(TAG, "收到策略命令: $command extras=${intent.extras}")

        try {
            when (command) {
                "lock_now" -> dpm.lockNow()

                "wipe" -> dpm.wipeData(DevicePolicyManager.WIPE_EXTERNAL_STORAGE)

                "set_kiosk" -> {
                    val pkg = intent.getStringExtra("package") ?: return
                    dpm.setLockTaskPackages(admin, arrayOf(pkg))
                }

                "clear_kiosk" -> dpm.setLockTaskPackages(admin, emptyArray())

                "suspend_app" -> {
                    val pkg = intent.getStringExtra("package") ?: return
                    dpm.setPackagesSuspended(admin, arrayOf(pkg), true)
                }

                "unsuspend_app" -> {
                    val pkg = intent.getStringExtra("package") ?: return
                    dpm.setPackagesSuspended(admin, arrayOf(pkg), false)
                }

                "set_factory_reset_allowed" -> {
                    val allowed = intent.getBooleanExtra("allowed", true)
                    if (allowed) {
                        dpm.clearUserRestriction(admin, UserManager.DISALLOW_FACTORY_RESET)
                    } else {
                        dpm.addUserRestriction(admin, UserManager.DISALLOW_FACTORY_RESET)
                    }
                }

                "set_camera_disabled" -> {
                    val disabled = intent.getBooleanExtra("disabled", true)
                    dpm.setCameraDisabled(admin, disabled)
                }

                "set_screen_capture_disabled" -> {
                    val disabled = intent.getBooleanExtra("disabled", true)
                    dpm.setScreenCaptureDisabled(admin, disabled)
                }

                "set_wifi" -> {
                    // Device Owner 可免用户确认地配置 Wi-Fi
                    val ssid = intent.getStringExtra("ssid") ?: return
                    val pwd = intent.getStringExtra("password")
                    val hidden = intent.getBooleanExtra("hidden", false)
                    val wm = context.getSystemService(Context.WIFI_SERVICE) as WifiManager
                    val cfg = WifiConfiguration().apply {
                        this.SSID = "\"$ssid\""
                        this.hiddenSSID = hidden
                        if (pwd.isNullOrEmpty()) {
                            allowedKeyManagement.set(KeyMgmt.NONE)
                        } else {
                            allowedKeyManagement.set(KeyMgmt.WPA_PSK)
                            preSharedKey = "\"$pwd\""
                        }
                    }
                    val netId = wm.addNetwork(cfg)
                    if (netId < 0) throw SecurityException("addNetwork 失败")
                    wm.disconnect()
                    wm.enableNetwork(netId, true)
                    wm.reconnect()
                }

                "set_always_on_vpn" -> {
                    val pkg = intent.getStringExtra("package") ?: ""
                    if (pkg.isEmpty()) {
                        dpm.setAlwaysOnVpnPackage(admin, null, false)
                    } else {
                        dpm.setAlwaysOnVpnPackage(admin, pkg, true)
                    }
                }

                "install_ca_cert" -> {
                    val path = intent.getStringExtra("path") ?: return
                    val pem = java.io.File(path).readBytes()
                    // PEM -> DER（DevicePolicyManager 只接受 DER 编码的 X.509）
                    val der = pemToDer(pem)
                    dpm.installCaCert(admin, der)
                }

                "clear_ca_certs" -> {
                    // uninstallCaCert 接受证书的 DER 字节，不接受别名
                    val path = intent.getStringExtra("path") ?: return
                    val pem = java.io.File(path).readBytes()
                    dpm.uninstallCaCert(admin, pemToDer(pem))
                }

                else -> Log.w(TAG, "未知命令: $command")
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "权限不足（需要 Device Owner）: $command", e)
        }
    }

    /** PEM 证书 -> DER 字节（去掉首尾标记与换行后 Base64 解码）。 */
    private fun pemToDer(pem: ByteArray): ByteArray {
        val text = String(pem, Charsets.UTF_8)
        val b64 = text
            .replace("-----BEGIN CERTIFICATE-----", "")
            .replace("-----END CERTIFICATE-----", "")
            .replace("\\s".toRegex(), "")
        return android.util.Base64.decode(b64, android.util.Base64.DEFAULT)
    }

    companion object {
        private const val TAG = "narciDPC"
    }
}

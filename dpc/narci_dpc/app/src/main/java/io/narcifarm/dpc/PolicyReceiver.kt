package io.narcifarm.dpc

import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.UserManager
import android.util.Log

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

                else -> Log.w(TAG, "未知命令: $command")
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "权限不足（需要 Device Owner）: $command", e)
        }
    }

    companion object {
        private const val TAG = "narciDPC"
    }
}

package io.narcifarm.dpc

import android.app.admin.DeviceAdminReceiver
import android.content.Context
import android.content.Intent
import android.widget.Toast

/**
 * Device Owner 接收器。
 *
 * 设为 Device Owner 后，narciFarm 通过广播下发策略命令（见 PolicyReceiver）。
 * 本类只负责被系统识别为设备管理员，不承载业务逻辑。
 */
class NarciDeviceAdminReceiver : DeviceAdminReceiver() {

    override fun onEnabled(context: Context, intent: Intent) {
        Toast.makeText(context, "narciFarm 设备托管已启用", Toast.LENGTH_SHORT).show()
    }

    override fun onDisabled(context: Context, intent: Intent) {
        Toast.makeText(context, "narciFarm 设备托管已停用", Toast.LENGTH_SHORT).show()
    }
}

"""Windows Toast 通知：纯标准库，经 PowerShell 的 WinRT ToastNotificationManager。

仅用于「后台驻留」模式下窗口不在时的系统级提醒（用户已知晓 V1 提醒能力
以应用运行为前提）。能力探测只检查 PowerShell 是否存在；实际弹送失败静默
降级（提醒状态已在调度器里落库，打开窗口后会通过 /api/notify 补拉）。
"""
import base64
import os
import shutil
import subprocess
import threading

_lock = threading.Lock()
_has_ps = [None]   # None=未探测，True/False

_PS_TEMPLATE = (
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
    " ContentType = WindowsRuntime] | Out-Null\n"
    "[Windows.UI.Notifications.ToastNotificationManager,"
    " Windows.UI.Notifications.ToastNotificationManager,"
    " ContentType = WindowsRuntime] | Out-Null\n"
    "$xml = [Windows.Data.Xml.Dom.XmlDocument]::new()\n"
    "$xml.LoadXml('<toast scenario=\"reminder\"><visual>"
    "<binding template=\"ToastGeneric\">"
    "<text>%s</text><text>%s</text>"
    "</binding></visual></toast>')\n"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)\n"
    "[Windows.UI.Notifications.ToastNotificationManager]"
    "::CreateToastNotifier('Personal Workspace').Show($toast)\n"
)


def _esc(s):
    """XML 文本转义，另防 PowerShell 单引号串提前结束。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("'", "''"))


def available():
    """本机是否能尝试弹 Toast（有 powershell 即认为可用，失败静默）。"""
    if _has_ps[0] is None:
        with _lock:
            if _has_ps[0] is None:
                _has_ps[0] = bool(os.name == "nt" and shutil.which("powershell"))
    return _has_ps[0]


def fire(title, body=""):
    """弹一条系统 Toast。返回是否已投递给 PowerShell（不代表用户可见）。"""
    if not available():
        return False
    script = _PS_TEMPLATE % (_esc(title), _esc(body or " "))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    flags = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=25, creationflags=flags)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False

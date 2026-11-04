#!/usr/bin/env python3
"""开发入口：起网页界面并打开浏览器（打包成 exe 时也用它）。

    python run.py                 # 默认 http://127.0.0.1:8766
    python run.py --port 8899     # 换端口（被占用会自动往后找）
    python run.py --no-browser    # 只起服务
    python run.py --app           # 用独立客户端窗口打开（没有地址栏）

等价的命令行入口：
    python -m personal_workspace today | list | backup | stats
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "src"))

from personal_workspace import server  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = 8766
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                print("--port 需要数字，已改用默认端口 %d" % port)
    no_browser = "--no-browser" in argv
    app = "--app" in argv
    # 无控制台的客户端 exe 里 sys.stdout 是 None：默认直接用客户端窗口
    if (not app and not no_browser and getattr(sys, "frozen", False)
            and sys.stdout is None):
        app = True
    return server.serve(None, port, not no_browser, app_window=app)


if __name__ == "__main__":
    sys.exit(main())

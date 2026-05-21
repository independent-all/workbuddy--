"""
一键本地服务器启动脚本
─────────────────────
用法：
  python serve.py

然后浏览器打开 http://localhost:8080 即可查看战术指挥舱。
也可以直接双击 index.html 打开（无需服务器）。
"""

import http.server
import socket
import webbrowser
import os
import sys
from pathlib import Path

PORT = 8080

os.chdir(Path(__file__).parent)

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"  {args[0]}")

def main():
    print(f"\n  ╔══════════════════════════════════════╗")
    print(f"  ║   周末交友战术指挥舱 · 本地服务器    ║")
    print(f"  ╚══════════════════════════════════════╝")
    print(f"\n  🌐 地址: http://localhost:{PORT}")
    print(f"  📁 目录: {os.getcwd()}")
    print(f"  ⏹  按 Ctrl+C 停止服务器\n")

    try:
        with http.server.HTTPServer(("0.0.0.0", PORT), Handler) as httpd:
            webbrowser.open(f"http://localhost:{PORT}")
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  服务器已停止。\n")

if __name__ == "__main__":
    main()

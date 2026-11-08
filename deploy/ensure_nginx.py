"""开机或定时检查：开发站 Nginx 若丢掉了 /campus/ 转发，就补回去。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SNIPPET = Path("/opt/campus-knowledge-agent/deploy/nginx-campus.location")
TARGET = Path("/mydata/nginx/conf/lab-pages.conf")
MARKER = b"campus-knowledge-agent"


def main() -> int:
    if not SNIPPET.exists() or not TARGET.exists():
        print("nginx config or snippet missing, skip")
        return 0
    if subprocess.call(["docker", "inspect", "nginx"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        print("nginx container not ready, skip")
        return 0
    text = TARGET.read_bytes().replace(b"\r\n", b"\n")
    if MARKER not in text:
        snippet = SNIPPET.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if not text.endswith(b"\n"):
            text += b"\n"
        TARGET.write_bytes(text + b"\n" + snippet)
        print("restored campus proxy")
    check = subprocess.call(["docker", "exec", "nginx", "nginx", "-t"])
    if check != 0:
        print("nginx config test failed", file=sys.stderr)
        return check
    subprocess.check_call(["docker", "exec", "nginx", "nginx", "-s", "reload"])
    print("nginx proxy ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

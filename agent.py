"""narciFarm Agent 入口: 本机设备接入中心服务。

用法:
    py -3 agent.py --center http://中心IP:8555 [--port 8600] [--name 机房A]
环境变量: NARCI_CENTER_URL / NARCI_AGENT_PORT / NARCI_AGENT_NAME / NARCI_AGENT_SECRET
"""
import argparse
import os

import uvicorn

from narci.agent_app import BIND, PORT, get_app


def main() -> None:
    parser = argparse.ArgumentParser(description="narciFarm Agent")
    parser.add_argument("--center", default=os.environ.get("NARCI_CENTER_URL", "http://127.0.0.1:8555"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NARCI_AGENT_PORT", "8600")))
    parser.add_argument("--name", default=os.environ.get("NARCI_AGENT_NAME", ""))
    parser.add_argument("--secret", default=os.environ.get("NARCI_AGENT_SECRET", ""))
    args = parser.parse_args()

    os.environ["NARCI_CENTER_URL"] = args.center
    if args.name:
        os.environ["NARCI_AGENT_NAME"] = args.name
    if args.secret:
        os.environ["NARCI_AGENT_SECRET"] = args.secret

    print(f"narciFarm Agent 启动: 中心={args.center} 端口={args.port}", flush=True)
    uvicorn.run(get_app(), host=BIND, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

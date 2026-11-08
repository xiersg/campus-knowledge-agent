"""命令行对话，方便不打开浏览器时试用。"""

from __future__ import annotations

import argparse

from campus_agent.agent import Agent
from campus_agent.mcp_client import McpClient
from campus_agent.tools import seed_if_empty

DEMOS = [
    "明天有什么课？课室和老师分别是谁？",
    "软件工程的教学内容是什么？",
    "最近有哪些考试需要提醒？",
    "帮我复盘一下操作系统上次课",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="校园知识咨询命令行")
    parser.add_argument("--demo", action="store_true", help="连续提问四个示例问题")
    args = parser.parse_args()
    seed_if_empty()
    client = McpClient()
    agent = Agent(client)
    try:
        if args.demo:
            for question in DEMOS:
                print(f"\n你：{question}")
                print(agent.chat("demo", question)["reply"])
            return
        print("校园知识咨询。直接输入问题，空行退出。")
        while True:
            text = input("\n你：").strip()
            if not text:
                break
            print(agent.chat("cli", text)["reply"])
    finally:
        client.close()


if __name__ == "__main__":
    main()

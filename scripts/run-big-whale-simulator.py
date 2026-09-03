#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行模拟手机大鲸：领取开场、回复消息或检查服务端记忆。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pet.memory_protocol import MemorySyncClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="大鲸接收端模拟器")
    parser.add_argument("action", choices=("claim", "reply", "stream"), nargs="?", default="claim")
    parser.add_argument("text", nargs="?")
    parser.add_argument("--url", default="http://127.0.0.1:47821")
    parser.add_argument("--token", default=os.environ.get("WHALE_MEMORY_TOKEN", "local-dev-token"))
    parser.add_argument("--user", default="local-user")
    parser.add_argument("--device", default="big-whale-simulator")
    parser.add_argument("--after", type=int, default=0)
    args = parser.parse_args()
    client = MemorySyncClient(args.url, args.token)

    if args.action == "stream":
        print(json.dumps(client.stream(args.user, args.after), ensure_ascii=False, indent=2))
        return 0
    if args.action == "reply":
        if not args.text:
            parser.error("reply 需要一段回复文字")
        response = client.post_message(
            user_id=args.user,
            device_id=args.device,
            message_id=f"user_{uuid.uuid4().hex}",
            role="user",
            text=args.text,
        )
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0

    response = client.claim_opening(
        user_id=args.user,
        device_id=args.device,
        claim_id=f"claim_{uuid.uuid4().hex}",
    )
    if response.get("shouldSend"):
        print(f"大鲸：{response['text']}")
    elif response.get("reason") == "awaiting_user_reply":
        print("大鲸没有重复发消息：上一条开场还在等你回复。")
    else:
        print("大鲸暂时没有新的记忆可以开场。")
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

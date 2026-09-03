#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动本机共享记忆服务模拟器。"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pet.chat.models import ProviderConfig  # noqa: E402
from pet.companion_llm import ModelCompanionResponder  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.memory_server import MemoryApiServer, MemoryRepository  # noqa: E402


def configured_responder() -> ModelCompanionResponder | None:
    config = Config()
    settings = config.chat_settings()
    if not settings.enabled:
        return None
    current = settings.active_config
    provider = ProviderConfig.from_dict(
        current.provider_id,
        current.to_dict(include_secret=True),
    )
    provider.api_key = config.resolve_api_key(current)
    if "WHALE_LLM_BASE_URL" in os.environ:
        provider.base_url = os.environ["WHALE_LLM_BASE_URL"].strip()
    if "WHALE_LLM_CHAT_PATH" in os.environ:
        provider.chat_path = os.environ["WHALE_LLM_CHAT_PATH"].strip()
    if "WHALE_LLM_MODEL" in os.environ:
        provider.model = os.environ["WHALE_LLM_MODEL"].strip()
    if "WHALE_LLM_API_KEY" in os.environ:
        provider.api_key = os.environ["WHALE_LLM_API_KEY"].strip()
    responder = ModelCompanionResponder(provider)
    return responder if responder.available else None


def main() -> int:
    parser = argparse.ArgumentParser(description="小鲸/大鲸共享记忆服务（本机 MVP）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47821)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".dsh-whale-memory" / "memory.sqlite3",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("WHALE_MEMORY_TOKEN", "local-dev-token"),
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="不调用聊天模型，只使用严格事实型回复",
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("模拟服务只允许监听本机；远程部署请放在 HTTPS 反向代理后")
    if not args.token:
        parser.error("token 不能为空")

    responder = None if args.no_llm else configured_responder()
    server = MemoryApiServer(
        MemoryRepository(args.db, responder=responder),
        token=args.token,
        host=args.host,
        port=args.port,
    )
    port = server.start()
    print(f"共享记忆服务已启动：http://{args.host}:{port}")
    print(f"大鲸手机页：http://{args.host}:{port}/")
    if responder is None:
        print("大鲸对话模型：未配置，使用严格事实型回复")
    else:
        print(f"大鲸对话模型：{responder.name}")
    print(f"SQLite：{args.db.expanduser().resolve()}")
    print("按 Ctrl+C 停止。")
    stopped = False

    def stop(*_args) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopped:
            time.sleep(0.25)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

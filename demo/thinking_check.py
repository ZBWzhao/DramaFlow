"""诊断：推理模型的"思考内容"到底能不能拿到（对应前端思考块的唯一数据来源）。

为什么单独有这个脚本：前端思考块依赖 `thinking_update` 事件，而它又依赖
provider 在流里给出 `reasoning_content`。这一层不确认，"思考过程"就是空中楼阁 ——
之前正是**攒在内存里没往外发**，所以界面上永远看不到思考。

用法：
  python demo/thinking_check.py                    # 默认试 deepseek-reasoner
  python demo/thinking_check.py --model xxx        # 指定模型
  python demo/thinking_check.py --env              # 只用当前 LLM_MODEL，不覆盖
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    parser = argparse.ArgumentParser(description="检查推理内容是否可用")
    parser.add_argument("--model", default="deepseek-reasoner")
    parser.add_argument("--env", action="store_true", help="不覆盖模型，用当前环境")
    args = parser.parse_args()

    if not args.env:
        os.environ["LLM_MODEL"] = args.model

    from core.config import Settings  # noqa: E402
    from core.llm import LLM  # noqa: E402

    settings = Settings.load()
    print(f"base_url = {settings.base_url}")
    print(f"model    = {settings.model}")

    llm = LLM(settings)
    text_chunks: list[str] = []
    reason_chunks: list[str] = []

    try:
        message = await llm.chat(
            [{"role": "user", "content": "9.11 和 9.9 哪个大？只回答哪个更大"}],
            None,
            on_text=text_chunks.append,
            on_reasoning=reason_chunks.append,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 请求失败：{type(exc).__name__}: {exc}")
        print("  → 若提示模型不存在，说明这个 key/账号没有该模型权限")
        return

    print(f"\n正文（{len(message.content)} 字）：{message.content.strip()[:80]}")
    print(f"message.reasoning 长度：{len(message.reasoning)}")
    print(f"on_reasoning 回调收到：{len(''.join(reason_chunks))} 字")
    print(f"流式正文回调收到：{len(''.join(text_chunks))} 字")

    if message.reasoning:
        head = message.reasoning.strip().replace("\n", " ")[:140]
        print(f"\n✓ 该模型会输出推理内容，前端思考块可以显示：\n  {head}…")
        print("\n结论：core/agent.py 的 thinking_update 事件有数据可用。")
    else:
        print("\n✗ 这个模型/账号没有返回 reasoning_content。")
        print("  → 前端思考块不会出现（非推理模型本来就没有思考过程）。")
        print("  → 换推理模型：设置环境变量 LLM_MODEL=deepseek-reasoner 再启动服务。")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

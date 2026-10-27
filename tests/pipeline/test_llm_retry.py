"""A3：传输层瞬态错误退避重试。纯单元：LLM.__new__ 绕过 __init__，注假 client。"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import (APIConnectionError, AuthenticationError, InternalServerError,
                    RateLimitError)

from core.llm import LLM


# openai>=2 的 APIConnectionError 只收 kwargs（message=, request=），与 1.x 的位置参数不同
_CONN = httpx.Request("POST", "http://t")


def _http_err(exc_cls, status: int):
    return exc_cls(f"http-{status}",
                   response=httpx.Response(status, request=httpx.Request("POST", "http://t")),
                   body=None)


def _chunk(*, content=None):
    """chunk 形状对齐 openai 流式增量；content=None 的空 chunk 会被消费体跳过。"""
    if content is None:
        return SimpleNamespace(usage=None, choices=[])
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(usage=None,
                           choices=[SimpleNamespace(finish_reason=None, delta=delta)])


class FlakyClient:
    """create 前 n_fail 次抛 exc，之后按 payload 逐个吐 chunk。"""

    def __init__(self, exc, n_fail, payload=None):
        self.calls = 0
        self.exc = exc
        self.n_fail = n_fail
        self.payload = payload or [_chunk()]

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.n_fail:
            raise self.exc

        async def _stream():
            for item in self.payload:
                yield item
        return _stream()


def _llm(client) -> LLM:
    llm = LLM.__new__(LLM)
    llm.client = client
    llm.model = "test-model"
    llm.settings = SimpleNamespace(max_output_tokens=0)
    return llm


_real_sleep = asyncio.sleep  # monkeypatch 会替换 asyncio.sleep，先捕获原件避免自递归


def _zero_sleep(_delay):
    return _real_sleep(0)


@pytest.mark.parametrize("exc", [
    APIConnectionError(message="conn", request=_CONN),
    _http_err(RateLimitError, 429),
    _http_err(InternalServerError, 500),
])
def test_transient_errors_retry_then_succeed(exc, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
    client = FlakyClient(exc, 2)
    msg = asyncio.run(_llm(client).chat([{"role": "user", "content": "hi"}]))
    assert client.calls == 3


def test_auth_error_no_retry(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
    client = FlakyClient(_http_err(AuthenticationError, 401), 5)
    with pytest.raises(AuthenticationError):
        asyncio.run(_llm(client).chat([{"role": "user", "content": "hi"}]))
    assert client.calls == 1


def test_no_retry_after_partial_stream(monkeypatch):
    """已经 on_text 吐过字：断流不重试（重试会向用户重复输出）。"""
    monkeypatch.setattr(asyncio, "sleep", _zero_sleep)

    class MidStreamDrop(FlakyClient):
        def __init__(self):
            super().__init__(APIConnectionError(message="x", request=_CONN), 0)

        async def _create(self, **kwargs):
            self.calls += 1

            async def _stream():
                yield _chunk(content="你")
                raise APIConnectionError(message="mid-stream", request=_CONN)
            return _stream()

    client = MidStreamDrop()
    seen: list[str] = []
    with pytest.raises(APIConnectionError):
        asyncio.run(_llm(client).chat([{"role": "user", "content": "hi"}], on_text=seen.append))
    assert seen == ["你"] and client.calls == 1

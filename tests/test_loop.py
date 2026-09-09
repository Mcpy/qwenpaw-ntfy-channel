# -*- coding: utf-8 -*-
"""回环防护回归测试 v3(tag 方案):
- 断言1:经频道发出的消息(带 bot_tag)绝不被自己的 listener 入站
- 断言2:外部直发(无 tag,模拟手机)正常入站
- 断言3:应答(再次 send)零回流
泄漏判据:listener 收到「带 bot_tag 的 message」= 自己的消息漏拦。
不带 tag 的消息(如服务 agent 的回复)属于环境干扰,不计入泄漏。
"""
import asyncio
import sys

sys.path.insert(0, "plugins/ntfy-channel")

import httpx  # noqa: E402
from ntfy_channel import NtfyChannel  # noqa: E402

SERVER = "http://10.0.0.102:18888"
TOKEN = "tk_a48x70lp9pb6voli8b97t1y97clzp"
TAG = "qwenpaw-bot"


class FakeProcess:
    def __getattr__(self, name):
        return lambda *a, **k: None


async def main():
    received = []
    raw_events = []  # (event, tags, text)
    got = asyncio.Event()

    def enqueue(native):
        received.append(native["content_parts"][0].text)
        got.set()

    ch = NtfyChannel.from_config(
        process=FakeProcess(),
        config={
            "enabled": True,
            "server_url": SERVER,
            "token": TOKEN,
            "subscribe_topics": "test-topic",
            "push_topics": "test-topic",  # 与订阅相同 = 靠 tag 防回环的配置
            "enable_outbound": True,
        },
    )
    assert ch.bot_tag == TAG

    orig = ch._handle_stream_event

    def spy(obj):
        """记录「真正入站且带 tag」的消息 = 自己的消息漏拦。"""
        before = len(received)
        orig(obj)
        if (
            obj.get("event") == "message"
            and TAG in (obj.get("tags") or [])
            and len(received) > before
        ):
            raw_events.append((obj.get("message") or "")[:60])

    ch._handle_stream_event = spy
    ch.set_enqueue(enqueue)
    await ch.start()
    await asyncio.sleep(2)

    # ── 断言1:发 5 条(含长文分片),listener 不得真正入站任何带 tag 消息 ──
    await ch.send("test-topic", "自发声A-LOOPCHK")
    await ch.send("test-topic", "自发声B-LOOPCHK\n" + "长" * 3000)
    await ch.send("test-topic", "自发声C-LOOPCHK")
    await asyncio.sleep(20)
    print(f"[断言1] 带 tag 消息真正入站数 = {len(raw_events)} (期望 0)")
    assert not raw_events, f"回环!泄漏消息: {raw_events}"

    # ── 断言2:外部直发(无 tag)正常入站 ──
    received.clear()
    raw_events.clear()
    async with httpx.AsyncClient() as c:
        await c.put(
            f"{SERVER}/test-topic",
            content="外部手机消息(应入站)".encode(),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    try:
        await asyncio.wait_for(got.wait(), timeout=15)
    except asyncio.TimeoutError:
        pass
    ext = [t for t in received if "外部手机消息" in t]
    print(f"[断言2] 外部消息入站数 = {len(ext)} (期望 1)")
    assert len(ext) == 1, "外部消息被误滤!"

    # ── 断言3:应答(send)零回流 ──
    received.clear()
    raw_events.clear()
    await ch.send("test-topic", "这是对外部消息的应答-LOOPCHK")
    await asyncio.sleep(15)
    print(f"[断言3] 应答回流入站 = {len(raw_events)+len([t for t in received if '应答-LOOPCHK' in t])} (期望 0)")
    assert not raw_events and not [t for t in received if "应答-LOOPCHK" in t], "应答回流!"

    await ch.stop()
    print("✅ 3/3 全绿:tag 方案回环防护有效,外部消息不受影响")
    return 0


sys.exit(asyncio.run(main()))

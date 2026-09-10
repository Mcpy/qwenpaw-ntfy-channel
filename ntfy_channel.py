# -*- coding: utf-8 -*-
"""ntfy 频道插件。

入站:订阅 ntfy JSON 流(GET /{topics}/json),断线用 since=<last_id> 续传。
出站:HTTP PUT /{topic},enable_outbound 关闭时全部静默(纯接收入口)。

路由规则(定稿):
- 应答与主动推送:统一只推送到 push_topics(逗号分隔,广播),
  不向「消息来源 topic」推送——收发职责分离,来源 topic 只是订阅入口。
- 接收 topic 与推送 topic 允许相同:此时靠回环防护标签(bot_tag)
  拦截自己发出的消息,不会死循环。
- enable_outbound=False 时 send() 为 no-op,频道对 ntfy 服务器零出站流量。
- sender_id = 来源 topic,访问控制按 topic 粒度走基类 ACL(白名单/审批)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

import httpx

from qwenpaw.schemas import (
    TextContent,
    ContentType,
)

# 插件作为顶层模块加载,必须用绝对导入
# (qwenpaw 包内频道才可以用 ..base 相对导入)
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.app.channels.base import (
    BaseChannel,
    OnReplySent,
    ProcessHandler,
)

logger = logging.getLogger(__name__)

# 单分片最小保护(字节),防止异常小的配置导致死循环
MIN_CHUNK_BYTES = 200
# 出站 PUT 超时(秒)
PUT_TIMEOUT = 10.0
# 入站 JSON 流读超时(秒):ntfy 每 ~45s 发 keepalive,超时即视为断线重连
STREAM_READ_TIMEOUT = 90.0
# 重连退避上限(秒)
RECONNECT_BACKOFF_MAX = 30.0
# 回环防护/寻址 tag 的默认值:经本频道发出的消息统一带此 tag,
# 入站检测到即跳过(利用 ntfy 官方 Tags 字段,发布/订阅两端原样保留)。
# 可通过配置项 bot_tag 覆盖(逗号分隔列表:第一个为身份 tag,
# 全部用于入站过滤)——多个 agent 共用同一 ntfy 服务器时,各自配置
# 不同 tag 并互相把对方加入过滤列表。
DEFAULT_BOT_TAG = "qwenpaw-bot"
# tag 合法字符:与 ntfy topic 字符集一致(URL/日志/正则中安全)
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# @ 寻址提取:整词提取(tag 后遇非法字符即截断),支持 @ 多个、
# 大小写不敏感;@ 与 tag 之间不能有空格,使用半角 @
_AT_PATTERN = re.compile(r"@([A-Za-z0-9_-]+)")


class NtfyChannel(BaseChannel):
    """ntfy 消息频道(自托管 ntfy.sh 或官方服务)。"""

    channel = "ntfy"  # 唯一 key,必须与 config key 一致

    def __init__(
        self,
        process: ProcessHandler,
        enabled: bool = False,
        server_url: str = "",
        token: str = "",
        subscribe_topics: str = "",
        extra_topics: str = "",  # 已废弃,兼容旧配置;等价于 push_topics
        push_topics: str = "",
        enable_outbound: bool = True,
        max_message_bytes: int = 4000,
        identity_tag: str = "",
        filter_tags: str = "",
        bot_tag: str = "",  # 已废弃,兼容旧配置;等价于 identity_tag+filter_tags
        require_mention: bool = False,
        bot_prefix: str = "",
        on_reply_sent: OnReplySent = None,
        display_config: ChannelDisplayConfig | None = None,
        no_text_debounce: bool = True,
        access_control_dm: bool = False,
        access_control_group: bool = False,
        **kwargs,
    ):
        super().__init__(
            process,
            on_reply_sent=on_reply_sent,
            display_config=display_config,
            no_text_debounce=no_text_debounce,
            access_control_dm=access_control_dm,
            access_control_group=access_control_group,
        )
        self.enabled = enabled
        self.bot_prefix = bot_prefix
        self.server_url = (server_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.subscribe_topics = subscribe_topics or ""
        self.enable_outbound = bool(enable_outbound)
        # push_topics 为空时回退旧字段名 extra_topics(向后兼容)
        self.push_topics = (push_topics or "").strip() or (
            extra_topics or ""
        ).strip()

        try:
            self.max_message_bytes = max(
                MIN_CHUNK_BYTES, int(max_message_bytes or 4000)
            )
        except (TypeError, ValueError):
            self.max_message_bytes = 4000

        # 回环防护/寻址 tag(拆分身份与过滤,正交配置):
        #   identity_tag = 身份 tag(出站 Tags header、@ 寻址的地址)
        #   filter_tags  = 额外过滤标记(已知外部 agent 的 tag,逗号分隔)
        # 入站过滤集 = {identity} ∪ filter_tags(自动含自己,防漏配自回环)
        # @ 认领集合 = {identity}
        # 兼容:identity_tag 为空时回退读旧字段 bot_tag(逗号分隔,
        # 第一个为身份、其余并入过滤),再空则回退默认。防护不可关闭。
        compat_tags = self._parse_bot_tags(bot_tag) if (bot_tag or "").strip() else []
        identity = (identity_tag or "").strip() or (
            compat_tags[0] if compat_tags else DEFAULT_BOT_TAG
        )
        if not _TAG_PATTERN.match(identity):
            if identity:
                logger.warning(
                    "ntfy: invalid identity_tag %r, falling back to %r",
                    identity, DEFAULT_BOT_TAG,
                )
            identity = DEFAULT_BOT_TAG
        self.identity = identity
        self.bot_tag = identity  # 兼容属性名(测试/旧引用)
        self._filter_extra = [
            t for t in self._parse_bot_tags(filter_tags)
            if t.lower() != identity.lower()
        ]
        # 旧字段 bot_tag 的其余元素(身份之外)并入过滤集,完整兼容 v0.2.0
        for t in compat_tags[1:]:
            if t.lower() == identity.lower():
                continue
            if t.lower() not in {e.lower() for e in self._filter_extra}:
                self._filter_extra.append(t)
        self._tag_set = {identity.lower()} | {
            t.lower() for t in self._filter_extra
        }
        # 群聊寻址开关(对齐内置频道语义):开启后仅响应 @自己 的消息
        self.require_mention = bool(require_mention)

        # 逗号分隔 → 列表(去空、去重、保序)
        self._subscribe_list = self._parse_topics(subscribe_topics)
        self._push_list = self._parse_topics(self.push_topics)

        self._http: Optional[httpx.AsyncClient] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        # 最后收到的消息 id,断线重连作为 since 参数续传
        self._last_since: str = ""

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: Any,
        on_reply_sent: OnReplySent = None,
        display_config: ChannelDisplayConfig | None = None,
        no_text_debounce: bool = True,
    ) -> "NtfyChannel":
        """从保存的配置创建实例。

        插件频道的 config 可能是 dict 或 SimpleNamespace,
        两种都兼容。读取用 getattr(config, field, default)。
        """
        if isinstance(config, dict):
            return cls(
                process=process,
                enabled=bool(config.get("enabled", False)),
                server_url=(config.get("server_url") or "").strip(),
                token=(config.get("token") or "").strip(),
                subscribe_topics=(config.get("subscribe_topics") or "").strip(),
                push_topics=(config.get("push_topics")
                             or config.get("extra_topics")
                             or "").strip(),
                enable_outbound=bool(config.get("enable_outbound", True)),
                max_message_bytes=config.get("max_message_bytes", 4000),
                identity_tag=(config.get("identity_tag") or "").strip(),
                filter_tags=(config.get("filter_tags") or "").strip(),
                bot_tag=(config.get("bot_tag") or "").strip(),
                require_mention=bool(config.get("require_mention", False)),
                bot_prefix=(config.get("bot_prefix") or "").strip(),
                on_reply_sent=on_reply_sent,
                display_config=display_config
                or ChannelDisplayConfig.from_config(config),
                no_text_debounce=no_text_debounce,
                access_control_dm=bool(config.get("access_control_dm", False)),
                access_control_group=bool(
                    config.get("access_control_group", False)
                ),
            )
        return cls(
            process=process,
            enabled=bool(getattr(config, "enabled", False)),
            server_url=(getattr(config, "server_url", "") or "").strip(),
            token=(getattr(config, "token", "") or "").strip(),
            subscribe_topics=(
                getattr(config, "subscribe_topics", "") or ""
            ).strip(),
            push_topics=(
                getattr(config, "push_topics", "")
                or getattr(config, "extra_topics", "")
                or ""
            ).strip(),
            enable_outbound=bool(getattr(config, "enable_outbound", True)),
            max_message_bytes=getattr(config, "max_message_bytes", 4000),
            identity_tag=(getattr(config, "identity_tag", "") or "").strip(),
            filter_tags=(getattr(config, "filter_tags", "") or "").strip(),
            bot_tag=(getattr(config, "bot_tag", "") or "").strip(),
            require_mention=bool(getattr(config, "require_mention", False)),
            bot_prefix=(getattr(config, "bot_prefix", "") or "").strip(),
            on_reply_sent=on_reply_sent,
            display_config=display_config
            or ChannelDisplayConfig.from_config(config),
            no_text_debounce=no_text_debounce,
            access_control_dm=bool(
                getattr(config, "access_control_dm", False)
            ),
            access_control_group=bool(
                getattr(config, "access_control_group", False)
            ),
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            logger.info("ntfy channel disabled, not starting")
            return

        if self._subscribe_list:
            if not self.server_url:
                logger.error(
                    "ntfy: subscribe_topics set but server_url missing; "
                    "inbound listener not started",
                )
            else:
                self._stop_event = asyncio.Event()
                self._listen_task = asyncio.create_task(self._listen_loop())
                logger.info(
                    "ntfy inbound listener starting: topics=%s outbound=%s",
                    ",".join(self._subscribe_list),
                    self.enable_outbound,
                )

        if self.enable_outbound and self._push_list and not self.server_url:
            logger.error("ntfy: outbound enabled but server_url missing")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._listen_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._listen_task = None
        client, self._http = self._http, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
        logger.info("ntfy channel stopped")

    # ------------------------------------------------------------------
    # 入站:JSON 流订阅
    # ------------------------------------------------------------------

    def _parse_topics(self, raw: str) -> List[str]:
        seen: List[str] = []
        for part in (raw or "").split(","):
            t = part.strip().strip("/").strip()
            if t and t not in seen:
                seen.append(t)
        return seen

    def _parse_bot_tags(self, raw: str) -> List[str]:
        """解析逗号分隔 tag 列表(纯解析:校验+去重,空输入返回空列表)。"""
        tags: List[str] = []
        for part in (raw or "").split(","):
            t = part.strip()
            if not t:
                continue
            if not _TAG_PATTERN.match(t):
                logger.warning(
                    "ntfy: invalid bot_tag %r dropped (allowed: letters, "
                    "digits, underscore, hyphen; max 64 chars)", t,
                )
                continue
            if t not in tags:
                tags.append(t)
        return tags

    def _auth_headers(self) -> Dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=STREAM_READ_TIMEOUT,
                    write=10.0,
                    pool=10.0,
                ),
                follow_redirects=True,
            )
        return self._http

    async def _listen_loop(self) -> None:
        """JSON 流订阅主循环,断线指数退避重连,since 续传。"""
        base = self.server_url
        topics = ",".join(self._subscribe_list)
        url = f"{base}/{topics}/json"
        backoff = 1.0

        while not (self._stop_event and self._stop_event.is_set()):
            params: Dict[str, str] = {}
            if self._last_since:
                params["since"] = self._last_since
            try:
                client = await self._get_http()
                async with client.stream(
                    "GET", url, params=params,
                    headers=self._auth_headers(),
                ) as resp:
                    if resp.status_code in (401, 403):
                        logger.error(
                            "ntfy stream auth failed (%s): check token / "
                            "topic ACL", resp.status_code,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
                        continue
                    resp.raise_for_status()
                    logger.info("ntfy stream connected: %s", url)
                    backoff = 1.0
                    async for line in resp.aiter_lines():
                        if self._stop_event and self._stop_event.is_set():
                            return
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self._handle_stream_event(obj)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ntfy stream error, reconnecting in %.0fs: %s",
                    backoff, exc,
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

    def _handle_stream_event(self, obj: Dict[str, Any]) -> None:
        event = obj.get("event")
        msg_id = str(obj.get("id") or "")
        if msg_id and event == "message":
            self._last_since = msg_id  # 自己的消息也要推进 since

        if event != "message":
            return  # open / keepalive / poll_request 等

        # ── 回环防护与寻址(定稿管线)──
        # 1) 硬层:消息 tags 与自己的 tag 集合有交集 → 自己发的
        #    或已知 agent 的消息,丢弃。tag 由 ntfy 官方字段承载,
        #    实时流与断线回放均原样保留;用户手打消息不可能携带。
        tags = obj.get("tags") or []
        incoming_tags = (
            {str(t).lower() for t in tags} if isinstance(tags, list) else set()
        )
        if incoming_tags & self._tag_set:
            logger.debug(
                "ntfy: skip agent message %s (tag match)", msg_id,
            )
            return

        text = str(obj.get("message") or "").strip()
        if not text:
            return

        # 2) @ 寻址(协议级,优先于 require_mention):
        #    正文含 @目标 时,仅被点名的 agent 处理。
        #    认领集合 = 身份 tag(过滤列表里的外部标记不是自己的名字)
        at_targets = {
            m.group(1).lower() for m in _AT_PATTERN.finditer(text)
        }
        if at_targets:
            if self.identity.lower() not in at_targets:
                logger.debug(
                    "ntfy: message %s addressed to %s, not me",
                    msg_id, sorted(at_targets),
                )
                return  # 定向给别人的,不接
            # 被 @ → 认领(正文原样保留,agent 读得懂称呼)
        elif self.require_mention:
            # 3) 无 @ 的广播消息:require_mention 开启时静默跳过
            logger.debug("ntfy: skip unaddressed message %s", msg_id)
            return

        topic = str(obj.get("topic") or "").strip()
        if not topic:
            return
        if topic not in self._subscribe_list:
            return  # 服务器多 topic 订阅时的保险过滤

        title = str(obj.get("title") or "").strip()
        if title:
            text = f"{title}: {text}"

        logger.info("ntfy [%s] >> %s", topic, text[:80])
        native = {
            "channel_id": self.channel,
            "sender_id": topic,  # ntfy 无发送者身份,以 topic 为粒度
            "content_parts": [
                TextContent(type=ContentType.TEXT, text=text),
            ],
            "meta": {
                "topic": topic,
                "message_id": msg_id,
                "title": title,
            },
        }
        if self._enqueue is not None:
            self._enqueue(native)
        else:
            logger.warning("ntfy: _enqueue not set, message dropped")

    # ------------------------------------------------------------------
    # 出站:HTTP PUT,统一广播到 push_topics(不回来源 topic)
    # ------------------------------------------------------------------

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: Optional[dict] = None,
    ) -> None:
        """出站统一入口。

        注意:to_handle 参数按 BaseChannel 契约传入(应答场景=来源
        topic),但本频道的路由规则是「只推送到 push_topics,不回来源
        topic」——收发分离由配置决定,想在哪问在哪答就把两组 topic
        配成相同的。to_handle 在此被有意忽略。
        """
        if not self.enable_outbound:
            return  # 纯接收入口:零出站流量
        if not text or not text.strip():
            return

        targets = list(self._push_list)
        if not targets:
            logger.warning(
                "ntfy send: push_topics empty, message dropped "
                "(outbound requires at least one push topic)",
            )
            return

        try:
            chunks = self._chunk_bytes(text, self.max_message_bytes)
        except Exception:  # noqa: BLE001
            logger.exception("ntfy chunk failed; sending truncated head")
            chunks = [text[: self.max_message_bytes // 4]]

        for chunk in chunks:
            for topic in targets:
                await self._put(topic, chunk)

    async def _put(self, topic: str, text: str) -> None:
        if not self.server_url:
            logger.error("ntfy send: server_url missing")
            return
        try:
            client = await self._get_http()
            headers = self._auth_headers()
            headers["Tags"] = self.identity  # 出站身份标记,供入站回环过滤
            resp = await client.put(
                f"{self.server_url}/{topic}",
                content=text.encode("utf-8"),
                headers=headers,
                timeout=PUT_TIMEOUT,
            )
            if resp.status_code // 100 != 2:
                logger.error(
                    "ntfy PUT %s failed: %s %s",
                    topic, resp.status_code, resp.text[:120],
                )
            else:
                logger.info(
                    "ntfy [%s] << %s", topic, text[:60].replace("\n", " "),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("ntfy PUT %s error: %s", topic, exc)

    @staticmethod
    def _chunk_bytes(text: str, limit: int) -> List[str]:
        """按 UTF-8 字节上限分片;回退字符边界,优先断在换行。

        ntfy 的 4KB 限制按字节算,中文 3 字节/字,
        所以不能照搬按字符数的切法。
        """
        encoded = text.encode("utf-8")
        limit = max(limit, MIN_CHUNK_BYTES)
        if len(encoded) <= limit:
            return [text]

        chunks: List[str] = []
        start = 0
        total = len(encoded)
        while start < total:
            end = min(start + limit, total)
            if end >= total:
                chunks.append(encoded[start:].decode("utf-8", "ignore"))
                break
            # 回退到 UTF-8 字符边界(续字节形如 0b10xxxxxx)
            while end > start and (encoded[end] & 0xC0) == 0x80:
                end -= 1
            if end <= start:  # 理论不可达,兜底
                end = start + 1
                while end < total and (encoded[end] & 0xC0) == 0x80:
                    end += 1
            piece = encoded[start:end].decode("utf-8", "ignore")
            # 优先在换行处断(后半段找最后一个 \n),保住段落
            nl = piece.rfind("\n")
            if nl > len(piece) // 2:
                piece = piece[: nl + 1]
            if not piece.strip():
                piece = encoded[start:end].decode("utf-8", "ignore")
            chunks.append(piece)
            start += len(piece.encode("utf-8"))
        return chunks

    # ------------------------------------------------------------------
    # 路由映射
    # ------------------------------------------------------------------

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[dict] = None,
    ) -> str:
        return f"ntfy:{sender_id}"

    def get_to_handle_from_request(self, request: Any) -> str:
        meta = getattr(request, "channel_meta", None) or {}
        topic = meta.get("topic")
        if topic:
            return str(topic)
        sid = getattr(request, "session_id", "")
        if sid.startswith("ntfy:"):
            return sid.split(":", 1)[-1]
        return getattr(request, "user_id", "") or ""

    def to_handle_from_target(self, *, user_id: str, session_id: str) -> str:
        """主动推送(cron / channels send)的日志用目标。

        send() 统一广播 push_topics,此值仅用于管理面日志展示。
        """
        if self._push_list:
            return self._push_list[0]
        return ""

    def build_agent_request_from_native(self, native_payload: Any) -> Any:
        payload = native_payload if isinstance(native_payload, dict) else {}
        channel_id = payload.get("channel_id") or self.channel
        sender_id = payload.get("sender_id") or ""
        content_parts = payload.get("content_parts") or []
        meta = payload.get("meta") or {}
        session_id = self.resolve_session_id(sender_id, meta)
        request = self.build_agent_request_from_user_content(
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
            content_parts=content_parts,
            channel_meta=meta,
        )
        request.user_id = sender_id
        request.channel_meta = meta
        return request

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    async def health_check(self) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "channel": self.channel,
                "status": "disabled",
                "detail": "ntfy channel is disabled.",
            }
        if not self.server_url:
            return {
                "channel": self.channel,
                "status": "error",
                "detail": "server_url is required.",
            }
        try:
            client = await self._get_http()
            resp = await client.get(
                f"{self.server_url}/v1/health",
                headers=self._auth_headers(),
                timeout=5.0,
            )
            healthy = resp.status_code == 200 and b"healthy" in resp.content
            detail = (
                "ntfy server healthy."
                if healthy
                else f"unexpected response: HTTP {resp.status_code}"
            )
            return {
                "channel": self.channel,
                "status": "ok" if healthy else "warn",
                "detail": detail,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "channel": self.channel,
                "status": "error",
                "detail": f"cannot reach ntfy server: {exc}",
            }

# -*- coding: utf-8 -*-
"""ntfy Channel 插件入口。"""

import logging

from qwenpaw.plugins.api import PluginApi

logger = logging.getLogger(__name__)

FIELD_LABEL_SERVER = {"zh": "服务器地址", "en": "Server URL"}
FIELD_HELP_SERVER = {
    "zh": "ntfy 服务地址,如 http://10.0.0.102:18888 或 https://ntfy.example.com",
    "en": "ntfy server base URL, e.g. https://ntfy.example.com",
}
FIELD_LABEL_SUB = {"zh": "订阅 topic(入站)", "en": "Subscribe topics (inbound)"}
FIELD_HELP_SUB = {
    "zh": "逗号分隔,如 test-topic,trade-cmd-1;留空 = 纯通知出口(只发不收)",
    "en": "Comma separated topics to listen on; empty = outbound only",
}
FIELD_LABEL_EXTRA = {"zh": "额外推送 topic(出站)", "en": "Extra push topics (outbound)"}
FIELD_HELP_EXTRA = {
    "zh": "开启输出推送时,应答会额外广播到这些 topic(与来源 topic 去重合并);定时任务推送目标取第一个",
    "en": "Replies are also broadcast to these topics; first one is used for scheduled pushes",
}
FIELD_LABEL_OUT = {"zh": "输出推送", "en": "Push replies"}
FIELD_HELP_OUT = {
    "zh": "开:回复推送到来源 topic + 额外 topic;关:完全静默(纯接收入口,只留会话历史)",
    "en": "On: replies are pushed; Off: fully silent inbound-only channel",
}
FIELD_LABEL_ACL = {"zh": "访问控制", "en": "Access control"}
FIELD_HELP_ACL = {
    "zh": "开启后,新 topic 的首条消息需在控制台审批后才能触发 agent",
    "en": "First message from a new topic requires console approval",
}


class NtfyChannelPlugin:
    """注册 ntfy 频道。"""

    def register(self, api: PluginApi) -> None:
        # loader 将插件目录加入 module __path__,裸导入按 #6683 语义
        # 重定向到插件目录(相对导入会报 beyond top-level package)
        from ntfy_channel import NtfyChannel

        api.register_channel(
            channel_class=NtfyChannel,
            label="ntfy",
            description=(
                "ntfy 推送通知频道:支持双向收发、纯通知出口、"
                "纯接收入口三种模式"
            ),
            config_fields=[
                {
                    "name": "server_url",
                    "label": FIELD_LABEL_SERVER,
                    "type": "text",
                    "required": True,
                    "placeholder": "http://10.0.0.102:18888",
                    "help": FIELD_HELP_SERVER,
                },
                {
                    "name": "token",
                    "label": {"zh": "访问令牌", "en": "Token"},
                    "type": "password",
                    "required": False,
                    "placeholder": "tk_xxxxxxxxxxxx",
                    "help": {
                        "zh": "ntfy 访问令牌(deny-all 模式必填)",
                        "en": "ntfy access token (required when deny-all)",
                    },
                },
                {
                    "name": "subscribe_topics",
                    "label": FIELD_LABEL_SUB,
                    "type": "text",
                    "required": False,
                    "placeholder": "test-topic,trade-cmd-1",
                    "help": FIELD_HELP_SUB,
                },
                {
                    "name": "push_topics",
                    "label": {"zh": "推送 topic(出站)", "en": "Push topics (outbound)"},
                    "type": "text",
                    "required": False,
                    "placeholder": "qwenpaw-notify",
                    "help": {
                        "zh": "agent 的所有输出只推送到这些 topic(逗号分隔,广播),不会推回消息来源 topic。可与订阅 topic 相同(相同则靠回环防护标签防死循环);定时任务推送同样走这里",
                        "en": "All outbound messages are broadcast to these topics only; source topics are never replied to. May overlap with subscribed topics",
                    },
                },
                {
                    "name": "enable_outbound",
                    "label": FIELD_LABEL_OUT,
                    "type": "switch",
                    "required": False,
                    "default": True,
                    "help": FIELD_HELP_OUT,
                },
                {
                    "name": "max_message_bytes",
                    "label": {
                        "zh": "单条消息字节上限",
                        "en": "Max bytes per message",
                    },
                    "type": "number",
                    "required": False,
                    "default": 4000,
                    "help": {
                        "zh": "按 UTF-8 字节计;ntfy.sh 默认限制 4096 字节,超出自动按换行/字符边界分片",
                        "en": "UTF-8 byte budget per message; overlong text is chunked",
                    },
                },
                {
                    "name": "identity_tag",
                    "label": {"zh": "身份标签", "en": "Identity tag"},
                    "type": "text",
                    "required": False,
                    "placeholder": "qwenpaw-bot",
                    "default": "qwenpaw-bot",
                    "help": {
                        "zh": "本 agent 的身份 tag:出站消息自动携带,也是 @ 寻址的地址(如 @qwenpaw-bot)。多 agent 共用 ntfy 时各自配置不同身份",
                        "en": "This agent's identity tag: attached to outbound messages and used as the @ mention address",
                    },
                },
                {
                    "name": "filter_tags",
                    "label": {"zh": "过滤标签列表", "en": "Filter tags"},
                    "type": "text",
                    "required": False,
                    "placeholder": "other-agent-tag, another-bot",
                    "help": {
                        "zh": "逗号分隔。携带这些 tag 的消息将被忽略(多 agent 共用 topic 时,把其他 agent 的身份 tag 加进来即可隔离);自己的身份 tag 自动包含,无需重复填写",
                        "en": "Comma-separated tags to ignore (e.g. other agents' identity tags when sharing a topic); your own identity tag is always included automatically",
                    },
                },
                {
                    "name": "require_mention",
                    "label": {"zh": "需要 @提及", "en": "Require @Mention"},
                    "type": "switch",
                    "required": False,
                    "default": False,
                    "help": {
                        "zh": "开启后,仅处理 @自己 的消息(如 @qwenpaw-bot 你好);多 agent 共用 topic 时建议副 agent 开启",
                        "en": "When enabled, only messages mentioning this agent's tag are processed",
                    },
                },
                # 显示开关(show_tool_calls/show_tool_results/show_thinking
                # 及预览长度)由控制台 ChannelDrawer 通用区块渲染,
                # 此处不再声明,否则 UI 会出现重复开关
                {
                    "name": "bot_prefix",
                    "label": {"zh": "消息前缀", "en": "Bot prefix"},
                    "type": "text",
                    "required": False,
                    "placeholder": "[QwenPaw] ",
                    "help": {
                        "zh": "回复消息的统一前缀",
                        "en": "Prefix prepended to replies",
                    },
                },
                {
                    "name": "access_control_dm",
                    "label": FIELD_LABEL_ACL,
                    "type": "switch",
                    "required": False,
                    "default": False,
                    "help": FIELD_HELP_ACL,
                },            ],
        )
        logger.info("✓ ntfy channel registered")


plugin = NtfyChannelPlugin()

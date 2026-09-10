# qwenpaw-ntfy-channel

[中文](#简介) | [English](#english)

---

## 简介

[QwenPaw](https://github.com/agentscope-ai/QwenPaw) 的 [ntfy](https://ntfy.sh) 消息频道插件。将你的 QwenPaw agent 接入自托管或 ntfy.sh 公共服务,实现手机推送、远程指令、agent 间消息总线。

**English**: A [ntfy](https://ntfy.sh) channel plugin for [QwenPaw](https://github.com/agentscope-ai/QwenPaw). Connect your QwenPaw agent to a self-hosted or ntfy.sh server for push notifications, remote commands, and agent-to-agent messaging.

## 功能特性

- **收发分离路由**:应答只推送到「推送 topic」(广播),不回消息来源 topic;订阅 topic 与推送 topic 可任意组合
- **回环防护**:出站消息自动携带身份 tag(基于 ntfy 官方 Tags 字段),入站检测到即过滤,收发同 topic 也不会死循环
- **@ 寻址**:消息中 `@<bot_tag>` 定向呼叫特定 agent(支持任意位置、@ 多个、大小写不敏感、整词匹配)
- **仅响应 @ 提及**(`require_mention`):多 agent 共用 topic 时,副 agent 开启后只响应被点名的消息
- **多 agent 隔离**:`bot_tag` 支持逗号分隔列表(第一个为身份 tag,全部用于入站过滤),互相把对方 tag 加入列表即完成隔离
- **UTF-8 字节安全分片**:长回复按字节上限自动分片(优先断在换行、回退字符边界),中文/emoji 不截断不乱码
- **三种运行形态**(配置即切换):
  - 双向对话(订阅 + 推送)
  - 纯通知出口(不订阅,cron / 定时任务推送)
  - 纯接收入口(推送开关关闭,输出零出站流量)
- **访问控制**:对接 QwenPaw 内置 ACL(topic 粒度白名单 + 控制台审批)
- **断线续传**:JSON 流订阅,断线用 `since=<last_id>` 续传 + 指数退避重连

## 安装

```bash
# 方式一:从 GitHub 直接安装
qwenpaw plugin install https://github.com/Mcpy/qwenpaw-ntfy-channel

# 方式二:克隆后本地安装
git clone https://github.com/Mcpy/qwenpaw-ntfy-channel
qwenpaw plugin install ./qwenpaw-ntfy-channel
```

安装后重启 QwenPaw(或重新保存频道配置触发热重载),在 **Control → Channels** 中即可看到 ntfy 频道卡片。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `server_url` | text | — | ntfy 服务地址,如 `https://ntfy.example.com` |
| `token` | password | — | 访问令牌(`deny-all` 模式必填,格式 `tk_xxx`) |
| `subscribe_topics` | text | — | 入站订阅 topic,逗号分隔;留空 = 纯通知出口 |
| `push_topics` | text | — | 出站推送 topic,逗号分隔广播;应答与定时任务均只推送到这里 |
| `enable_outbound` | switch | 开 | 关闭后输出不推送(纯接收入口) |
| `identity_tag` | text | `qwenpaw-bot` | 身份 tag:出站消息自动携带,也是 @ 寻址的地址(如 `@qwenpaw-bot`) |
| `filter_tags` | text | — | 额外过滤标记(逗号分隔):携带这些 tag 的消息将被忽略;自己的身份 tag 自动包含,多 agent 共用 topic 时把其他 agent 的身份 tag 加进来 |
| `require_mention` | switch | 关 | 需要 @提及:开启后仅处理 `@自己` 的消息;多 agent 共用 topic 时建议副 agent 开启 |
| `markdown` | switch | 关 | 出站携带 `X-Markdown: true`,ntfy 客户端渲染粗体/列表/代码块;旧客户端无害降级显示原文。长消息分片时代码块可能跨片 |

**可靠性细节**:入站按消息 id 去重(300 秒窗口,防断线重连重放);流连接遇 401/403/404(认证/ACL/topic 不存在等确定性拒绝)停止重连循环并在日志报 fatal,修复配置后保存触发热重载恢复;5xx/网络错误仍指数退避重试。
| `max_message_bytes` | number | 4000 | 单条消息 UTF-8 字节上限,超出自动分片 |
| `bot_prefix` | text | — | 回复消息前缀 |
| `access_control_dm` | switch | 关 | 开启后新 topic 首条消息需在控制台审批 |

### 快速开始(最小配置)

1. **推送场景**(任务完成提醒):填 `server_url` + `token` + `push_topics: my-notify`,手机订阅 `my-notify`
2. **双向对话**:再填 `subscribe_topics: my-cmd`,手机从 `my-cmd` 发消息,agent 处理后结果推送到 `my-notify`
3. **在哪问在哪答**:`push_topics` 与 `subscribe_topics` 配成相同的 topic 即可(回环防护自动生效)

## 运行测试

```bash
# 需要一个可读写的 ntfy topic,修改脚本头部常量后运行:
python tests/test_loop.py
```

测试覆盖:出站消息零入站(回环防护)、外部消息正常入站、应答零回流、长文本分片。

## 多 agent 场景

| 场景 | 推荐配置 |
|---|---|
| 多 agent 各管各的(推荐) | 收发分离:每个 agent 订阅/推送各自的 topic,天然隔离 |
| 多 agent 共用一个 topic | 各配不同的 `bot_tag`,并互相把对方 tag 加入自己的过滤列表;副 agent 开 `require_mention`,用户用 `@<tag>` 定向呼叫 |

**诚实边界**:ntfy 不提供消息发送者身份(平台级无 `author.bot` 之类字段),对于不携带任何 tag 的第三方 agent(无法从消息层与人类区分),只能靠 topic 拓扑分离隔离——这也是 Telegram 靠平台屏蔽 bot 消息、Discord 靠 `author.bot` 字段才能做到的事。

## 设计说明

- **为什么用 Tags 做回环防护**:ntfy 官方未提供消息回环机制(消息无发送者身份,订阅过滤器不支持负向排除);官方 Tags 字段在实时流与断线回放中均原样保留,是零竞态、零误滤的标记载体
- **为什么不回来源 topic**:收发职责分离,把"在哪问在哪答"与"指令/结果通道分离"两种拓扑都交给配置决定
- 详细设计讨论见 [QwenPaw contributing 文档](https://qwenpaw.agentscope.io/docs/contributing/) 与本仓库 issue

## License

[MIT](LICENSE)

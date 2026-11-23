# app-gateway · 给聊天机器人接一个自己写的 App

> 镜像仓库：[Gitee](https://gitee.com/rainprayer/app-gateway) ｜ [GitHub](https://github.com/rain-prayer/app-gateway)

> 运行在开源机器人框架 **[Neo-MoFox](https://github.com/)** 上的第三方插件，**不是独立应用**。
> 框架本身不是我写的，本仓库只包含我编写的网关插件；配套客户端见 **[bubble-app](https://gitee.com/rainprayer/bubble-app)**。

## 为什么会有这个

第三方 IM 协议受平台政策限制、无法长期使用。于是我自己写了一个聊天 App（Flutter），并写了这个网关，把 App 和机器人框架接起来：

```
Flutter App  ──HTTP / WS──▶  [app-gateway :8100]  ──▶  框架消息总线  ──▶  Chatter  ──▶  LLM
    │                              │
    └── 媒体上传 / 空间 / 日记 ─────┴──▶ SQLite（动态、评论、日记）
```

**关键约束**：不能绕过框架内部的 Chatter，也不能另起一套人格——她的人设与记忆必须完全沿用。所以网关只做传输层，所有「她怎么想、怎么回」都交回框架处理。

## 架构

```
adapter.py            平台适配器：入站消息喂进框架消息总线，出站拦截框架的发送出口
protocol.py           帧协议：文字 / 图片 / 表情 / 语音 / 引用
service/bridge.py     与框架聊天流对接（算 stream_id、取或建 chatter）
service/hub.py        连接管理：多连接广播 + 离线 pending 队列
server/app.py         FastAPI 应用装配 + 限流中间件
server/api.py         /health /me /chat/send /chat/history /upload /profile /diary/*
server/ws.py          WebSocket（首帧 5 秒内鉴权，否则 4401）
server/qzone.py       空间：动态 / 点赞 / 评论与回复（SQLite）
server/diary.py       日记：每日 02:00 生成前一天的日记 + 手动补写限流
server/ratelimit.py   滑动窗口限流
server/storage.py     媒体落盘
server/auth.py        Bearer 鉴权
```

## 几个真实工程坑（都是踩过才有的设计）

**① 独立起 HTTP 服务，必须覆盖 uvicorn 的信号处理**

框架插件里自己 `uvicorn.Server` 起服务时，它会在主线程装 SIGINT/SIGTERM 处理器，把框架的退出信号抢走（按 Ctrl+C 只关网关不关 bot）。做法是：

```python
class _NoSignalServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield
```

**② 出站永远返回成功，离线进队列**

App 不在线时如果直接返回失败，框架会把「她说了话但没人收到」记成发送错误，导致会话状态与快照分叉。所以：消息推给在线连接（全失败则进 pending，上限 500），**对框架永远返回 success=True**。

**③ 延迟/媒体消息走独立通道**

base64 动辄几百 KB，塞进 WebSocket 帧会拖慢消息通道。所以 `POST /upload` 单独上传，消息帧只带 `media_id`；发送前按文件头嗅探真实 MIME（GIF/JPEG/PNG/WebP），而不是用扩展名猜。

**④ 给视觉模型的图片要传本地路径**

框架的图片识别读本地缓存目录，给 URL 让它自己下载容易失败。

## 接口约定（客户端照此对接）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 免鉴权，用于探活 |
| POST | `/api/v1/chat/send` | `{text, client_msg_id, kind, media_id}` |
| GET | `/api/v1/chat/history?limit&before` | 分页拉历史 |
| POST | `/api/v1/upload` | `{data(base64), kind, mime}` → `{id, url}` |
| WS | `/ws` | 首帧 `{type:"auth", token}`，之后双向帧 |
| GET | `/api/v1/qzone/posts` | 空间动态 |
| POST | `/api/v1/diary/generate` | 手动补写日记（每天一次） |

鉴权：`Authorization: Bearer <token>`。token 留空时插件会**自动生成并持久化**到 `data/neo_app_gateway/token.txt`。

## 配置

复制 `config.example.toml` 到框架的插件配置目录（文件名 `config.toml`），按需改：

```toml
[server]
host = "0.0.0.0"
port = 8100
token = ""            # 留空自动生成

[bridge]
user_id = "100000000" # App 用户的稳定 ID
nickname = "你"
her_name = "她"
```

## 依赖

- Neo-MoFox 框架（插件 API、消息总线、Chatter）
- Python 3.11+、FastAPI、uvicorn

## 许可

MIT（仅本仓库内的插件代码；宿主框架版权归其作者）

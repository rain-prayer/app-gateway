"""自建 App 网关插件（neo_app_gateway）。

设计要点（与 QQ 适配器完全隔离，不绕过 KFC）：
1. 入站：App 的文本 → MessageEnvelope(platform="app") → CoreSink
   → MessageReceiver → ChatStream.unread → tick 触发 KokoroFlowChatter。
   插件**不直接调用** chatter.execute()，也不自己造 ChatStream。
2. 出站：KFC 回复 → send_api → MessageSender → AppAdapter._send_platform_message
   → WebSocket 推给 App；App 不在线则进离线队列，上线补推。
3. 隔离：platform 是出站路由的唯一键，OneBot 适配器只认 "qq"，
   本适配器只认 "app"，互不相干。详见 README.md。
"""

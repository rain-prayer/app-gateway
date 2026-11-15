"""App 网关的配置声明。"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class AppGatewayConfig(BaseConfig):
    """自建 App 网关配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "自建聊天 App 的 WebSocket 网关配置"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):
        """插件开关。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 App 网关（关闭后不监听端口）",
            label="启用网关",
            tag="plugin",
        )

    @config_section("server", title="服务端", tag="network")
    class ServerSection(SectionBase):
        """HTTP / WebSocket 服务端。"""

        host: str = Field(
            default="0.0.0.0",
            description="监听地址。0.0.0.0 才能被手机访问；只想本机调试可改 127.0.0.1",
            label="监听地址",
            tag="network",
        )
        port: int = Field(
            default=8100,
            description="监听端口（核心的 8000 只绑 127.0.0.1，手机连不上，所以这里单独起一个）",
            label="端口",
            tag="network",
        )
        token: str = Field(
            default="",
            description="Bearer Token。留空则自动生成并保存到 data/neo_app_gateway/token.txt，重启不变",
            label="访问令牌",
            tag="security",
        )
        allow_origins: list[str] = Field(
            default_factory=lambda: ["*"],
            description="CORS 允许来源。手机 App 用原生请求时可保持 *",
            label="CORS",
            tag="network",
        )

    @config_section("bridge", title="桥接", tag="user")
    class BridgeSection(SectionBase):
        """App 用户与框架流的对应关系。"""

        user_id: str = Field(
            default="100000000",
            description="App 用户的稳定 ID。沿用原来的 QQ 号可以让记忆归属保持一致",
            label="用户 ID",
            tag="user",
        )
        nickname: str = Field(
            default="淮",
            description="她在提示词里看到的你的名字",
            label="昵称",
            tag="user",
        )
        her_name: str = Field(
            default="小鲸鱼",
            description="她在空间里显示的名字（不是代词「她」）",
            label="她的名字",
            tag="user",
        )
        upstream_platform: str = Field(
            default="qq",
            description="迁移来源平台。迁移脚本会从该平台的会话文件复制上下文到这里",
            label="来源平台",
            tag="user",
            disabled=True,
        )

    @config_section("push", title="离线推送", tag="network")
    class PushSection(SectionBase):
        """ntfy 离线推送（App 未连接时把她的消息推到手机通知栏）。"""

        ntfy_enabled: bool = Field(
            default=False,
            description="是否启用 ntfy 推送。Bark 只支持 iOS，安卓用 ntfy",
            label="启用 ntfy",
            tag="network",
        )
        ntfy_url: str = Field(
            default="https://ntfy.sh",
            description="ntfy 服务地址。自建填自己的地址",
            label="ntfy 地址",
            tag="network",
        )
        ntfy_topic: str = Field(
            default="",
            description="ntfy 主题名（相当于密码，别用短的）",
            label="ntfy 主题",
            tag="network",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    server: ServerSection = Field(default_factory=ServerSection)
    bridge: BridgeSection = Field(default_factory=BridgeSection)
    push: PushSection = Field(default_factory=PushSection)

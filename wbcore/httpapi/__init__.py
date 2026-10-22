"""HTTP 接口层。

按 HTTP 方法拆成三个 mixin，最后在 server.Handler 里组合：

    base.py      传输 + 鉴权 + 静态资源 + GET
    protocol.py  POST（Responses API 与 Chat Completions 兼容层）
    admin.py     PATCH / DELETE 及管理面板 REST 接口
    server.py    Handler 组合 + 端口探测 + 启动 + 心跳
"""

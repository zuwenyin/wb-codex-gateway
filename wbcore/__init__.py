"""wb-codex-gateway 的核心库。

按职责分层，彼此只用显式 import，不共享隐式全局：

    paths.py            路径常量（唯一定义"文件放在哪"的地方）
    context.py          配置 / 日志 / 状态文件 / STORE 持有者
    store.py            账号池与设置的持久化（data.json）
    proto.py            Responses <-> Chat Completions 协议转换
    billing.py          积分与签到
    meta.py             模型上下文窗口元数据
    upstream.py         访问 WorkBuddy 上游
    local_accounts.py   扫描并调度本机 WorkBuddy 账号
    models.py           模型清单
    tokens.py           token 估算与上下文窗口
    faults.py           上游错误判定与解释
    codex_config.py     改写 Codex 的 config.toml
    auth.py             管理页口令
    httpapi/            HTTP 接口层
    server.py           服务装配与生命周期
"""

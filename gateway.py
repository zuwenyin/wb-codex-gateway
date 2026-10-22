#!/usr/bin/env python3
"""
wb-codex-gateway — Codex / ChatGPT 桌面端 <-> WorkBuddy 云的协议网关。

这个入口只做一件事：把服务跑起来。所有实现都在 wbcore/ 里分层组织：

    wbcore/paths.py            路径常量
    wbcore/context.py          配置、日志、状态文件、STORE
    wbcore/store.py            账号池与设置持久化
    wbcore/proto.py            Responses <-> Chat Completions 转换
    wbcore/upstream.py         访问 WorkBuddy 上游
    wbcore/local_accounts.py   本机账号扫描与调度
    wbcore/httpapi/            HTTP 接口层
    wbcore/httpapi/server.py   服务装配与生命周期

用法：
    python gateway.py
    或双击 start-gateway.cmd
"""

import sys

from wbcore.httpapi.server import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

"""路径常量。

所有需要落盘 / 读取的项目文件都从这里走，避免各模块自己
拼 os.path.dirname(__file__) —— 那样一旦模块挪进子包，
data.json 就会跟着跑到 wbcore/ 里去。
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # 项目根目录（网关在这里运行）
WEB_DIR = os.path.join(ROOT, "web")          # 管理面板静态资源
STATUS_PATH = os.path.join(ROOT, "status.json")
DATA_PATH = os.environ.get("WB_DATA_PATH") or os.path.join(ROOT, "data.json")
CATALOG_PATH = os.path.join(ROOT, "models.json")

# Node 侧凭证读取脚本
WBCREDS_DIR = os.path.join(ROOT, "wbcreds")
WBCREDS_JS = os.path.join(WBCREDS_DIR, "wbcreds.js")

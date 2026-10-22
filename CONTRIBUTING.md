# 贡献指南

感谢你想让 wb-codex-gateway 更好。

## 开发约定

- 本项目**只用 Python 标准库**，不要引入第三方依赖（这是刻意的设计，让克隆即用）。
- 需要文件路径时**一律从 `wbcore.paths` 取**（`ROOT` / `DATA_PATH` / `CATALOG_PATH` …），
  **不要写死绝对路径**（会泄露个人隐私），**也不要在子包里自己拼 `__file__`**
  —— 那样模块一挪位置，`data.json` 就跟着跑到子目录里去了。
- 涉及上游请求的地方，注意处理 `Expect: 100-continue` 和 chunked 编码。

## 代码放在哪

| 新增的东西 | 放哪 |
|---|---|
| 协议转换（报文结构、工具调用、流式事件） | `wbcore/proto.py` |
| 访问上游（URL、请求头、重试） | `wbcore/upstream.py` |
| 账号池 / 设置的持久化 | `wbcore/store.py` |
| 配置、日志、`status.json` | `wbcore/context.py` |
| HTTP 接口 | 按方法拆到 `wbcore/httpapi/` 下对应的 mixin |
| 一次性工具、校验脚本 | `scripts/` |
| 测试 | `tests/`（文件名 `test_*.py`） |

跨模块访问配置和账号池**只能**用 `ctx.CFG` / `ctx.STORE`（`from .. import context as ctx`），
不要在模块顶层写 `STORE = ctx.STORE` —— 那是快照，启动后注入的新实例看不到。

## 提交前请确认

```cmd
python tests\selftest.py
python tests\test_failure_kind.py
python tests\test_model_cooldown.py
python tests\test_quota_429.py
python tests\test_finish_reason.py
python tests\test_token_estimate.py
python tests\test_context_pipeline.py
python tests\test_expire_first.py
```

全部 PASS 再提交。

## 提交信息

用中文或英文都行，说清楚「改了什么 / 为什么」：

```
修复 11148 工具调用序列错乱

并行调用时 tool 结果被提前 flush，导致上游报序列不匹配。
改成两阶段收集后再按顺序落盘。
```

## 提 Issue 时请附上

- Windows 版本、Codex 版本（`codex --version`）
- `~/.codex/config.toml` 里 provider 相关段落（去掉密钥）
- 网关日志（`start-gateway.cmd` 窗口的输出；后台启动时是 `gw.err.log` / `gw.out.log`）
- 复现步骤

## 安全提示

**不要**把以下文件提交上来：

- `data.json`（含真实 token）
- `config.json`（含 approval_key）
- 任何日志文件

这些已经在 `.gitignore` 中排除，提交前请 `git status` 确认。

# wb-codex-gateway

让 **ChatGPT 桌面应用 / Codex** 使用 **WorkBuddy（CodeBuddy）** 的云模型。

**核心特点：不需要手动填任何 API Key —— 点一下就能扫描本机已登录的 WorkBuddy 账号。**

> 📌 **开源说明**：本仓库已移除所有个人路径、用户名与密钥。
> 首次运行会自动生成 `data.json` 与 `config.json`（二者已在 `.gitignore` 中，请勿提交）。

---

## 为什么必须有一层网关

ChatGPT 桌面应用的内核 `codex-cli 0.163` 已彻底移除 `wire_api = "chat"`：

```
Error: `wire_api = "chat"` is no longer supported.
How to fix: set `wire_api = "responses"`
```

而 WorkBuddy 只提供 Chat Completions，两边协议不通，中间必须有翻译层。

```
ChatGPT 桌面/Codex ──Responses──> gateway.py ──ChatCompletions──> WorkBuddy 云
                                         │
                                   管理界面（本机账号 / 账号池 / 开关）
```

---

## 目录结构

入口只有一个 `gateway.py`，实现按职责分层放在 `wbcore/`：

```
gateway.py              入口：把服务跑起来
start-gateway.cmd       Windows 双击启动

wbcore/                 核心库
  paths.py        路径常量（唯一定义"文件放哪"的地方）
  context.py      配置 / 日志 / status.json / STORE 持有者
  store.py        账号池与设置的持久化（data.json）
  proto.py        Responses <-> Chat Completions 协议转换
  upstream.py     访问 WorkBuddy 上游（地址、请求头、重试）
  local_accounts.py  本机账号扫描与自动扫描调度
  models.py       模型清单
  tokens.py       上下文窗口与 token 估算
  faults.py       上游错误判定与人话解释
  billing.py      积分、签到
  meta.py         模型上下文窗口元数据
  codex_config.py 改写 Codex 的 config.toml（官方/网关切换）
  auth.py         管理页口令
  httpapi/        HTTP 接口层
    base.py       传输 / 鉴权 / 静态资源 / GET
    protocol.py   POST（Responses 与 Chat Completions 兼容层）
    admin.py      PATCH / DELETE 及管理面板 REST
    server.py     Handler 组合 + 端口探测 + 启动 + 心跳

wbcreds/                Node 脚本：从桌面端读取并解密本机登录态
web/                    管理面板前端
scripts/                一次性工具脚本（生成/校验模型目录）
tests/                  自测与单元测试
```

模块之间不共享隐式全局：只能通过 `wbcore.context`（`ctx`）晚绑定访问
配置与账号池 —— 这是拆分成包之后仍然不会出循环依赖的原因。

---

## 快速开始（三步）

```cmd
start-gateway.cmd
```

1. 打开 **<http://127.0.0.1:18080/>**
2. 点 **「⟳ 扫描本机 WorkBuddy 账号」** —— 自动读取本机已登录账号，**无需填 Key**
3. 打开右上角 **代理开关**

就这样。账号表格里会直接出现你的 WorkBuddy 昵称（如 `177****0961`）和积分。

---

## 本机账号是怎么读到的

整个过程**不修改 WorkBuddy 任何文件**，只读：

| 环节 | 位置 / 方式 |
|---|---|
| 登录态 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| 桌面端定位 | 环境变量 → 卸载注册表（`DisplayIcon` / `InstallLocation`）→ 所有盘符的 `Program Files`、盘根及名字像 WorkBuddy 的目录 |
| 解密密钥 | `ELECTRON_RUN_AS_NODE=1 WorkBuddy.exe -e <脚本>`，取 `atRestSecretKey`（不支持 `-e` 的构建回退临时文件） |
| 密钥派生 | **SHA-256(key 的 base64 字符串)**，keyId 取摘要前 16 位十六进制 |
| 解密 | AES-256-GCM，AAD = `WB-AAD\0` + 版本号 + 若干字段；多份安装按信封里的 keyId 选密钥 |

拿到 `access_token` 就存在网关自己的 `data.json` 里，之后直接调用上游。

> 密钥派生这一条是踩坑总结出来的：不是对解码后的字节做 SHA-256，而是对 **base64 字符串本身**做摘要。

找不到桌面端时（装在非常规目录、绿色版、被改名），两种补救办法：

```cmd
set WORKBUDDY_APP_EXECUTABLE=D:\你的路径\WorkBuddy\WorkBuddy.exe
set WB_APP_SEARCH_DIRS=D:\你的路径;E:\Tools
```

### 扫描拿不到账号怎么查

网关启动时会在日志里先做一次自检，缺什么直接说：

```
INFO  扫描自检通过：登录态 5 个文件，桌面端 WorkBuddy.exe
WARN  找不到 Node.js，无法扫描本机账号（[WinError 2] 系统找不到指定的文件）
WARN  没有找到 WorkBuddy 登录态文件（workbuddy-desktop.info）
```

再跑一次完整诊断（`--deep` 会真的尝试解密一次）：

```cmd
node wbcreds\wbcreds.js --doctor --deep
```

输出里逐项看：

| 字段 | 含义 |
|---|---|
| `node` / `localappdata` | Node 版本与当前用户目录（用管理员/别的账号跑时这里会变） |
| `auth_dirs[].files` | 每个候选目录里有几个 `.info`，全是 0 说明登录态不在这些位置 |
| `auth_files` | 实际会读取的文件（已知目录全空时会在用户目录下浅层搜索） |
| `app_candidates` | 找到的桌面端 exe，空数组说明解密密钥拿不到 |
| `dump.code` | `NO_ACCOUNT`（没文件）/`DECRYPT_FAILED`（版本不匹配）/`REFRESH_EXPIRED`（登录过期）/`NO_TOKEN` |

登录态确实在别处时，用 `set WORKBUDDY_AUTH_FILE=<目录或文件>` 直接指过去，
或 `set WB_AUTH_SEARCH_DIRS=D:\some\auth` 追加搜索目录。

---

## 模型目录（models.json）

Codex 需要一个模型目录文件，网关会生成并维护它：

```cmd
python scripts\build_catalog.py    :: 从上游拉取真实的上下文窗口
python scripts\enrich_catalog.py   :: 把窗口/倍率写进 display_name 与 description
python scripts\verify_catalog.py   :: 校验生成结果
```

当前 15 个模型，例如：

```
DeepSeek V4.1 Flash (1M · x0.11)    上下文 1,000,000
Kimi K2.7 (256K · x0.57)            上下文   256,000
DeepSeek V3.2 (96K · x0.29)          上下文    96,000
```

**两处显示位置的差异（实测）**：

| 位置 | 渲染字段 |
|---|---|
| Composer 的模型下拉 | 只有 `display_name` |
| `/model` 斜杠命令的搜索列表 | `description` 会显示 |

所以倍率既进了名字（简短），也在 description 里有完整版。

> 改完目录后**需要重启 ChatGPT 桌面端**才会生效 —— 目录是启动时读一次的。

---

## 控制面板

浏览器打开 **<http://127.0.0.1:18080/>**，可以：

- **扫描 / 管理本机账号**（新增、删除、测试、重置、**让给外部**）
- **账号池策略**：轮询 / 故障转移 / 随机 / **积分过期优先**
- **代理总开关**
- **停止网关**：结束进程（右上角按钮）
- **签到与积分**：一键签到、查看每账号剩余积分与最近到期时间
- **异常账号**一键恢复

### 与其它客户端共存（dsh 等）

同一个 WorkBuddy 登录态可能被别的程序（例如 dsh 的 workbuddy 插件）同时使用，
这时**别让两边共用同一个账号**——上游会把它当成"多渠道混用"而拦截。

两种彻底解耦的方式：

**① 让出账号（推荐）**

账号行右侧点 **「让给外部」**，该账号会变成「● 已让出」：

- 网关不再用它发请求，也不查它的积分（连 `/v2/billing` 都不打）
- 它也不会出现在 `/v1/models` 的模型来源里
- 需要收回时点 **「收回自用」** 即可

于是可以这样分工：dsh 用 A 账号，网关用 B 账号，互不打扰。

**② 停止网关**

右上角 **「停止网关」**（或 `POST /admin/api/shutdown`）会结束网关进程，
之后本机其它客户端与网关之间不存在任何耦合；要用时重新运行 `start-gateway.cmd`。

> 网关**空闲时不访问上游**：积分刷新只在收到 `/v1` 请求时发生，
> 扫描只在启动/手动点击时发生。所以"网关在跑"本身不会占用你的账号。

### 上游的 11128「渠道校验」是什么

上游对某些**放在 `system` 消息里的 Agent 标识短语**会返回：

```
400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel",
     "displayTips":"This call failed channel verification…"}
```

实测（2026-10-22，同一账号同一 token）：

| `system` 消息内容 | 结果 |
|---|---|
| `You are a helpful assistant.` | 200 |
| `You are an AI agent powered by DeepSeek Harness.` | **400 / 11128** |
| `DeepSeek Harness`（只有这几个字） | 200 |
| 同样一句话，放到 `user` 角色 | 200 |

也就是说：**规则 = 在 `system` 角色里匹配特定 Agent 标识短语**（例如 dsh 的系统提示开头）。
看起来是上游为拦截"第三方 harness"加的内容风控，2026-10-20 之后收紧。

网关的转换层把系统提示整体折叠成 user 消息，所以不会命中：

```python
# wbcore/proto.py
responses_to_chat(..., system_as_user=True)   # system / developer → user
```

**直连上游的第三方插件必须自己折叠角色**，否则会出现"普通请求能过、Agent 请求必挂"的现象。

### 四种策略说明

| 策略 | 行为 |
|---|---|
| `round_robin` | 轮流使用（默认） |
| `failover` | 固定优先级，坏了才换下一个 |
| `random` | 随机挑 |
| `expire_first` | **优先消耗最快过期的积分**，减少浪费 |

### 签到与积分

- 一键为所有账号签到，领取每日积分
- 积分按到期时间排序显示

三个数字口径不同，别混着看（早先把「累计」当成「余额」显示，导致和官网差一大截）：

| 面板字段 | 含义 | 对应官网 |
|---|---|---|
| **可用积分** | 积分包剩余合计（`CapacityType=1`） | 「剩余 805.24 积分」「可用 8 个」 |
| **套餐额度** | 套餐/体验版基础额度（`CapacityType=4`），单列 | 套餐余额，**不计入**上一项 |
| **累计获得** | 历史签到累计，只增不减 | 「累计积分」 |

数值取上游 `*Precise` 字段（字符串，带小数），
用整数字段会把 `27.76000183` 截成 `27`，越算越偏。
调度（`expire_first`）仍按「积分包 + 套餐额度」的总可用量排序，
因为套餐额度通常最先到期，不优先烧掉就浪费了。

---

## 配置

首次运行会自动从 `config.example.json` 生成 `config.json`：

```json
{
  "listen": "127.0.0.1:18080",
  "admin_password": "",
  "approval_key": "wb-local-8f3a2c",
  "models": "deepseek-v4-flash,deepseek-v4-pro,glm-5.3,kimi-k3-1",
  "upstream_base": "https://copilot.tencent.com",
  "upstream_key": "",
  "timeout": 300
}
```

| 字段 | 说明 |
|---|---|
| `listen` | 监听地址，只绑 127.0.0.1 |
| `admin_password` | 管理页密码，留空则本机免密 |
| `approval_key` | 网关准入 key，要和 Codex 的 bearer token 一致 |
| `models` | 暴露给 Codex 的模型白名单 |
| `upstream_base` | 上游地址（国内版） |
| `upstream_key` | 手动模式才需要填 |
| `timeout` | 上游超时（秒） |

支持的环境变量：`WB_LISTEN`、`WB_DATA_PATH`、`WB_MODELS`、`WB_APPROVAL_KEY`、`WB_NODE_PATH`、`WB_DEBUG_UPSTREAM`、`WB_LOG_REQUESTS`、`WB_NO_AUTO_SCAN`、`WORKBUDDY_APP_EXECUTABLE`、`WB_APP_SEARCH_DIRS`、`WORKBUDDY_AUTH_FILE`、`WB_AUTH_SEARCH_DIRS`、`CODEX_HOME`。

### 自动扫描本机账号

不用每次手动点「扫描」：网关启动时若账号池里**没有可用的本机账号**会自己扫一次，
之后每 5 分钟巡检一遍，仍然没有可用账号就再扫（在 WorkBuddy 里重新登录后能自动接上）。

- 只影响**本机账号**（`kind=local`），手动填 key 的账号不参与
- 你手动删掉的本机账号不会被自动加回来；手动点一次「扫描」才会重新加入
- `WB_NO_AUTO_SCAN=1` 可关闭（自测脚本就用它，避免把真实账号扫进隔离数据）
- 找不到桌面端时可用 `WORKBUDDY_APP_EXECUTABLE` 指定 exe，`WB_APP_SEARCH_DIRS` 追加搜索目录

---

## Codex 侧配置

在 `~/.codex/config.toml` 末尾加：

```toml
model = "deepseek-v4.1-flash"
model_provider = "workbuddy"

[model_providers.workbuddy]
name = "WorkBuddy"
base_url = "http://127.0.0.1:18080/v1"
wire_api = "responses"
experimental_bearer_token = "wb-local-8f3a2c"
```

`model_catalog_json` 指向生成的 `models.json`（绝对路径）。

### 在管理页一键切换

手动改上面这些行容易改错，管理页的 **「Codex 接入模式」** 面板可以直接切：

- **WorkBuddy 网关**：写好 `model` / `model_provider` / `model_catalog_json`，并可下拉选择用哪个网关模型
- **官方模型**：把 `model_provider`、`model_catalog_json` 注释掉，`model` 恢复成切走前记下的原值

改动只涉及这三行，你在文件里写的注释都保留；改前会备份成 `config.toml.wb-bak`。
对应接口：`POST /admin/api/codex`，`{"mode": "gateway" | "official", "model": "可选"}`。

> 无论手动改还是点按钮，**都要重启 ChatGPT 桌面端**才生效（配置是启动时读一次的）。
> 注意「暂停代理」不等于切回官方模型 —— 暂停只是拒绝请求，模型列表仍来自 `config.toml`。

---

## 关于「上下文太长」

上游返回的真实报错：

```json
{"code":11115,"msg":"prompt is too long: 1081330 tokens > 1048576 maximum"}
```

网关会把它翻译成人话，并**动态填入真实上限**（不同模型窗口不同，96K ~ 1M）。

⚠️ 曾经有个 bug：提示写死成「100K」，跟实际模型无关。已修。

另外，上游的 **6004 是按【模型】计的日额度**，不是积分不足。同一个账号换个模型照样能用。

---

## 已知坑

### 代理软件会拦截 Codex 的请求

如果装了 Clash Verge / mihomo（系统代理 `127.0.0.1:7897`），Codex 发往 `127.0.0.1:18080` 的请求会被代理接管，然后返回 **502 Bad Gateway**，而网关日志里**完全看不到请求**。

```powershell
[Environment]::SetEnvironmentVariable("NO_PROXY", "127.0.0.1,localhost", "User")
```

设完重启 ChatGPT 桌面端。

---

## 测试

```cmd
python tests\selftest.py               :: 主回归（起 mock 服务 + 真 Codex 客户端往返）
python tests\test_failure_kind.py      :: 失败分类（账号/请求/链路抖动 三类互斥）
python tests\test_model_cooldown.py    :: 按模型冷却
python tests\test_quota_429.py         :: 配额耗尽处理
python tests\test_finish_reason.py     :: finish_reason 截断标记
python tests\test_token_estimate.py    :: token 估算（图片按尺寸而非字节）
python tests\test_context_pipeline.py  :: 完整转换链路
python tests\test_expire_first.py      :: 过期优先排序
```

全部为纯标准库，无第三方依赖。测试会在 `tests/` 下临时落盘数据文件并在结束时清理，
不会碰真实 `data.json`（`tests/selftest.py` 还额外设了 `WB_NO_AUTO_SCAN=1`，
避免把本机真实账号扫进隔离的测试数据）。

---

## 免责声明

- 本项目是**协议适配工具**，不提供、不托管任何模型服务。
- 你需要**自行拥有合法的 WorkBuddy 账号**并遵守其服务条款。
- 项目仅读取本机登录态用于调用你自己的订阅额度，不上传任何数据。
- 使用后果由使用者自行承担，与本项目无关。
- 请勿用于任何违反上游服务条款的用途（如多人共享、转售）。

---

## License

[MIT](LICENSE) © 2026 wb-codex-gateway contributors

#
使用问题和配置教程请在https://linux.do/t/topic/2403436
（限制最低1级可见）讨论。

#
glm端午到现在都没放货哈，抢不到是正常的。可以去他们的飞书群里喷他们。

# AegisFlow

`AegisFlow` 是一个本地运行的 `GLM Coding` 支付运营后台，用来管理多账号导入、套餐同步、自动验证码链路、预览下单、签单出二维码，以及定时启动任务。

当前页面已经不是早期的调试台，而是简约列表后台：

- 顶部弹窗导入账号
- 导入后自动同步账号上下文和套餐
- 每个账号单独选择套餐
- 每个账号单独配置定时启动时间
- 列表直接展示最新支付二维码和价格
- 点击账号名查看上下文
- 支持“同步并换指纹”
- 支持删除账号，并清理该账号本地缓存

## 功能概览

- 导入 `bigmodel_token_production`
- 自动调用 `getCustomerInfo`
- 自动补齐默认 `Organization / Project`
- 自动调用 `batch-preview` 获取套餐状态
- 后端拉取腾讯验证码图片
- 内置 OCR 识别点击点位
- 后端生成 `collect / eks / pow`
- 后端自动提交腾讯 `verify`
- `preview` 无 `bizId` 时自动重跑整条链路
- 新购走 `/biz/pay/create-sign`
- 升级走 `/biz/pay/product/update/sign`
- 签单失败先重试 `3` 次，失败后回到整条链路重新拿新 `bizId`
- 本地生成支付二维码
- 定时任务自动启动指定账号链路
- 服务启动时自动检查本地缓存账号是否有效
- 服务重启时自动清空旧二维码缓存

## 不包含

- 账号密码登录自动化
- `deviceToken`
- 浏览器页面端支付跳转逻辑
- 前端中间页 AES 协议复刻

## 环境要求

- Windows 或 macOS
- Python 3.12+
- Node.js 可用并在 `PATH` 中

说明：

- Python 用于 FastAPI 服务和 OCR
- Node.js 用于腾讯验证码 TDC VM 运行
- `start.bat` / `start.sh` 首次启动会自动创建 `.venv` 并安装 Python 依赖

## 最傻瓜版本地启动

先说结论：本地模式不用代理池，也不用旧的 Go `dynamic-proxy`。装好 Python 和 Node.js，Windows 运行 `start.bat`，macOS 运行 `./start.sh`，打开页面就完事儿，别一上来就把自己扔进配置海里游泳。

### 1. 先确认环境

Windows 在 PowerShell 里确认这几个命令能跑：

```powershell
py -3 --version
node --version
npm --version
```

macOS 在 Terminal 里确认这几个命令能跑：

```bash
python3 --version
node --version
npm --version
```

推荐版本：

- Python `3.12+`
- Node.js LTS，能正常执行 `npm`

### 2. 第一次启动

Windows 直接双击项目根目录的 `start.bat`。

macOS 在项目根目录执行：

```bash
chmod +x start.sh
./start.sh
```

第一次启动会自动做这些事：

- 创建 `.venv`
- 安装 Python 依赖
- 安装并构建 Vue 前端
- 启动 FastAPI 本地服务
- 如果 `FALLBACK_PROXY_URL` 指向本机 `17286`，顺手准备内置 Python 代理池服务

看到服务启动后，打开：

```text
http://127.0.0.1:8787
```

### 3. 最小本地配置

正常情况下不需要手动创建 `.env`，项目会按默认值跑。如果你想显式配置，复制 `.env.example` 为 `.env`，本地模式保持下面这样就行：

```env
APP_HOST=127.0.0.1
APP_PORT=8787
DATA_DIR=data

NETWORK_EGRESS_MODE=local
FALLBACK_PROXY_URL=http://127.0.0.1:17286
FALLBACK_PROXY_TICKET_POOL_ONLY=0
PROXY_POOL_CONFIG=proxy_pool.yaml

TENCENT_OCR_WORKERS=4
RUNTIME_LOG_LEVEL=INFO
RUNTIME_LOG_RETENTION_DAYS=7
```

这几个最常改：

- `APP_PORT`：页面端口，默认 `8787`。端口冲突时换一个。
- `DATA_DIR`：本地数据目录，默认 `data`。
- `TENCENT_OCR_WORKERS`：OCR 并发，机器一般就用 `4`，卡就改成 `1` 或 `2`。
- `NETWORK_EGRESS_MODE`：默认 `local`，也就是本机网络出口。要用代理池可以在 Web 右上角切换，不建议新手直接把默认值改成 `proxy_pool`。

其他 BigModel、腾讯验证码协议类配置，没明确原因别乱动。那玩意儿不是旋钮，是地雷阵。

### 4. 本地启动排错

- 页面打不开：先看 `start.bat` 窗口或 `start.sh` 终端有没有报错，再确认打开的是 `http://127.0.0.1:8787`。
- 提示 Python 不存在：安装 Python 后重新打开终端。Windows 确认 `py -3 --version` 能输出版本；macOS 确认 `python3 --version` 是 `3.12+`。
- 提示 npm 不存在：安装 Node.js LTS 后重新打开终端，确认 `npm --version` 能输出版本。
- 端口被占用：改 `.env` 里的 `APP_PORT`，比如 `APP_PORT=8788`，然后重新运行启动脚本。

## 启动进程与 OCR Worker 说明

本项目本地默认通过 `start.bat` 或 `start.sh` 启动：

- Windows: `uvicorn app.main:app --host %APP_HOST% --port %APP_PORT% --reload`
- macOS: `uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT" --reload`

这意味着正常开发启动时通常会看到：

- `1` 个 `uvicorn` reload 监控进程
- `1` 个实际承载 FastAPI 的应用进程
- `N` 个 OCR 进程池 worker 上限，其中 `N = TENCENT_OCR_WORKERS`

说明：

- 项目没有配置 `uvicorn --workers`，所以 Web 服务本身不是多 worker 部署
- OCR 并发是独立进程池，不是 `uvicorn` worker
- 多账号调度是单应用进程里多线程拉任务，真正重 CPU 的 OCR 再交给 OCR 进程池
- 服务启动阶段会按 `.env` 中的 `TENCENT_OCR_WORKERS` 一次性预热全部 OCR worker，避免首轮支付链路临时 spawn worker 拖慢识别
- 支付链路启动前仍会按前端 `Preview 并发` 配置和当前活跃任务需求校验 OCR 容量，但不会超过 `TENCENT_OCR_WORKERS`
- 真正运行时 OCR 并发上限由 `TENCENT_OCR_WORKERS` 控制

`TENCENT_OCR_WORKERS` 是系统 OCR worker 最大数量，不配置时默认 `4`：

- `TENCENT_OCR_WORKERS=4`

举例：

- `Preview 并发=4` 且 `TENCENT_OCR_WORKERS=4`，服务启动时会直接预热 4 个 OCR worker
- 两个账号同时运行，账号 A `Preview 并发=3`，账号 B `Preview 并发=1`，活跃 OCR 需求为 `4`，服务启动后已经有 4 个 OCR worker 可用
- 如果活跃 OCR 需求为 `8`，但 `TENCENT_OCR_WORKERS=4`，仍然最多只跑 4 个 OCR worker，其余 OCR 请求排队

如果你想强制单路 OCR，直接把：

- `TENCENT_OCR_WORKERS=1`

写进 `.env` 就行。

## 傻瓜版代理池启动和配置

先把话说明白：现在代理池是项目内置 Python 服务，不需要 `dynamic-proxy`，也不需要 Go。默认入口就是：

```env
FALLBACK_PROXY_URL=http://127.0.0.1:17286
```

它的链路长这样：

```text
AegisFlow -> 127.0.0.1:17286 -> 内置 Python 代理池 -> 上游代理 -> BigModel / Captcha
```

### 1. 不用代理池怎么配

啥都不用改，保持：

```env
NETWORK_EGRESS_MODE=local
FALLBACK_PROXY_URL=http://127.0.0.1:17286
PROXY_POOL_CONFIG=proxy_pool.yaml
```

Web 右上角出口模式保持“本地”。这种情况下支付链路走本机网络，代理池即使被服务准备好了，也不会参与请求。

### 2. 使用代理池怎么配

第一步，在项目根目录新建或更新 `proxies.txt`，一行一个上游代理：

```text
1.2.3.4:1080
socks5://1.2.3.4:1080
socks5://user:pass@1.2.3.4:1080
http://1.2.3.4:8080
```

没有协议前缀时，项目默认按 SOCKS5 代理处理。

第二步，确认 `proxy_pool.yaml` 里指向筛选后的代理文件：

```yaml
proxy_list_urls:
  - "good_proxies.txt"

health_check:
  target: "www.bigmodel.cn:443"

ports:
  http_relaxed: ":17286"
```

第三步，先运行一次启动脚本，让项目创建 `.venv` 并安装依赖。Windows 用 `start.bat`，macOS 用 `./start.sh`。已经启动过可以跳过这步。

第四步，用内置检测脚本从 `proxies.txt` 筛出可用代理：

```powershell
.venv\Scripts\python.exe -m app.proxy_pool.checker --from-config proxy_pool.yaml --source proxies.txt --target www.bigmodel.cn:443 --max-latency 3000 --timeout 6 --concurrency 200 --output good_proxies.txt --show-errors
```

macOS 对应命令：

```bash
.venv/bin/python -m app.proxy_pool.checker --from-config proxy_pool.yaml --source proxies.txt --target www.bigmodel.cn:443 --max-latency 3000 --timeout 6 --concurrency 200 --output good_proxies.txt --show-errors
```

跑完后确认根目录生成了 `good_proxies.txt`，并且里面不是空的。空文件就说明这批代理基本废了，别硬上，硬上就是给自己添堵。

第五步，重新运行启动脚本。

第六步，打开 Web 页面右上角出口模式，切到“代理池”。页面会弹窗提醒：如果没有配置代理池或代理源不可用，切换后服务无法正常运行。确认你已经有 `good_proxies.txt` 后再点确认。

### 3. 代理池推荐配置

`.env` 里推荐保持：

```env
NETWORK_EGRESS_MODE=local
FALLBACK_PROXY_URL=http://127.0.0.1:17286
FALLBACK_PROXY_TICKET_POOL_ONLY=0
PROXY_POOL_CONFIG=proxy_pool.yaml
```

推荐默认 `NETWORK_EGRESS_MODE=local`，需要时在 Web 端切到代理池。这样代理池炸了还能切回本地，不至于一启动就全线趴窝。

如果你确定每次启动都要默认走代理池，再改成：

```env
NETWORK_EGRESS_MODE=proxy_pool
```

如果只想让 ticket 池消耗阶段的 `/preview` 走代理池，后续生成二维码、查询支付状态、验证码 challenge/verify 走本地，可以开启：

```env
FALLBACK_PROXY_TICKET_POOL_ONLY=1
```

### 4. 代理服务商白名单

如果你的代理服务商要求先把本机出口 IP 加白名单，再拉代理，才需要开启这些配置：

```env
PROXY_WHITEIP_ENABLED=1
PROXY_WHITEIP_SECRET_ID=your_secret_id
PROXY_WHITEIP_SECRET_KEY=your_secret_key
PROXY_WHITEIP_SECRET_TOKEN_API=
PROXY_WHITEIP_SIGN_TYPE=token
PROXY_WHITEIP_SIGNATURE=
PROXY_WHITEIP_API=
PROXY_WHITEIP_LIST=
PROXY_WHITEIP_WAIT_SECONDS=5
```

不用白名单的服务商，保持 `PROXY_WHITEIP_ENABLED=0`，其他字段留空。

### 5. 代理池端口说明

- `http://127.0.0.1:17286`：HTTP relaxed，推荐给 AegisFlow 使用，兼容性最好。
- `http://127.0.0.1:17285`：HTTP strict，会验证上游 TLS。
- `socks5://127.0.0.1:17284`：SOCKS5 relaxed。
- `socks5://127.0.0.1:17283`：SOCKS5 strict。

`FALLBACK_PROXY_URL` 默认填 `http://127.0.0.1:17286` 就行，别整花活。

### 6. 代理池排错

- Web 提示代理池不可用：先确认 `good_proxies.txt` 存在且有内容。
- 筛选结果为空：换代理源，或者把 `--max-latency 3000` 临时放宽到 `5000` 再试。
- 启动时报端口占用：确认本机 `17283`、`17284`、`17285`、`17286` 没有被其他程序占用，重新运行启动脚本会尝试清理这些本地监听。
- 代理商要白名单：配置 `PROXY_WHITEIP_*`，否则代理拿到了也可能连不上。
- 免费代理不稳定：正式跑前重新执行一次 checker，别拿昨天的 `good_proxies.txt` 赌今天的链路。

## 页面使用

### 1. 导入账号

点击顶部 `导入账号`，填写：

- 账号备注
- Token
- 邀请码：可选，留空使用默认邀请码

注意：

- 导入成功后会立刻执行：
  - 保存账号
  - 同步上下文
  - 获取套餐列表

### 2. 查看账号列表

列表页每行展示：

- 账号备注
- 购买模式：`新购 / 升级`
- 当前账号指纹 profile：例如 `chrome146 / chrome145 / edge146 / firefox149`
- 套餐下拉选择器
- 定时启动配置
- Ticket 池大小和发射间隔
- 账号状态
- 最新支付二维码
- 操作按钮

### 3. 切换套餐

每个账号的套餐用下拉框选择，切换后自动保存到本地会话。

### 4. 定时启动

每个账号都可以设置是否启用定时任务，以及启动时间。

默认时间：

- `09:59:58`

时间格式支持：

- `HH:MM`
- `HH:MM:SS`

实际保存时会统一格式化成 `HH:MM:SS`。

Ticket 池发射间隔在 Web 端按账号设置，不再通过 `.env` 配置：

- `0ms`：并行发射所有未使用 ticket 的 `/preview` 请求，谁先拿到 `bizId` 谁胜出
- 大于 `0ms`：按固定间隔串行发射，例如 `300` 表示两次 `/preview` 之间间隔约 `300ms`

### 5. 立即启动

点击 `立即启动` 后，会立即执行该账号的完整支付链路：

1. 走验证码链路
2. 获取 `preview`
3. 签单
4. 生成二维码

### 6. 同步并换指纹

点击 `同步并换指纹` 后，会按“换指纹 -> 同步账号上下文 -> 同步套餐”的顺序执行。

如果同步失败，后端会继续换下一个指纹并重试，直到同步成功或达到最大重试次数。

默认最大重试次数：

- `BOOTSTRAP_FINGERPRINT_MAX_RETRIES=99`

这适合在上游风控、链路异常、套餐状态异常时主动切换一套新的网络指纹继续尝试。

### 7. 查看上下文

点击账号名，可以查看当前账号上下文，包括：

- `customerNumber`
- `customerName`
- 账号状态
- 状态说明
- 定时配置
- 最近检查时间
- 完整账号 / 会话 JSON

### 8. 删除账号

点击 `删除账号` 后，会删除这个账号的本地数据。

当前删除范围包括：

- `accounts.json` 中该账号记录
- `tasks.json` 中该账号的二维码任务
- `data/sessions/{account_id}.json`
- `data/logs/` 中名字或内容带该 `account_id` 的文件 / 目录
- `data/test_runs/` 中名字或内容带该 `account_id` 的文件 / 目录

## 账号状态说明

页面状态列会展示两类状态：

### 账号状态

- `unchecked`：尚未检查
- `valid`：最近一次同步 / 启动检查成功
- `expired`：账号态不可用或凭据失效
- `error`：接口异常、系统繁忙、链路失败等

### 定时任务状态

- `running`
- `success`
- `failed`

说明：

- 手动同步失败时，账号状态会保留失败原因，不会再提前误写成 `valid`
- 服务启动后的自动检查如果成功，账号状态也会更新为最新结果

## 支付链路说明

### 购买模式判定

`/products` 这一步会根据 `batch-preview.isSubscribed` 给账号和套餐打标：

- `new_purchase`
- `upgrade`

后续二维码签单会按这个模式分流。

### 新购链路

使用：

- `/biz/pay/create-sign`

### 升级链路

使用：

- `/biz/pay/product/update/sign`

依赖 `preview.lastSubscriptionSummary` 中的：

- `productId`
- `agreementNo`

### 验证码重试策略

- OCR 点位少于 `3` 个，直接刷新验证码重新跑
- OCR 抛异常，也会刷新重跑
- `verify` 失败会回到整条验证码链路重新开始
- `preview` 没拿到 `bizId`，会无限重跑整条链路

### 二维码签单重试策略

拿到 `bizId` 后：

- 先对当前 `bizId` 重试签单 `3` 次
- 如果都失败：
  - 清空当前 `preview`
  - 重新走整条 `preview` 链路拿新 `bizId`
  - 再继续签单

## 指纹策略

`AegisFlow` 当前不是完整浏览器驱动，而是后端 HTTP 请求链路，所以做的是账号级稳定伪装：

- 每个账号首次导入时随机分配一个具体版本的 `browser_impersonate`
- 当前随机候选值为：`chrome146 / chrome145 / edge146 / firefox149`
- 随机分配带权重，默认更偏向 Chrome，少量分配 Edge / Firefox，贴近真实桌面浏览器分布
- 每个 profile 同时绑定 `curl-cffi` TLS/HTTP 指纹和匹配的 Windows 桌面 `User-Agent`
- 后续这个账号的 BigModel、腾讯验证码、TDC 请求都复用同一个 profile

这样做的目的：

- 保持同一账号整条链路的指纹一致
- 避免“每次请求随机换脸”导致风控更容易命中
- 避免新版 UA 搭配旧版 TLS 指纹这种很假的组合

浏览器版本说明：

- 2026-04-26 查询桌面浏览器版本占有率后，Chrome 侧优先使用高占比的 `145/146`
- Edge 侧使用 `edge146` 对应的 Windows UA；由于 `curl-cffi 0.15.0` 缺少新版 Edge TLS profile，transport 暂时复用同代 Chrome 146 指纹
- Firefox 侧使用 `firefox149` 对应的 Windows UA；由于 `curl-cffi 0.15.0` 支持的最新 Firefox transport 为 `firefox147`，底层 TLS 暂时落到 `firefox147`

## 本地数据目录

正式运行数据位于：

```text
data/
  accounts.json
  tasks.json
  sessions/
  logs/
    runtime/
  tdc_cache/
```

说明：

- `accounts.json`：账号主档
- `tasks.json`：最新二维码任务
- `sessions/`：账号上下文缓存
- `logs/`：运行日志
- `logs/runtime/`：正式运行日志
- `tdc_cache/`：腾讯 TDC 脚本缓存

## 正式运行日志

项目现在已经补了正式运行日志，不再只是控制台里飘几行 `INFO`。

日志目录：

```text
data/logs/runtime/
  app.log
  events-YYYY-MM-DD.jsonl
  accounts/
    {account_id}/
      YYYY-MM-DD.jsonl
```

说明：

- `app.log`：标准文本日志，适合直接翻看服务启动、异常栈、调度器运行情况
- `events-YYYY-MM-DD.jsonl`：当天全量结构化流水，适合排查整站任务
- `accounts/{account_id}/YYYY-MM-DD.jsonl`：单账号结构化流水，适合盯一个账号复盘

结构化日志核心字段：

- `timestamp`：事件时间
- `account_id`：账号 ID
- `run_id`：单次运行链路 ID，同一条链路会贯穿 `captcha -> preview -> sign`
- `action`：动作名称，比如 `run_payment_flow`、`bootstrap_account`
- `stage`：步骤名称，比如 `batch_preview`、`captcha_verify`、`preview`、`sign`
- `status`：步骤状态，比如 `started`、`success`、`retry`、`failed`、`paused`
- `details`：补充字段，包含 OCR 点位数、置信度、`bizId`、签单轮次等

当前正式日志会覆盖这些关键链路：

- 服务启动 / 停止
- OCR 预热
- 调度器启动、轮询异常、启动检查
- 账号导入、删除、同步并换指纹
- `getCustomerInfo`
- `/biz/pay/batch-preview`
- 验证码获取
- OCR 点位门禁判断
- 腾讯 `verify`
- `preview`
- `create-sign` / `update-sign`
- 二维码生成
- 支付状态检查
- 暂停、失败、重试

敏感字段处理：

- `token`
- `cookie`
- `ticket`
- `randstr`
- `sign`
- `collect / eks`
- base64 图片 / 二维码

这些字段进入结构化日志时会自动脱敏，不会原样落盘。

## 重启行为

服务每次启动时会做两件事：

1. 清空旧二维码缓存
2. 异步检查本地缓存账号是否有效

另外：

- 已生成的旧二维码不会跨重启保留
- 正式日志不会清空，会持续按天累积
- 日志保留天数默认 `7` 天，可通过 `RUNTIME_LOG_RETENTION_DAYS` 调整

这意味着：

- 页面不会沿用上一次生成的旧二维码
- 账号状态会在启动后自动更新

## 配置项

可选环境变量见 `.env.example`。现在模板分成两块：

- **可自定义配置**：按本机端口、数据目录、代理池、OCR 并发和日志需求调整。
- **默认配置**：BigModel / 腾讯验证码协议参数和稳定默认行为，通常不需要动，除非上游接口或排查目标明确变化。

```env
APP_HOST=127.0.0.1
APP_PORT=8787
DATA_DIR=data
NETWORK_EGRESS_MODE=local
FALLBACK_PROXY_URL=http://127.0.0.1:17286
FALLBACK_PROXY_TICKET_POOL_ONLY=0
PROXY_POOL_CONFIG=proxy_pool.yaml
PROXY_WHITEIP_ENABLED=0
PROXY_WHITEIP_SECRET_ID=
PROXY_WHITEIP_SECRET_KEY=
PROXY_WHITEIP_SECRET_TOKEN_API=
PROXY_WHITEIP_SIGN_TYPE=token
PROXY_WHITEIP_SIGNATURE=
PROXY_WHITEIP_API=
PROXY_WHITEIP_LIST=
PROXY_WHITEIP_WAIT_SECONDS=5
PROXY_POOL_MAX_LATENCY_MS=3000
PROXY_POOL_FAST_WINDOW=32
PROXY_POOL_FAILURE_COOLDOWN_SECONDS=60
TENCENT_OCR_WORKERS=4
RUNTIME_LOG_LEVEL=INFO
RUNTIME_LOG_RETENTION_DAYS=7

BIGMODEL_API_BASE=https://www.bigmodel.cn/api
BIGMODEL_ORIGIN=https://www.bigmodel.cn
BIGMODEL_REFERER=https://www.bigmodel.cn/glm-coding
DEFAULT_LANGUAGE=zh
BROWSER_IMPERSONATE=chrome146
BOOTSTRAP_FINGERPRINT_MAX_RETRIES=99
REQUEST_TIMEOUT_SECONDS=20
TENCENT_CAPTCHA_DOMAIN=https://turing.captcha.qcloud.com
TENCENT_CAPTCHA_AID=196026326
TENCENT_CAPTCHA_ENTRY_URL=https://www.bigmodel.cn/glm-coding
TENCENT_CAPTCHA_MAX_RETRIES=3
TENCENT_CAPTCHA_MIN_CONFIDENCE=0.55
TENCENT_CAPTCHA_NODE=node
TENCENT_OCR_ENABLED=1
TENCENT_OCR_INCLUDE_DEBUG=0
TENCENT_OCR_TIMEOUT_SECONDS=6
TENCENT_OCR_OPENCV_THREADS=1
TENCENT_OCR_ONNX_THREADS=1
```

布尔值参数支持：

- `1 / true / yes / on` 表示开启
- 其他值视为关闭

可自定义配置：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `APP_HOST` | `127.0.0.1` | Web 服务监听地址，启动脚本会优先读取这个值来启动 `uvicorn` |
| `APP_PORT` | `8787` | Web 服务监听端口，启动脚本会先杀掉当前端口已占用进程再启动 |
| `DATA_DIR` | `data` | 本地数据目录，保存账号、会话、任务、日志、TDC 缓存；相对路径会按项目根目录解析 |
| `NETWORK_EGRESS_MODE` | `local` | 启动默认出口模式，只支持 `local` 和 `proxy_pool`；运行中也可在 Web 端切换 |
| `FALLBACK_PROXY_URL` | `http://127.0.0.1:17286` | 代理池模式使用的本地入口，默认指向内置 Python 代理池 HTTP relaxed 端口 |
| `FALLBACK_PROXY_TICKET_POOL_ONLY` | `0` | 为 `1` 时，代理池只用于 ticket 池 drain 阶段的 `/preview` |
| `PROXY_POOL_CONFIG` | `proxy_pool.yaml` | 内置 Python 代理池配置文件，相对路径按项目根目录解析 |
| `PROXY_WHITEIP_ENABLED` | `0` | 是否启用代理服务商白名单接口；开启后内置 Python 代理池启动时会尝试添加当前出口 IP |
| `PROXY_WHITEIP_SECRET_ID` | 空 | 代理服务商 API 身份 ID；不用白名单接口时留空 |
| `PROXY_WHITEIP_SECRET_KEY` | 空 | 代理服务商 API 密钥；不用动态令牌接口时留空 |
| `PROXY_WHITEIP_SECRET_TOKEN_API` | 空 | 代理服务商动态令牌接口地址；仅在服务商需要先用密钥换取令牌时填写 |
| `PROXY_WHITEIP_SIGN_TYPE` | `token` | 代理服务商签名方式，按自己的代理服务商文档填写 |
| `PROXY_WHITEIP_SIGNATURE` | 空 | 代理服务商签名令牌；如果使用密钥自动换取令牌，可留空 |
| `PROXY_WHITEIP_API` | 空 | 代理服务商白名单接口地址，模板不预设外部网站 |
| `PROXY_WHITEIP_LIST` | 空 | 需要加入白名单的 IP 列表；留空时由代理服务商接口自行识别当前出口 IP |
| `PROXY_WHITEIP_WAIT_SECONDS` | `5` | 调用白名单接口后等待代理池生效的时间，单位秒 |
| `PROXY_POOL_MAX_LATENCY_MS` | `3000` | 代理健康检测后的最大允许延迟，单位毫秒；超过该值的代理会被丢弃 |
| `PROXY_POOL_FAST_WINDOW` | `32` | 运行时只在延迟排序最靠前的 N 个代理内轮询；小于 `1` 时不限制窗口 |
| `PROXY_POOL_FAILURE_COOLDOWN_SECONDS` | `60` | 某个代理连接失败后的冷却时间，单位秒；冷却期内不再分配请求给该代理 |
| `TENCENT_OCR_WORKERS` | `4` | 系统 OCR worker 数量和最大并发；服务启动时会按该值一次性预热全部 worker |
| `RUNTIME_LOG_LEVEL` | `INFO` | 正式运行日志级别 |
| `RUNTIME_LOG_RETENTION_DAYS` | `7` | `app.log` 按天轮转保留天数 |

默认配置：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `BIGMODEL_API_BASE` | `https://www.bigmodel.cn/api` | BigModel API 根地址 |
| `BIGMODEL_ORIGIN` | `https://www.bigmodel.cn` | BigModel 请求头 `Origin` 默认值 |
| `BIGMODEL_REFERER` | `https://www.bigmodel.cn/glm-coding` | BigModel 请求头 `Referer` 默认值 |
| `DEFAULT_LANGUAGE` | `zh` | 默认请求语言，会写入 `Accept-Language` 和 `Set-Language` |
| `BROWSER_IMPERSONATE` | `chrome146` | 全局兜底浏览器指纹 profile；账号实际请求优先用账号自己的随机 `browser_impersonate` |
| `BOOTSTRAP_FINGERPRINT_MAX_RETRIES` | `99` | 点击“同步并换指纹”时的最大尝试次数；每轮先换一个账号级指纹，再完整同步上下文和套餐，失败才进入下一轮 |
| `REQUEST_TIMEOUT_SECONDS` | `20` | 上游 HTTP 请求超时时间，单位秒 |
| `TENCENT_CAPTCHA_DOMAIN` | `https://turing.captcha.qcloud.com` | 腾讯验证码域名 |
| `TENCENT_CAPTCHA_AID` | `196026326` | 腾讯验证码业务 `aid` |
| `TENCENT_CAPTCHA_ENTRY_URL` | `https://www.bigmodel.cn/glm-coding` | 腾讯验证码 `entry_url` 和默认 `Referer` |
| `TENCENT_CAPTCHA_MAX_RETRIES` | `3` | 预留的验证码客户端最大重试次数配置，目前主链路重试逻辑由支付服务控制 |
| `TENCENT_CAPTCHA_MIN_CONFIDENCE` | `0.55` | OCR 点位门禁最低置信度，小于这个值会直接刷新验证码重跑 |
| `TENCENT_CAPTCHA_NODE` | `node` | 跑腾讯 TDC VM 时使用的 Node.js 命令 |
| `TENCENT_OCR_ENABLED` | `1` | 是否启用本地 OCR；关闭后自动识别不可用 |
| `TENCENT_OCR_INCLUDE_DEBUG` | `0` | 是否在 OCR 结果中附带调试图像 base64，开启后日志和响应会更重 |
| `TENCENT_OCR_TIMEOUT_SECONDS` | `6` | 单次 OCR worker 超时秒数 |
| `TENCENT_OCR_OPENCV_THREADS` | `1` | 每个 OCR worker 内 OpenCV 线程数，建议保持 `1`，避免多进程并发时线程爆炸 |
| `TENCENT_OCR_ONNX_THREADS` | `1` | 每个 OCR worker 内 ONNXRuntime 推理线程数，建议保持 `1`，多 worker 并发时更稳 |

补充说明：

- `BROWSER_IMPERSONATE` 现在主要是全局兜底 profile 和 transport 展示值
- 真正运行时优先用账号自己的 `browser_impersonate`
- 账号级 `browser_impersonate` 在首次导入账号时随机分配为 `chrome146 / chrome145 / edge146 / firefox149`
- 历史账号里的 `chrome / edge / firefox / chrome124 / chrome136 / firefox137 / firefox147` 会自动映射到当前支持的具体 profile
- `BOOTSTRAP_FINGERPRINT_MAX_RETRIES` 小于 `1` 时会自动按 `1` 处理，避免配置错误导致完全不尝试
- 如果你把 `TENCENT_OCR_WORKERS` 配得太高，OCR 并发会更猛，但内存占用也会跟着往上窜，别一上来就梭哈
- ticket 池发射间隔不再读取 `.env`，在 Web 端按账号设置；`0ms` 并行，大于 `0ms` 串行

## 已知说明

- 上游 `batch-preview`、`create-sign`、`update/sign` 偶发会返回 `555 / 系统繁忙`
- 页面里的错误提示和账号状态会保留最近一次失败原因，方便复盘
- 正式页面不会单独落盘二维码 PNG 文件，只在 `tasks.json` 中保存 `qr_base64`

## 关键文档

- `glm-coding-new-purchase-field-map.md`
- `glm-coding-new-purchase-detailed-spec.md`
- `glm-coding-tencent-captcha-verify.md`

## 鸣谢

感谢 [Linux.do](https://linux.do) 社区，本项目在开发与调试过程中受益于社区内众多开发者分享的技术思路与讨论氛围。

## 开源协议

本项目基于 [MIT License](./LICENSE) 开源，详情见仓库根目录的 `LICENSE` 文件。

## 免责声明

本项目仅供学习、研究和技术交流使用，下载和使用本项目即视为您已阅读并同意以下内容：

- 本项目按"现状"提供，不提供任何形式的明示或暗示担保，使用者需自行承担全部风险
- 使用者应自行确保使用行为符合当地法律法规及所涉及服务的相关条款；因使用本项目引发的任何账号封禁、财产损失或法律责任，由使用者本人承担，与作者无关
- 本项目不鼓励、不支持任何用于商业牟利或违反服务条款的自动化操作
- 若本项目侵犯了任何第三方的合法权益，请提供相关证明联系作者，确认后会及时处理
- 作者不对本项目的可用性、准确性或适用性作任何保证，且无义务对使用者提供后续维护或支持

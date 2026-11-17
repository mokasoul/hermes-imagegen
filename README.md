# hermes-imagegen

**在 Hermes Desktop 里生成、编辑、管理图片，一站完成。**

`hermes-imagegen` 是把 [dickpy/dsh-imagegen](https://github.com/dickpy/dsh-imagegen)
（DeepSeek Harness 生图插件，v1.18.1，Apache-2.0）移植到 Hermes 平台的项目：
一个统一包，桌面端提供工作台，后端在网关进程里直连你自己配置的 OpenAI 兼容生图渠道。

> 状态：**进行中**。后端（渠道/模型探测/文生图/图生图/异步任务/历史）已完成并有测试；
> 桌面工作台 UI 在 PR2，历史与画廊在 PR3。详细的移植范围见 [NOTICE](NOTICE)。

## 现在能做什么

| 能力 | 状态 |
|---|---|
| 渠道配置（多提供方、密钥只存服务端、支持 `api_key_env`） | ✅ |
| 模型探测（同时探 `<base>/models` 与 `<base>/models?kind=image`） | ✅ |
| 文生图 `images/generations` | ✅ |
| 图生图 `images/edits`（多参考图 + PNG 遮罩尺寸校验） | ✅ |
| 按模型族塑形请求体（gpt-image / dall-e / grok / nanobanana / seedream / sensenova / zhipu） | ✅ |
| 三种非 OpenAI 协议：Chat Completions（含 markdown/HTML/裸链/base64 图片抽取与 `<think>` 剥离）、DashScope 原生 Qwen（`宽*高`、版本化批量）、MiniMax 原生 `/image_generation`（`base_resp` 判定、`subject_reference` 图生图） | ✅ |
| 异步任务轮询（`task_id` → `GET /tasks/{id}`，含 ModelScope 头） | ✅ |
| 历史记录（SQLite）+ 图片落盘 + 服务端取图 | ✅ |
| 桌面工作台（`/imagegen` 整页 + 侧边栏入口 + ⌘K 命令：提示词、模式、渠道/模型、比例、清晰度、数量、参考图、结果下载/复用、参数记忆） | ✅ |
| 历史 / 画廊（分页浏览、收藏筛选、载入参数、单图下载、删除记录与文件） | ✅ |
| 对话出图工具（`generate_image` / `edit_image` / `get_image_generation_task` / `cancel_image_generation_task`，与工作台共用同一任务队列与历史） | ✅ |
| 模板库（5 个源、2462 条案例，离线快照 + 关键词/分类检索 + 收藏 + 一键回填提示词；参考图按需从源站取并本地缓存，只允许各源固定域名） | ✅ |
| 无限画布、海报文字/Logo 编辑、电商套图、图片转可编辑 PPT、提示词反推、订阅渠道 | ⏳ 进行中 |

后端对每条渠道自动判定协议（`api_url` 以 `/chat/completions` 结尾 → Chat；DashScope 官方域上的 qwen-image → 原生 Qwen；`image-*` → MiniMax 原生；其余走 OpenAI Images 与各模型族塑形），也可在渠道设置里手动指定。判定不出的地址不会被猜着打。

## 安装

```bash
hermes plugins install mokasoul/hermes-imagegen --enable
```

然后在 Hermes Desktop → **Capabilities → Plugins** 里打开 `hermes-imagegen`
（桌面半默认是关闭的，需要你手动开一次），必要时 ⌘K → **Reload desktop plugins**。

装好之后，配置入口是插件自己的工作台：添加渠道 → 填 API 地址与密钥 → 检测并选择模型 → 保存。

## 数据在哪

按 profile 隔离，位于 `$HERMES_HOME/imagegen/`（默认 profile 即 `~/.hermes/imagegen/`）：

```
settings.json      # 渠道配置，模式 0600；API 密钥只存在这里
history.sqlite3    # 生成记录
files/YYYYMMDD/    # 生成结果原图
```

API 密钥永不出现在任何接口响应里（只回显 `…后四位`）；也可以用 `api_key_env`
把密钥放在环境变量里，文件里一个字节都不存。

## 开发

```bash
# venv 必须放在包目录外：这个目录会被整体复制进 ~/.hermes/desktop-plugins/（树上限 64MB）
python3.11 -m venv ../.venv-imagegen
../.venv-imagegen/bin/pip install -r requirements-dev.txt
../.venv-imagegen/bin/python -m pytest tests -q     # 53 项测试，全部离线（MockTransport）
../.venv-imagegen/bin/ruff check .
```

**真实链路自检**（可选，会消耗上游额度，绝不在 CI 跑；数据写临时目录，密钥只从环境变量读）：

```bash
export IMAGEGEN_LIVE_API_URL=https://your-relay.example.com/v1
export IMAGEGEN_LIVE_KEY_ENV=MY_IMAGE_API_KEY   # 环境变量名，密钥本身不落盘
export MY_IMAGE_API_KEY=sk-…
python scripts/live_check.py    # 建渠道 → 检测模型 → 生成一张 → 取图 → 查历史
```

本地联调：把 checkout 软链进插件目录（`linked` 开发模式，保存即热重载，无需 Rescan）：

```bash
ln -s "$PWD" ~/.hermes/plugins/hermes-imagegen
hermes plugins enable hermes-imagegen
```

桌面半仍是 opt-in：Hermes Desktop → Capabilities → Plugins → 打开 `hermes-imagegen`。

目录结构：

```
plugin.yaml                     # 统一包清单（kind: standalone）
__init__.py                     # 必需：插件发现只看含 __init__.py 的目录
dashboard/
  manifest.json                 # { "api": "plugin_api.py" }
  plugin_api.py                 # FastAPI router → /api/plugins/hermes-imagegen/
  imagegen_backend/             # paths / models / settings / engine / store / jobs
desktop/plugin.js               # 桌面半（PR2 提交构建产物）
tests/                          # pytest，全部离线
```

## 许可证

Apache-2.0。上游项目 © dickpy 及贡献者；见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。

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
| 图生图 `images/edits`（多参考图 + PNG 遮罩尺寸校验；渠道可配结果图下载超时 10–600 秒，默认 60） | ✅ |
| 按模型族塑形请求体（gpt-image / dall-e / grok / nanobanana / seedream / sensenova / zhipu） | ✅ |
| 三种非 OpenAI 协议：Chat Completions（含 markdown/HTML/裸链/base64 图片抽取与 `<think>` 剥离）、DashScope 原生 Qwen（`宽*高`、版本化批量）、MiniMax 原生 `/image_generation`（`base_resp` 判定、`subject_reference` 图生图） | ✅ |
| 异步任务轮询（`task_id` → `GET /tasks/{id}`，含 ModelScope 头） | ✅ |
| 上游可靠性（连接失败 / 408 / 429 / 5xx / Cloudflare 520–524 自动重试 3 次指数退避，鉴权与内容审核不重试；并行出图部分成功保留已出图并点明失败；轮询抖动原地重试不重复提交、不重复计费；任务被接受后绝不重发） | ✅ |
| 历史记录（SQLite）+ 图片落盘 + 服务端取图 | ✅ |
| 桌面工作台（`/imagegen` 整页 + 侧边栏入口 + ⌘K 命令：提示词、模式、渠道/模型、比例、清晰度、数量、参考图、结果下载/复用、参数记忆） | ✅ |
| 结果板（按张数固定排版：1 张铺满 / 2 张左右 / 3 张上二下一 / 4 张 2×2 / 5～6 张上三下二三 / 更多三列滚动；图片 `contain` 不裁切；右键或「⋯」出全部操作：查看大图、继续编辑、重新生成、复制图片、反推提示词、下载；角落下载与耗时仅悬停显示；生成中点阵动画；大图左右键翻页） | ✅ |
| 历史 / 画廊（3 列瀑布卡片，图片按 4:5 / 4:3 / 3:4 交错；卡上标签 chips + 行内标签编辑、按标签筛选、右上多选、批量收藏/取消收藏/删除、载入参数、收藏过滤、分页） | ✅ |
| 对话出图工具（`generate_image` / `edit_image` / `get_image_generation_task` / `cancel_image_generation_task`，与工作台共用同一任务队列与历史；`edit_image` 支持 `reference_images` 最多 4 张额外参考图，缺失附件在计费前失败） | ✅ |
| 模板库（5 个源、2462 条案例，离线快照 + 关键词/分类检索 + 收藏 + 一键回填提示词；缩略图网格按上游形态，参考图按需从源站取并本地缓存，只允许各源固定域名） | ✅ |
| 提示词增强 / 润色 / 从参考图反推（走渠道自带文本模型，系统提示词与规则照抄上游；无文本模型时明确报错不假装） | ✅ |
| 无限画布（页面 CRUD + 乐观锁 revision、提示词/配置/图片三类节点、节点连线、拖拽与缩放平移、连线勾选作为生成输入、生成结果自动成新增节点、素材池） | ✅ |
| 海报文字/Logo 编辑（可编辑图层配方 + 画布内实时预览 + 拖拽定位 + 导出 PNG + 保存为新节点并保留可编辑配方；配方在保存时校验，坏配方不会拖到导出才炸） | ✅ |
| 电商套图（主图/卖点/场景/细节/规格/使用六类 slot 预置 + 一致性提示词契约照抄上游 + 参考图分角色上传 + 计划预览逐张改词 + 主图产出后其余图带一致性前缀并挂主图为参考 + 串行生成） | ✅ |
| 多模型对比（同一提示词/参考图/遮罩并发投给多个模型，每卡独立重试、下载、一键切到该模型） | ✅ |
| 图生图遮罩画笔（在参考图上涂抹，涂过处透明=改、其余不透明=保留；PNG 尺寸与首张参考图不一致时后端直接拒绝） | ✅ |
| 订阅渠道（第三方账号登录态）——**按决定不移植**：它的价值完全在 ChatGPT/Grok/Google 的 OAuth 令牌上，机制本身没有独立用途，还会把令牌引进插件目录；加上已决定不做 pptx 产出，上游功能面至此收官 | ✅ 收官 |

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

# YouTube Radar

跟踪指定 YouTube 频道，自动获取带时间戳字幕或利用 Gemini 读取公开视频音画内容，生成结构化中文研究笔记，并同步到本地 Obsidian。支持普通创作者频道与加密货币/美股市场跟踪频道双模式，云端或本地 Docker worker 持续运行。

## 当前能力

- **定时巡检与基线管理**：每 15 分钟检查频道最近 15 条视频；首次加入频道时只将已有视频作为基线记录，不付费回溯分析（可通过命令行按需回溯指定天数或单条视频）。
- **可配置处理延迟**：通过 `RADAR_PROCESS_AFTER_HOURS` 控制新视频发布后等待多久开始抓取（支持设为 `0` 发现新视频后立即处理）。
- **双模式结构化分析**：
  - **普通创作者频道（`channels.txt`）**：按完整字幕拆解核心观点、叙事结构、开头钩子、差异化选题与跨频道主题双链，包含时间点链接与 Mermaid 结构图（精简版式，不展示播放量快照与观众评论板块）。
  - **市场跟踪频道（`market_channels.txt`）**：面向加密货币与美股行情分析频道，提取标的、方向态度、关键支撑/阻力/目标/止损点位及逐字原话依据（自动跳过视频描述区原文抓取以节省 Token）。
- **市场笔记增强视图與跨期追溯**：
  - **顶部核心态度与点位总表**：单篇笔记顶部直接汇总本期涉及标的、看多/看空/震荡态度、核心价位区间（支撑/压力/目标/止损）及置信度。
  - **与前序视频观点对比（连续性追踪）**：自动比对同一博主上一期同标的观点，标记“延续看多 / 延续看空 / 观点反转 / 点位修正 / 首次提及”。
  - **频道历史推演与行情验证追溯表**：在每个市场频道的 `频道索引.md` 中汇总每期方向、关键价位、24h 实际高低点与涨跌幅、是否触及目标/支撑位。
  - **全局《加密博主多空全景看板.md》**：在 `YouTube Radar/加密博主多空全景看板.md` 自动汇总所有市场频道的最新态度、关键点位与近期行情复盘。
- **Gemini 视频音画回退与图表视觉提取**：
  - 视频没有 YouTube 字幕且配置了 `GEMINI_API_KEY` 时，使用 Gemini 直接读取公开视频语音与画面，生成带近似时间点的 AI 内容记录并完成结构化分析；长视频支持分段补读。
  - 市场频道自动定位提及斐波那契、趋势线、支撑阻力等画线的时间窗口，并在配置 `GEMINI_API_KEY` 时调用 Gemini 视觉读取对应片段中的 TradingView K 线价格、斐波那契刻度、手绘水平线/方框区间及鼠标悬停价格标签，补充“嘴上没报数字但画面指了点位”的关键信息。
- **24 小时行情窗口与自动交易所回退**：市场频道记录视频发布后完整的 24 小时行情窗口（最多 30 天）。优先使用 Binance 公开 USDT 永续合约小时 K 线；若当前网络环境访问 Binance 返回 451 等地区限制错误，无论此前是否已有旧记录，均自动回退至 OKX 同类永续合约并在表格中明确标注数据源。
- **Obsidian 目录与同步保护**：每个频道独立一个 Obsidian 文件夹与 `频道索引.md`，笔记以北京时间发布日期和视频标题命名，同目录内保存字幕或 AI 内容记录。同步覆盖前若检测到本地手动修改，会自动备份到 `.youtube-radar/backups/`。

笔记路径示例：`YouTube Radar/示例频道/2026-10-18-示例视频标题.md`。同名视频会自动加 `(2)` 等序号避免覆盖。YouTube 视频 ID 保存在笔记元数据中，用于去重和生成时间点跳转链接。

## 密钥与配置

复制 `.env.example` 为 `.env`，填写以下配置。`.env` 已被 Git 忽略；切勿把密钥提交到仓库。普通创作者频道写在 [`channels.txt`](channels.txt)，市场跟踪频道写在 [`market_channels.txt`](market_channels.txt)，每行一个频道链接或 `@Handle`，启动时自动导入。

| 变量 | 用途 |
|---|---|
| `TRANSCRIPT_API_KEY` | [TranscriptAPI](https://transcriptapi.com/docs/api/) 的密钥，用于频道更新检查与字幕获取。频道最新视频列表接口不扣积分（账户需保持有效积分），每次成功下载字幕按服务商计费。 |
| `YOUTUBE_API_KEY` | 可选；Google Cloud 项目中启用了 YouTube Data API v3 的 API Key，用于普通创作者频道的描述区元数据获取。 |
| `GEMINI_API_KEY` 或 `gemini_API_KEY` | 可选；在无字幕时读取公开视频语音与画面生成内容记录，以及在市场频道中视觉识别视频图表片段上的 K 线价格、画线区间与斐波那契刻度。 |
| `GEMINI_VIDEO_MODEL` | Gemini 视频模型，默认 `gemini-2.5-flash`；更换模型前需确认其支持 YouTube URL 视频输入。 |
| `LLM_PROVIDER` | `openai` 或 `deepseek`，用于字幕文本与市场观点的结构化提取。 |
| `LLM_API_KEY` / `LLM_MODEL` | 对应供应商的密钥和具体模型名。 |
| `RADAR_PROCESS_AFTER_HOURS` | 视频发布后等待多少小时开始处理，设为 `0` 表示发现新视频后立即处理。 |
| `RADAR_SYNC_TOKEN` | 自定义随机长令牌（至少 24 字符），云端和本地须一致。可用 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成。 |
| `RADAR_DOMAIN` | 云端域名，用于 Caddy 自动配置 HTTPS。 |
| `OBSIDIAN_VAULT` | 本机 Obsidian 仓库的绝对路径；在 `.env` 或 `local.env` 中填写。 |
| `RADAR_SERVER_URL` | 同步服务端地址，例如 `https://你的域名` 或本机 Docker `http://127.0.0.1:18765`。 |

常用维护与回填命令：
- `python3 -m radar backfill-market-views`：从本地已保存的字幕或 AI 内容记录重新提取市场观点与逐字证据，不重复下载字幕。
- `python3 -m radar backfill-market-charts`：补充图表相关原话时间段，并在配置了 `GEMINI_API_KEY` 时执行 Gemini 图表画面点位识别。
- `python3 -m radar refresh-market-data`：补齐已结束的 24 小时行情窗口数据并重新渲染笔记与全局看板。

OpenAI 使用官方 [Responses API](https://developers.openai.com/api/docs/guides/text)；DeepSeek 使用官方 [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。没有密钥时可运行本地单元测试，但无法进行真实采集和分析。

## 本地试运行

要求 Python 3.11+，无第三方 Python 包依赖。先在 shell 中加载 `.env`：

```sh
set -a
source .env
set +a
python3 -m radar add-channel 'https://www.youtube.com/@ExampleCreator'
python3 -m radar list
python3 -m radar run-once
```

想验证一条指定视频，可运行 `python3 -m radar add-video VIDEO_ID`，再运行 `python3 -m radar run-once`。设置字幕服务密钥后可运行 `python3 -m radar check-channels`，确认频道链接可正常解析（仅访问频道最近视频接口，不调用 AI，也不下载字幕）。

## 本机 Docker 部署与验收

复制 `.env.example` 为 `.env`，填写字幕服务、模型密钥、模型名和本机 `OBSIDIAN_VAULT` 绝对路径。启动 worker 和 Obsidian 同步服务，并将同步 API 限制在本机端口：

```sh
docker compose -f compose.yaml -f compose.local.yaml up -d --build radar sync
curl http://127.0.0.1:18765/health
docker compose -f compose.yaml -f compose.local.yaml exec radar python -m radar check-channels
docker compose -f compose.yaml -f compose.local.yaml logs --tail 50 radar
docker compose -f compose.yaml -f compose.local.yaml logs --tail 20 sync
```

要用一条已有视频验证完整的字幕、AI 分析与笔记流程，可运行：

```sh
docker compose -f compose.yaml -f compose.local.yaml exec radar python -m radar add-video VIDEO_ID
docker compose -f compose.yaml -f compose.local.yaml exec radar python -m radar run-once
```

`sync` 容器启动时立即同步一次，此后每 15 分钟自动同步到 Obsidian；手动触发同步也可执行 `zsh scripts/run_sync.sh`（将 `local.env` 的 `RADAR_SERVER_URL` 设为 `http://127.0.0.1:18765`，令牌与 `.env` 保持一致）。停止本机服务可执行 `docker compose -f compose.yaml -f compose.local.yaml down`，`data/` 数据目录会保留。

## 云端常驻

在有公网 IP、域名解析和 Docker Compose 的服务器上，复制代码与 `.env`，运行：

```sh
docker compose up -d --build
docker compose exec radar python -m radar add-channel 'https://www.youtube.com/@ExampleCreator'
docker compose logs -f radar
```

开放 80/443 端口供 Caddy 申请证书。`data/` 保存 SQLite 数据库、字幕和生成记录，须持久化并定期备份。`/health` 可做健康检查。同步 API 校验 `RADAR_SYNC_TOKEN`，请勿直接暴露容器的 8765 端口。

本机复制 `local.env.example` 为 `local.env`，只填写云端 URL 和同步令牌（本机无需保存字幕服务或 AI 密钥）。手动同步一次：

```sh
zsh scripts/run_sync.sh
```

macOS 自动同步模板位于 `scripts/com.youtube-radar.sync.plist.in`。`local.env` 填好后，可生成适用于当前项目路径的用户 LaunchAgent：

```sh
python3 scripts/install_macos_sync.py
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.youtube-radar.sync.plist"
```

## 重要边界

- **字幕可用性**：YouTube 自动字幕可能延迟或不可用；当字幕服务返回无字幕且配置了 `GEMINI_API_KEY` 时会自动启用 Gemini 视频音画回退，否則会在重试窗口结束后标记为 `unavailable`。
- **行情核对与免责声明**：行情窗口记录公开交易所（Binance / OKX）的小时 K 线价格，仅用于客观对照视频发布后的市场波动与高低点触及情况，不代表博主实际使用的交易平台或合约，也不构成任何交易建议。
- **图表视觉提取校验**：Gemini 图表画面识别会先判断对应时间窗内是否存在真实价格图表（若画面仅为静态封面或头像则跳过），但模型识别出的画面刻度与手绘线价格仍建议点击时间戳链接回看原视频确认。
- **本地编辑备份**：自动同步更新已存在的笔记时，如果检测到本地修改过该文件，会先将旧版本备份至 vault 的 `.youtube-radar/backups/`，再写入新版。个人交易复盘建议写在独立笔记中并通过双链引用自动笔记。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

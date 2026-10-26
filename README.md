# YouTube Radar

跟踪指定 YouTube 频道，在视频发布约 3 小时后取得带时间戳字幕，生成中文研究笔记，并在本机开机时同步到 Obsidian。云端 worker 持续运行；本地 vault 不必在云端挂载。

## 当前能力

- 每 15 分钟检查频道最近 15 条视频；首次加入时只将已有视频作为基线，不付费回溯分析。
- 视频达到设定年龄后取字幕，按完整字幕拆解观点、结构、钩子和差异化选题。长视频分段处理。
- 每个频道各有一个 Obsidian 文件夹和“频道索引”，即使还没有新视频笔记也能看到该频道；笔记以北京时间的发布日期和视频标题命名，同频道内保存完整字幕和封面图片。笔记包含时间点链接、Mermaid 结构图、播放量快照及资料局限。
- 自动重试暂时缺少字幕的内容；48 小时后仍无字幕则标记 unavailable。每日成功分析数量有上限。
- 云端通过带令牌的只读 HTTPS API 提供文件，本地同步程序写入 Obsidian vault。

笔记路径示例：`YouTube Radar/Seven科技生活/2026-10-18-18个月 Google AI Pro 只需几块钱 到底靠不靠谱 实测与原理分析.md`。同名视频会自动加 `(2)` 等序号避免覆盖。YouTube 视频 ID 仍保存在笔记元数据中，用来去重和生成时间点链接。旧版以视频 ID 命名的文件会自动迁移；只有内容与原始文件完全一致的旧文件会被清理，你自己修改过的旧文件会保留。

## 密钥与配置

复制 `.env.example` 为 `.env`，填写以下值。`.env` 已被 Git 忽略；不要把密钥提交到仓库或贴进聊天。当前三个频道已写在 [`channels.txt`](channels.txt) 中，云端启动时自动导入；以后每行增删一个链接即可。

| 变量 | 用途 |
|---|---|
| `TRANSCRIPT_API_KEY` | [TranscriptAPI](https://transcriptapi.com/docs/api/) 的密钥，用于频道更新与字幕。频道最新视频接口不扣积分，但账户仍需至少有一笔有效积分；每次成功取字幕按服务商计费。 |
| `LLM_PROVIDER` | `openai` 或 `deepseek`。 |
| `LLM_API_KEY` / `LLM_MODEL` | 对应供应商的密钥和具体模型名。模型名称由你填写，避免代码内名称过期。 |
| `RADAR_SYNC_TOKEN` | 自己生成的随机长令牌，至少 24 字符；云端和本地须相同。可用 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成。 |
| `RADAR_DOMAIN` | 云端域名，用于 Caddy 自动配置 HTTPS。 |
| `OBSIDIAN_VAULT` | 本机 Obsidian 仓库的绝对路径；在 `local.env` 填写即可。 |
| `RADAR_SERVER_URL` | `https://你的域名`。 |

OpenAI 使用官方 [Responses API](https://developers.openai.com/api/docs/guides/text)；DeepSeek 使用官方 [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。只支持你主动选择并配置的模型。没有密钥时可运行本地单元测试，但无法进行真实采集和分析。

## 本地试运行

要求 Python 3.11+，无第三方 Python 包依赖。先在 shell 中加载 `.env`：

```sh
set -a
source .env
set +a
python3 -m radar add-channel 'https://www.youtube.com/@Example'
python3 -m radar list
python3 -m radar run-once
```

想验证一条旧视频，可运行 `python3 -m radar add-video VIDEO_ID`，再运行 `run-once`。它会发生字幕服务及模型调用费用。未配置密钥时，不会发起付费请求。

目前已收录 `@huangsiping`、`@7techlife`、`@sev7enshare`。设置字幕服务密钥后，先运行 `python3 -m radar check-channels`，确认三个链接都能解析。这个命令只访问频道最近视频接口，不调用 AI，也不下载字幕。

## 本机 Docker 验收

先复制 `.env.example` 为 `.env`，填写字幕服务、模型密钥、模型名和本机 `OBSIDIAN_VAULT` 绝对路径。首次启动会把三个频道已有视频记录为基线，之后只自动分析新发布的视频。本机启动 worker 和 Obsidian 同步服务，并把同步 API 限制在本机端口：

```sh
docker compose -f compose.yaml -f compose.local.yaml up -d --build radar sync
curl http://127.0.0.1:18765/health
docker compose -f compose.yaml -f compose.local.yaml exec radar python -m radar check-channels
docker compose -f compose.yaml -f compose.local.yaml logs --tail 50 radar
docker compose -f compose.yaml -f compose.local.yaml logs --tail 20 sync
```

要用一条已有视频验证完整的字幕、AI 分析与笔记流程，可运行 `docker compose -f compose.yaml -f compose.local.yaml exec radar python -m radar add-video VIDEO_ID`，然后运行 `docker compose -f compose.yaml -f compose.local.yaml exec radar python -m radar run-once`。手动加入会把基线中被忽略的同一视频重新排队；这一步会产生 API 费用。

`sync` 容器启动时同步一次，此后每 15 分钟同步 Obsidian；手动补同步仍可执行 `zsh scripts/run_sync.sh`（需将 `local.env` 的 `RADAR_SERVER_URL` 设为 `http://127.0.0.1:18765`，令牌与 `.env` 保持一致）。验收结束后可运行 `docker compose -f compose.yaml -f compose.local.yaml down` 停止本机服务；`data/` 不会被删除。

## 云端常驻

在有公网 IP、域名解析和 Docker Compose 的服务器上，复制代码与 `.env`，运行：

```sh
docker compose up -d --build
docker compose exec radar python -m radar add-channel 'https://www.youtube.com/@Example'
docker compose logs -f radar
```

开放 80/443 端口供 Caddy 申请证书。`data/` 是 SQLite 数据、字幕和笔记生成记录，须持久化并备份。`/health` 可做健康检查。同步 API 要求 `RADAR_SYNC_TOKEN`；不要直接暴露容器的 8765 端口。

本机复制 `local.env.example` 为 `local.env`，只填写云端 URL 和与云端一致的同步令牌。本机无需保存字幕服务或 AI 密钥。手动同步一次：

```sh
zsh scripts/run_sync.sh
```

Mac 自动同步模板已放在 `scripts/com.youtube-radar.sync.plist.in`。`local.env` 填好后，生成适用于当前项目路径的用户 LaunchAgent：

```sh
python3 scripts/install_macos_sync.py
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.youtube-radar.sync.plist"
```

登录后立即同步一次，此后每 15 分钟同步；电脑关机期间云端继续采集，开机登录后会补齐文件。脚本使用 `/opt/homebrew/bin/python3`；若本机 Python 路径不同，修改 `scripts/run_sync.sh`。项目移动后需重新生成 LaunchAgent。

## 重要边界

- 3 小时是**开始尝试取字幕**的时间，不是结果保证。YouTube 自动字幕可能延迟或一直不可用；失败会在 `python3 -m radar list` 中显示。见 [YouTube 自动字幕说明](https://support.google.com/youtube/answer/6373554)。
- 官方 YouTube 字幕下载接口要求对视频有编辑权限，因此此工具使用第三方字幕服务；服务的覆盖率、价格与可用性需要用你指定的真实频道验证。见 [官方接口权限](https://developers.google.com/youtube/data/reference/rest/v3/captions/download)。
- 目前记录播放量快照，但**不自动称某条视频为“爆款”**。需要积累同频道、同类型、相同发布时长的样本后才适合计算相对热度。
- AI 分析依据字幕；视频画面中的图表、操作演示和封面之外的视觉信息暂未解析。
- 当前是可部署的命令行工具，未提供桌面图形界面。三个频道与单条视频的完整流程已在本机验证；云端部署时仍需在服务器配置密钥，并将本机同步 URL 指向云端域名。

## 测试

```sh
python3 -m unittest discover -s tests -v
```


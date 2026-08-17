# 网页端 GPT 集成（gpt_web.py）

让本机自动化能操作网页端**已登录**的 ChatGPT：发消息、读取回复。
用于 coding agent 在任务执行中遇到疑问时，直接向 GPT 提问并依据回复调整工作流程。

## 版本说明（保底仓库）

| 文件 | 说明 |
|---|---|
| `gpt_web.py` | **当前最新版**：ask（可指定会话）/login、后台最小化（屏幕外启动无闪现）、等待完整回复（Stop 按钮硬闸门）、指定会话用侧边栏扫描 + href 精确定位（找不到报错并列出会话，绝不新建） |
| `gpt_web_v1_initial_20260816.py` | **初始版（2026-08-16）**：仅 ask（新建会话）/login、无指定会话功能、纯文本稳定判定等待回复 |
| `gpt_web_session.py` | **指定会话版（2026-08-18）**：`list` 列出侧边栏会话；`ask --title` 在既有会话提问（侧边栏扫描 + href 精确定位 + 找不到报错，绝不新建会话） |
| `read_reply.py` | 只读回复的辅助脚本 |
| `ask_with_file.py` / `extract_task_card.py` | 附加脚本 |

> ⚠️ `gpt_profile/`（登录会话档案，含 ChatGPT cookie）已被 `.gitignore` 排除，**严禁**上传到任何仓库。

## 前置条件

- Windows + 系统 Chrome（脚本用 `channel="chrome"` 复用系统浏览器内核）
- Python 3 + playwright（`python -m pip install playwright`）

## 首次使用：登录一次

```powershell
python gpt_web.py login            # 弹出 Chrome，手动登录 ChatGPT，自动检测并保存会话
```

- 会话档案保存在 `./gpt_profile`，之后复用，**不碰系统浏览器**。
- 若长时间未使用，会话可能过期，重新执行 `login` 即可。

## 提问

```powershell
python gpt_web.py ask "你的问题"
python gpt_web.py ask "长任务问题" --timeout 300   # 生成超时上限（秒）
python gpt_web.py ask "无头模式" --headless         # 不弹窗（风控更严，默认有头）
```

退出码：0 = 成功（stdout 为 GPT 回复正文）；2 = 空问题；3 = 未登录；
4 = 找不到输入框（页面结构变化）；5 = 超时无回复。

## agent 工作流中的调用约定

- 提问前先检查 `Test-Path .\gpt_profile` 判断是否已登录；未登录先跑 `login`。
- 调用示例（PowerShell 捕获回复）：

```powershell
$reply = python gpt_web.py ask "我的问题" 2>$null
if ($LASTEXITCODE -eq 0) { Write-Output $reply }
```

- 建议：把要问的问题先明确写好（含必要的上下文），一次问清，降低请求频率，减少风控风险。

## 注意事项与风险

- 网页版 ChatGPT 有反自动化风控：高频/无头操作可能触发临时限制（OpenAI 账号层面），
  请控制调用频率、优先有头模式。
- 依赖页面 DOM 结构（`#prompt-textarea`、`[data-message-author-role="assistant"]` 等），
  若 OpenAI 改版导致找不到元素，会返回退出码 4，需要同步更新选择器。
- 本工具只做「发消息 + 读回复」，不做任何绕过验证、批量灌水等滥用行为。

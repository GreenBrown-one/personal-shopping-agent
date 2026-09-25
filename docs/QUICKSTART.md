# 快速开始与安装验收

本文面向第一次在电脑上下载和运行项目的用户。完成本页后，可以启动安全的默认 MCP 服务，也可以生成必须主动确认的京东配置；不能把“服务已启动”或“配置已生成”误解为真实平台已经验收通过。

## 1. 当前适用范围

默认服务适合：

- 让支持本地 stdio MCP 的 AI 宿主理解自然语言，先用 `review_shopping_request` 审查需求并追问缺口，再创建结构化购物任务；
- 查询本地工作流状态；
- 读取已经生成的确定性 Markdown / HTML 报告；
- 在明确配置 OpenAI 或 DeepSeek 后请求可选解释。

默认服务不采集真实京东页面。独立京东入口会显式组合受控浏览器、限频器、京东解析器和端到端管线，默认关闭且仍需电脑端真实环境验收。

## 2. 下载项目

仓库为私有仓库，下载者必须先获得 GitHub 访问权限。

### 方法 A：下载 ZIP

在 GitHub 仓库页面选择 **Code → Download ZIP**，解压后进入 `personal-shopping-agent` 目录。该方式最直观，但以后更新需要重新下载。

### 方法 B：Git 克隆

```bash
git clone https://github.com/GreenBrown-one/personal-shopping-agent.git
cd personal-shopping-agent
```

该方式保留提交历史，方便以后执行 `git pull`、切换分支或让维护型 AI 审查 PR。

## 3. 安装 uv

项目只要求先安装 `uv`；它会根据 `.python-version` 准备 Python 3.12 和隔离环境。安装方式以 [uv 官方文档](https://docs.astral.sh/uv/getting-started/installation/) 为准。

安装后确认：

```bash
uv --version
```

## 4. 一条命令初始化

在项目根目录运行：

```bash
uv run --locked python scripts/bootstrap.py
```

它只组合已经受测试的正式命令：

1. `health`：确认安装版本；
2. `migrate`：创建或向前迁移本地 SQLite；
3. `doctor`：确认数据库和权限已经就绪。

最后一行应类似：

```json
{"command":"bootstrap","ok":true,"service":"personal-shopping-agent"}
```

**同时写入 Claude Desktop（Windows / macOS）：**

```bash
uv run --locked python scripts/bootstrap.py --claude-desktop             # 默认离线模式
uv run --locked python scripts/bootstrap.py --claude-desktop --live-jd   # 真实京东模式
uv run --locked personal-shopping-agent install-claude-desktop --dry-run # 只预览，不写入
```

安装器只写入 Claude Desktop 配置中的 `personal-shopping-agent` 条目，其他服务器和设置保持不变；内容有变化时
先在同目录创建 `claude_desktop_config.json.backup-<时间>` 备份再写入，已有文件损坏时拒绝修改。它使用当前
`uv` 的绝对路径，避免 Claude Desktop 找不到 `uv`。Windows 用户也可以直接双击仓库根目录的
`install-windows.cmd` 完成全部步骤。

默认数据库位于 `data/personal-shopping-agent.db`。不要把 `data`、浏览器资料、截图、导出文件或模型密钥提交到 Git。

## 5. 启动 MCP

先生成不含密钥的通用配置：

```bash
uv run --locked personal-shopping-agent mcp-config
```

返回 JSON 的 `config` 字段可以复制到支持本地 stdio MCP 的宿主。命令会解析当前项目的绝对路径，因此通常不再需要手工编辑路径。

直接测试启动：

```bash
uv run --locked personal-shopping-agent-mcp
```

stdio MCP 会等待宿主通信，因此终端没有普通交互提示是正常现象。按 `Ctrl+C` 可停止。

生成的默认配置采用以下形状；配置文件位置由具体宿主决定：

```json
{
  "mcpServers": {
    "personal-shopping-agent": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/personal-shopping-agent",
        "run",
        "--locked",
        "personal-shopping-agent-mcp"
      ],
      "env": {
        "PERSONAL_SHOPPING_LLM_PROVIDER": "disabled"
      }
    }
  }
}
```

Windows 可以使用正斜杠路径，例如 `C:/Users/name/personal-shopping-agent`。必须改成实际绝对路径。

**推荐宿主：Claude Desktop（Windows / macOS）。** 打开 设置 → 开发者 → 编辑配置，把 `mcp-config` 输出中
`config` 里的 `mcpServers` 块合并进 `claude_desktop_config.json`（Windows 位于
`%APPDATA%\Claude\`），保存后完全退出并重新打开 Claude Desktop。若提示找不到 `uv`，安装 uv 后重启电脑
让 PATH 生效。购物对话建议在 Claude Desktop 中进行；修改本项目代码请在单独的开发会话（如 Claude Code）
中进行，避免网页内容与代码修改权限出现在同一个会话里（见 `DESIGN.md` 3.1）。浏览器型 AI 宿主如果不能启动本地进程，则需要额外的受控远程 MCP 网关；本仓库目前不提供该网关。

连接后先让 AI 调用 `shopping_agent_status`。默认结果应明确显示：需求审查、本地工作流和报告可用，而 `platform_collection` 与 `end_to_end_pipeline` 为 `false`。

创建任务前，AI 应调用 `review_shopping_request`。它不存储、不联网，只按评分规则指出问题：例如条件缺少
最小/最大值方向、使用了评分无法识别的键（如“内存”应写作 `memory_capacity`）或单位与规范单位不一致。
存在阻断问题时 `start_shopping_workflow` 会直接拒绝，避免在真实采集后才发现需求无法评分。

### 显式京东模式与首次真实测试

京东网页版现在要求登录后才能搜索。以下步骤全部在你自己的电脑上、在项目目录中执行：

1. 安装浏览器（只需一次）：

   ```bash
   uv run --locked playwright install chromium
   ```

2. 亲自登录京东（登录失效后重复这一步）：

   ```bash
   uv run --locked personal-shopping-agent login jd
   ```

   会打开一个可见的浏览器窗口，由你本人输入账号并完成验证；完成后回到终端按回车关闭。程序不读取、
   不保存密码，登录状态只保存在本机私有目录 `data/browser-profiles/`（已被 Git 忽略）。

3. 生成京东 MCP 配置并替换宿主中的默认配置，然后重启 AI 宿主：

   ```bash
   uv run --locked personal-shopping-agent mcp-config --live-jd
   ```

   该配置选择 `personal-shopping-agent-mcp-jd`，显式设置 `PERSONAL_SHOPPING_ENABLE_LIVE_JD=true`，
   并默认 `PERSONAL_SHOPPING_JD_HEADLESS=false`，让你能看到每一个被打开的页面并随时介入。

4. 在 AI 宿主中依次让它：调用 `shopping_agent_status`（确认 `platform_collection=true`）→ 用你的原话
   描述需求并调用 `review_shopping_request` 直到没有阻断问题 → `start_shopping_workflow` →
   `run_shopping_pipeline`（首次建议 `maximum_candidates=10`、`maximum_details=3`）→ `get_shopping_report`。

**可选：芯片性能评分。** 京东规格里的芯片型号（如“CPU型号：第三代骁龙8”）可以换算成极客湾 SOCPK
综合性能分（CPU 70%、GPU 30%，骁龙 865 = 100）参与评分：

```bash
uv run --locked personal-shopping-agent benchmark refresh   # 读取一次公开排行页，建议每月最多一次
uv run --locked personal-shopping-agent benchmark show --filter 骁龙8
```

`refresh` 使用独立的无界面浏览器资料，只读取页面渲染后的表格，结果只保存在本机私有文件
`data/benchmarks/socpk-allperf.json`，不进入 Git。输出中的 `suggested_criterion` 是“越高越好”的加分项：
骁龙 865 基准（100 分）得半分，榜单最高分得满分，不排除任何候选。刷新后需要重启 AI 宿主，
`shopping_agent_status` 中 `chip_benchmark=true` 表示已启用。芯片未收录或名称不能精确对应时，该项按缺失
处理并在报告中显示，不做猜测。

页面自身的脚本、图片和价格数据只允许来自 `jd.com`、
`360buyimg.com`、`3.cn`；同一主机两次访问至少间隔 3 秒；交易路径、下载和私网地址始终被阻止。
遇到以下情况系统会停止而不是绕过：

| 返回 | 含义与处理 |
|---|---|
| `sign_in_required` | 登录已失效，重新运行 `login jd` 后再次调用 `run_shopping_pipeline`，会从上次成功阶段继续 |
| `jd_access_restricted`、403、429 | 京东要求验证或限流；停止使用一段时间，必要时在浏览器中手动处理，不要提高频率 |
| 解析失败或没有候选 | 页面结构可能已变化；运行 `improvement-case` 生成脱敏案例并反馈 |

真实页面结构只能在你的电脑上验收。首次运行后请人工核对报告中的价格与商品页面是否一致。

## 6. 可选模型解释

OpenAI 和 DeepSeek 只解释已经生成的确定性报告，不负责浏览、评分或修改软件。默认保持关闭最安全。

OpenAI：

```text
PERSONAL_SHOPPING_LLM_PROVIDER=openai
OPENAI_API_KEY=<runtime secret>
PERSONAL_SHOPPING_OPENAI_MODEL=<explicit model name>
```

DeepSeek：

```text
PERSONAL_SHOPPING_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=<runtime secret>
PERSONAL_SHOPPING_DEEPSEEK_MODEL=<explicit model name>
```

不要把真实密钥写进仓库、Issue、聊天记录或示例文件。ChatGPT 订阅也不等于 OpenAI API 额度。

## 7. 常用管理命令

```bash
uv run personal-shopping-agent health
uv run personal-shopping-agent doctor
uv run personal-shopping-agent migrate
uv run personal-shopping-agent data export <WORKFLOW_UUID> --output workflow-export.json
uv run personal-shopping-agent data delete <WORKFLOW_UUID>
uv run personal-shopping-agent improvement-case <WORKFLOW_UUID> --error-code <stable_error_code>
uv run personal-shopping-agent login jd
uv run personal-shopping-agent benchmark refresh
uv run personal-shopping-agent benchmark show --filter <芯片名>
```

删除命令第一次只生成预览和确认 token，不会立即删除。完整隐私与权限规则见 [`SECURITY.md`](../SECURITY.md)。

`improvement-case` 只在终端打印一份脱敏改进案例预览：软件版本、停在哪个阶段、稳定错误码和需求结构
摘要（条件数量、受支持的规范键等），不包含查询文字、品类、地区、金额、商品、卖家或链接，也不写文件、
不上传。是否把它粘贴到私有 Issue 交给维护者或维护型 AI，由你决定。

## 8. 故障排查

| 现象 | 处理 |
|---|---|
| 找不到 `uv` | 重新安装 uv，并重启终端以刷新 PATH |
| `doctor` 返回数据库未就绪 | 运行 `uv run personal-shopping-agent migrate` |
| MCP 启动后没有普通文字界面 | 正常；它在等待 MCP 宿主通过 stdio 通信 |
| AI 看不到 `run_shopping_pipeline` | 默认组合故意未开启真实平台采集 |
| `start_shopping_workflow` 提示需要澄清 | 先调用 `review_shopping_request`，按每条建议向用户确认后重新提交 |
| 京东模式启动即退出 | 确认显式开关、数据库迁移和 Playwright Chromium 已完成 |
| 返回 `sign_in_required` | 运行 `uv run --locked personal-shopping-agent login jd` 重新登录后重试 |
| 京东返回验证码、403 或 429 | 停止自动访问并由用户处理；不要增加绕过逻辑 |
| 模型解释回退到原报告 | 检查提供方、模型名、API 密钥和网络；排名不受影响 |
| Chromium 未安装 | 只有显式配置真实采集时才需要运行 `uv run playwright install chromium` |

## 9. 易用性结论

- **下载和本地初始化：较容易。** ZIP 或 Git 克隆后，一条 `uv run --locked ...bootstrap.py` 命令即可完成依赖、数据库和就绪检查。
- **开发、测试和 AI 审计：容易。** 锁定依赖、清晰分层、完整类型检查、离线 fixture 和发布验收均已具备。
- **普通用户直接完成实时购物比较：仍需验收。** 显式京东入口和配置生成已经完成，但真实页面、登录状态、图形界面/安装包仍未完成电脑端验收。

在这些缺口补齐前，不应把当前版本宣传成“下载安装即可自动比价”的成品。

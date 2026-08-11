# 快速开始与安装验收

本文面向第一次在电脑上下载和运行项目的用户。完成本页后，可以启动安全的默认 MCP 服务；真实京东采集仍需后续显式配置，不能把“服务已启动”误解为“已经可以自动浏览平台”。

## 1. 当前适用范围

默认服务适合：

- 让支持本地 stdio MCP 的 AI 宿主理解自然语言并创建结构化购物任务；
- 查询本地工作流状态；
- 读取已经生成的确定性 Markdown / HTML 报告；
- 在明确配置 OpenAI 或 DeepSeek 后请求可选解释。

默认服务暂时不能直接采集真实京东页面。内部端到端管线只有在开发者注入受安全策略约束的页面采集器和官方证据提供器时才会出现。

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

默认数据库位于 `data/personal-shopping-agent.db`。不要把 `data`、浏览器资料、截图、导出文件或模型密钥提交到 Git。

## 5. 启动 MCP

直接测试启动：

```bash
uv run --locked personal-shopping-agent-mcp
```

stdio MCP 会等待宿主通信，因此终端没有普通交互提示是正常现象。按 `Ctrl+C` 可停止。

支持本地 stdio MCP 的宿主通常接受以下配置形状；配置文件位置由具体宿主决定：

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

Windows 可以使用正斜杠路径，例如 `C:/Users/name/personal-shopping-agent`。必须改成实际绝对路径。浏览器型 AI 宿主如果不能启动本地进程，则需要额外的受控远程 MCP 网关；本仓库目前不提供该网关。

连接后先让 AI 调用 `shopping_agent_status`。默认结果应明确显示：本地工作流和报告可用，而 `platform_collection` 与 `end_to_end_pipeline` 为 `false`。

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
```

删除命令第一次只生成预览和确认 token，不会立即删除。完整隐私与权限规则见 [`SECURITY.md`](../SECURITY.md)。

## 8. 故障排查

| 现象 | 处理 |
|---|---|
| 找不到 `uv` | 重新安装 uv，并重启终端以刷新 PATH |
| `doctor` 返回数据库未就绪 | 运行 `uv run personal-shopping-agent migrate` |
| MCP 启动后没有普通文字界面 | 正常；它在等待 MCP 宿主通过 stdio 通信 |
| AI 看不到 `run_shopping_pipeline` | 默认组合故意未开启真实平台采集 |
| 模型解释回退到原报告 | 检查提供方、模型名、API 密钥和网络；排名不受影响 |
| Chromium 未安装 | 只有显式配置真实采集时才需要运行 `uv run playwright install chromium` |

## 9. 易用性结论

- **下载和本地初始化：较容易。** ZIP 或 Git 克隆后，一条 `uv run --locked ...bootstrap.py` 命令即可完成依赖、数据库和就绪检查。
- **开发、测试和 AI 审计：容易。** 锁定依赖、清晰分层、完整类型检查、离线 fixture 和发布验收均已具备。
- **普通用户直接完成实时购物比较：尚不够容易。** 真实京东采集组合、AI 宿主配置向导、图形界面/安装包和真实环境验收仍未完成。

在这些缺口补齐前，不应把当前版本宣传成“下载安装即可自动比价”的成品。

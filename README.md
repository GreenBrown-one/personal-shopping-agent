# Personal Shopping Agent

一个面向个人消费者的、证据驱动的购物决策助手。它把商品参数、报价、适用条件和来源证据整理为可复核的比较结果，但不替用户下单或支付。

## 当前结论

M0–M7 的首轮架构、本地可用性与发布基线已经完成，核心代码可安装、可测试，也适合继续由人类或 AI 维护。默认服务保持离线；另有必须显式开启的京东 MCP 入口，但真实页面兼容性仍需在用户电脑上人工验收。

| 能力 | 当前状态 |
|---|---|
| AI 宿主把自然语言整理成结构化购物需求 | 已支持，由 MCP 宿主完成 |
| 本地工作流、SQLite、确定性评分和可审计报告 | 已支持 |
| OpenAI / DeepSeek 报告解释 | 可选，默认关闭 |
| 京东搜索与详情解析、可恢复端到端管线 | 已提供显式安全入口；默认关闭，真实环境待验收 |
| 下载源码后一条命令完成本地初始化 | 已支持 |
| 自动生成本地 MCP 配置 | 已支持默认离线与显式京东两种模式 |
| 默认 MCP 直接访问真实京东 | 尚未启用 |
| 显式京东 MCP 入口 | 已实现；默认关闭，真实环境待验收 |
| 淘宝、天猫、拼多多比较 | 尚未实现 |
| AI 自动修改并发布自身代码 | 不允许；改进必须走分支、测试、PR 和人工确认 |
| 自动下单或支付 | 明确不提供 |

## 快速开始

要求：安装 [uv](https://docs.astral.sh/uv/)，首次运行时可联网下载 Python 3.12 和锁定依赖。

下载或克隆仓库后，在项目目录运行：

```bash
uv run --locked python scripts/bootstrap.py
```

该命令复用正式 CLI，依次执行健康检查、数据库向前迁移和就绪检查。随后可启动默认本地 MCP：

```bash
uv run --locked personal-shopping-agent mcp-config
uv run --locked personal-shopping-agent-mcp
```

完整的下载方式、Windows 路径示例、MCP 配置模板、模型配置和故障排查见 [快速开始](docs/QUICKSTART.md)。

## AI 如何参与

项目刻意隔离两类 AI：

1. **运行时 AI**理解用户语言，通过严格类型化的 MCP 工具创建任务、读取状态和报告。它不能修改代码、伪造工作流状态或扩大平台权限。
2. **维护型 AI**读取脱敏问题、固定测试样例和仓库代码，在独立分支提出修改，经过静态检查、完整测试、PR 审查和人工确认后才能进入 `main`。

这允许软件持续改进，同时避免网页提示注入或一次错误模型调用直接改变软件本身。详细演进方案见 [AI 持续改进边界](docs/AI_EVOLUTION.md)。

## 项目结构

| 路径 | 职责 |
|---|---|
| `src/personal_shopping_agent/domain/` | 商品、报价、预算、证据等核心不变量 |
| `src/personal_shopping_agent/application/` | 确定性用例、状态机、评分、报告与管线 |
| `src/personal_shopping_agent/platforms/` | 可替换的平台解析适配器，目前为京东 |
| `src/personal_shopping_agent/browser/` | 受控 Playwright 生命周期、导航和限频 |
| `src/personal_shopping_agent/storage/` | SQLite、工作单元、仓储与数据生命周期 |
| `src/personal_shopping_agent/mcp/` | AI 宿主使用的类型化工具边界 |
| `src/personal_shopping_agent/llm/` | OpenAI / DeepSeek 可选解释适配器 |
| `src/personal_shopping_agent/rendering/` | 确定性 Markdown 与安全 HTML 呈现 |
| `migrations/` | 唯一数据库迁移历史 |
| `tests/` | 离线单元、集成和端到端回归测试 |
| `scripts/` | 初始化与隔离 wheel 验收脚本 |

规范性设计以 [DESIGN.md](DESIGN.md) 为准；安全与发布边界见 [SECURITY.md](SECURITY.md)；AI 或人类贡献者须遵守 [AGENTS.md](AGENTS.md)。可迁移到其他 Agent 项目的工程经验总结在 [ENGINEERING_LESSONS.md](docs/ENGINEERING_LESSONS.md)。

## 开发与发布门禁

```bash
uv sync --locked --dev
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest --cov --cov-report=term-missing
uv build
uv run python scripts/verify_wheel.py dist
```

当前测试基线保持语句和分支覆盖率 100%。隔离验收会安装构建出的 wheel，验证版本、迁移、模板、三个命令入口、数据库权限和默认 MCP 组合。

## 产品边界

- 事实必须携带来源、时间和适用范围；缺失与冲突不得由模型猜测补齐。
- Product 与具体卖家、SKU、地区和时间下的 Offer 分开保存。
- LLM 只能解析需求或解释已验证报告，不能改写排名和证据。
- 不绕过验证码、反爬、登录限制或访问控制，不逆向私有 API。
- 不提供任意 URL 浏览、通用状态推进、数据批量删除、下单或支付工具。

## 许可

本项目目前为私有项目，未授予公开使用许可。

# Personal Shopping Agent

一个面向个人消费者的、证据驱动的购物决策助手。它把商品参数、报价、适用条件和来源证据整理为可复核的比较结果，但不替用户下单或支付。

## 当前结论

M0–M7 的首轮架构、本地可用性与发布基线已经完成；M8 把代码按“明确需求 → 查找商品 → 呈现待购 → 自动化 → 自动进化”五层重组，核心代码可安装、可测试，也适合继续由人类或 AI 维护。默认服务保持离线；另有必须显式开启的京东 MCP 入口，但真实页面兼容性仍需在用户电脑上人工验收。

| 能力 | 当前状态 |
|---|---|
| AI 宿主把自然语言整理成结构化购物需求 | 已支持，由 MCP 宿主完成 |
| 开始前确定性审查需求并给出追问建议 | 已支持（`review_shopping_request`） |
| 本地工作流、SQLite、确定性评分和可审计报告 | 已支持 |
| OpenAI / DeepSeek 报告解释 | 可选，默认关闭 |
| 京东搜索与详情解析、可恢复端到端管线 | 已提供显式安全入口；默认关闭，真实环境待验收 |
| 下载源码后一条命令完成本地初始化 | 已支持 |
| 自动生成本地 MCP 配置 | 已支持默认离线与显式京东两种模式 |
| 默认 MCP 直接访问真实京东 | 尚未启用 |
| 显式京东 MCP 入口 | 已实现；默认关闭，真实环境待验收 |
| 京东手动登录（用户本人在可见浏览器中完成） | 已支持（`login jd`） |
| 芯片性能评分（极客湾 SOCPK 综合性能，本地私有参考） | 已支持（`benchmark refresh`），真实页面待验收 |
| 闲鱼二手比较 | 计划中（M10，合规自研，见 DESIGN 7.2） |
| 淘宝、天猫、拼多多比较 | 尚未实现 |
| 本地生成脱敏改进案例预览 | 已支持（`improvement-case` CLI，不上传） |
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

代码按购物决策的五个能力层组织，每层一个包；技术适配器放在它服务的层内部。

| 路径 | 模块 | 职责 |
|---|---|---|
| `src/personal_shopping_agent/intake/` | ① 明确用户需求 | 用与评分相同的规则审查结构化需求，列出阻断问题、警告和追问建议 |
| `src/personal_shopping_agent/sourcing/` | ② 自动查找对应商品 | 搜索、受限详情采集、Offer 转换、官方核验、规格规范化；`browser/` 受控 Playwright，`platforms/` 京东解析器 |
| `src/personal_shopping_agent/presentation/` | ③ 呈现待购商品 | 条件效用、证据置信度、排名、确定性报告；`rendering/` 安全 HTML，`llm/` 可选解释 |
| `src/personal_shopping_agent/automation/` | ④ 自动化 | 工作流生命周期、可恢复端到端管线、单工作流导出/删除 |
| `src/personal_shopping_agent/evolution/` | ⑤ 自动进化 | 本地生成只含代码与结构信息的脱敏改进案例，交给维护型 AI 流程 |
| `src/personal_shopping_agent/domain/` | 共享内核 | 商品、报价、证据、状态机、规格计量目录与条件规则 |
| `src/personal_shopping_agent/infrastructure/` | 共享设施 | SQLite 存储与迁移、私有文件边界、进程设置 |
| `src/personal_shopping_agent/interfaces/` | 外层接口 | MCP 服务器、CLI、健康检查、宿主配置与组合根 |
| `migrations/` | — | 唯一数据库迁移历史 |
| `tests/` | — | `unit/` 按层分目录；`integration/` 覆盖跨层与端到端；`unit/test_architecture.py` 强制层间依赖方向 |
| `scripts/` | — | 初始化与隔离 wheel 验收脚本 |

依赖只能由外向内：需求层与进化层只依赖共享内核，呈现层可读取查找层产出的事实契约，自动化层编排前三层，
只有 `interfaces/` 可以同时装配存储、浏览器和模型。详细规则见 [DESIGN.md 第 3 节](DESIGN.md#3-模块划分与分层)。

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

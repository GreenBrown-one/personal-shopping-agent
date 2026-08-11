# Personal Shopping Agent

一个面向个人消费者的、证据驱动的购物决策助手。它的目标不是替用户下单，而是把分散的商品参数、报价、适用条件和证据整理成可复核的比较结果。

> 当前状态：Milestone 1（领域与本地存储）正在建设。本仓库暂未包含网页采集、MCP 服务或推荐业务逻辑。

## 产品边界

计划中的核心能力：

- 把自然语言需求转换为结构化购物约束；
- 区分商品身份（Product）与具体报价（Offer）；
- 对参数、价格、条件和来源证据做标准化与交叉核验；
- 计算能力、证据置信度、风险调整成本和性价比；
- 输出带来源、缺失项、冲突项和风险提示的比较报告。

明确不做：

- 自动购买、支付或代替用户作最终消费决定；
- 绕过验证码、反爬机制、登录限制或平台访问控制；
- 逆向私有 API、批量高频抓取或规避网站条款；
- 把模型推测写成事实，或把营销表达直接定性为“虚假广告”。

完整设计约束见 [DESIGN.md](DESIGN.md)，协作规则见 [AGENTS.md](AGENTS.md)。

## 本地开发

要求：Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --locked --dev
uv run python -m personal_shopping_agent
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

健康检查成功时会输出：

```json
{"service": "personal-shopping-agent", "status": "ok", "version": "0.1.0"}
```

## 迭代路线

1. M0：工程基础、测试、静态检查与 CI；
2. M1：领域模型、SQLite 存储和迁移；
3. M2：MCP 接口和确定性编排器；
4. M3：浏览器与平台适配器（首个目标为京东）；
5. M4：标准化、证据、价格与排序引擎；
6. M5：LLM 解析/解释层和报告生成；
7. M6：端到端测试、安全加固、安装与发布。

## 当前领域与存储能力

M1 提供严格校验的 `ShoppingRequest`、`Product`、`Offer` 和 `Evidence` 模型，以及本地 SQLite 仓储。报价和商品身份分别存储，证据允许对同一字段保留多个相互冲突的观察。数据库结构通过 Alembic 迁移管理。

## 许可

本项目目前为私有项目，未授予公开使用许可。

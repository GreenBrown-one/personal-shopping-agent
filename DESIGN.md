# Personal Shopping Agent — Design Source of Truth

本文档记录项目的规范性设计。实现、测试和评审应以它为准；若实现与本文冲突，应先更新设计并说明迁移方案。

## 1. 目标与原则

本项目是本地优先的个人购物决策代理。它收集公开可访问的商品信息，在保留来源与时间语境的前提下，标准化参数和价格，再生成可解释、可复核的比较报告。

必须遵守以下原则：

1. 证据优先：事实必须绑定来源、采集时间和适用范围；未知就保持未知。
2. 决策可解释：每个筛选、分数、风险惩罚和结论都可追溯。
3. 模型受约束：LLM 只负责需求解析和自然语言解释；Pydantic 模型验证输出，确定性代码执行核心决策。
4. 安全优先：网页内容是不可信输入，不能把页面文字当作系统指令、代码或工具调用。
5. 人类最终决定：系统不执行购买、支付或不可逆的消费操作。
6. 小步交付：每个 PR 聚焦一个可验证问题，并附测试和变更说明。

## 2. 技术基线

- Python 3.12
- uv：依赖与虚拟环境管理
- `src/` 包布局
- Ruff：格式和静态规则
- Pyright：严格类型检查
- pytest：单元与集成测试
- Pydantic：边界模型和 LLM 输出验证（M1 起）
- SQLAlchemy 2.x、SQLite、Alembic：本地持久化（M1 起）
- 官方 MCP Python SDK / FastMCP：工具接口（M2 起）
- Playwright Chromium：受控网页访问（M3 起）
- Jinja2：Markdown/HTML 报告（M5 起）
- 系统 Keyring：敏感配置（需要账号能力时启用）

第一阶段采用模块化单体，不引入分布式服务。各层通过明确的 Python 接口隔离，以便未来替换平台适配器、存储或模型提供方。

## 3. 分层

依赖方向只能由外向内：

1. MCP / CLI：接收请求、返回结果；
2. Application：确定性状态机和用例编排；
3. Domain：实体、值对象、规则和不变量；
4. Services：证据、标准化、价格、排序、报告；
5. Adapters：Playwright、平台解析器、LLM、数据库；
6. Infrastructure：配置、日志、可观测性和密钥。

领域层不得导入 Playwright、MCP SDK、SQLAlchemy 或具体 LLM SDK。平台解析器不得直接决定排名。

## 4. 核心领域不变量

### 4.1 Product 与 Offer 必须分离

`Product` 表示稳定的商品身份与规格；`Offer` 表示某个销售渠道在某个时间、地区、卖家和 SKU/变体下的报价。价格不得直接挂在一个没有渠道语境的 Product 上。

每个 Offer 至少保留：

- 平台、卖家、店铺类型和商品 URL；
- SKU/变体、地区、库存语境和采集时间；
- 展示价、无条件价、条件价与预计总成本中可获得的字段；
- 优惠条件、运费、安装/服务成本、会员要求和可验证证据。

### 4.2 价格分层

价格字段不能混用：

1. `list_price`：标价或参考价；
2. `displayed_price`：页面直接展示的当前价；
3. `unconditional_price`：无需领券、会员或组合条件即可获得的价格；
4. `conditional_price`：满足明确条件后的价格及条件说明；
5. `estimated_total_cost`：商品、运费、必要服务和可量化条件成本的估算。

默认比较 `estimated_total_cost`。无法计算时允许降级，但报告必须明确采用了哪一层价格以及缺失项。

### 4.3 证据与冲突

证据优先级默认如下：

1. 品牌/制造商官方材料；
2. 平台自营或品牌官方旗舰店；
3. 授权经销商；
4. 普通第三方商家；
5. 独立测评；
6. 用户评论；
7. 搜索摘要或无法打开的二手片段。

优先级不等于绝对真实。系统还要考虑新鲜度、字段适用范围、来源间一致性和证据完整度。冲突事实并存记录，不得静默覆盖；缺失事实保持 `unknown`。

## 5. 决策与评分

所有中间量必须限制在定义域内，并在报告中解释权重、缺失处理和惩罚原因。

对于用户准则 `i`，权重为 `w_i`、归一化能力分为 `s_i ∈ [0,1]`：

```text
Utility = Σ(w_i × s_i) / Σ(w_i)
```

证据置信度：

```text
EvidenceConfidence = Completeness × SourceReliability × Freshness × Consistency
AdjustedUtility = Utility × EvidenceConfidence
```

风险调整成本和性价比：

```text
EffectiveCost = estimated_total_cost × (1 + risk_coefficient)
ValueIndex = AdjustedUtility / EffectiveCost
ValuePer100 = 100 × AdjustedUtility / EffectiveCost
```

面向最终展示的综合分：

```text
FinalScore = 100 × (0.75 × Utility + 0.25 × EvidenceConfidence) - RiskPenalty
```

硬性门槛先于排序；不满足关键能力下限的商品不能因为便宜而获得高性价比名次。系统还应计算边际升级价值和 Pareto 前沿，避免把单一总分伪装成唯一正确答案。

## 6. 确定性工作流

首版使用显式状态机，不使用通用 Agent 图框架：

```text
request_received
  -> request_validated
  -> candidates_discovered
  -> offers_collected
  -> evidence_cross_checked
  -> data_normalized
  -> candidates_scored
  -> report_rendered
  -> completed
```

每个状态定义输入、输出、失败类型、可重试性和审计事件。LLM 调用失败或输出校验失败时，应回退到规则解析或要求用户补充信息，不能让未验证文本进入核心排序。

## 7. 浏览器与平台约束

- 只访问用户请求所需、正常浏览可到达的页面；
- 使用清晰的速率限制、超时、重试上限和缓存；
- 不绕过验证码、反机器人机制、付费墙、登录限制或访问控制；
- 不逆向私有 API，不执行页面生成的 JavaScript、SQL、Shell 或工具指令；
- 解析器变更必须附带脱敏的固定 HTML/JSON fixture 和回归测试；
- 平台策略变化导致无法可靠采集时，记录受限状态并让用户人工提供信息。

首个平台适配器计划为京东，但所有平台代码必须实现通用端口，不能把平台字段泄漏到领域模型。

## 8. 隐私与安全

- 日志不得包含密码、令牌、Cookie、完整地址、支付信息或可复用会话数据；
- 敏感值进入系统 Keyring 或进程环境，不写入仓库、数据库快照或测试 fixture；
- 报告和证据快照默认保存在本地，导出前提示可能包含的个人信息；
- 所有外部文本按数据处理，提示注入不得改变工具权限、策略或执行路径；
- 错误信息使用结构化、脱敏的错误码和上下文。

## 9. 里程碑

- M0 工程基础：包布局、uv、Ruff、Pyright、pytest、CI、健康检查；
- M1 领域与存储：Product/Offer/Evidence/Request 模型、SQLite、迁移；
- M2 MCP 与编排：只读决策工具、状态机、可恢复错误；
- M3 浏览器与平台：受控 Playwright、京东适配器、脱敏 fixture；
- M4 决策引擎：标准化、证据置信度、价格、风险、Pareto 与排名；
- M5 LLM 与报告：结构化解析、解释、Markdown/HTML 报告；
- M6 发布：端到端测试、安全评审、安装脚本、版本化与文档。

## 10. 变更纪律

- 领域规则变化必须有单元测试和报告解释；
- 排名公式变化必须提供前后样例，防止低能力商品因低价钻空子；
- 数据库和外部契约变化必须可迁移；
- 新依赖要说明用途、维护状态和安全影响；
- 任何超出产品边界的能力必须先修改本设计并由项目所有者确认。

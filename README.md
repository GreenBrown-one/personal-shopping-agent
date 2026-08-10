# Personal Shopping Agent

一个面向个人消费者的、证据驱动的购物决策助手。它的目标不是替用户下单，而是把分散的商品参数、报价、适用条件和证据整理成可复核的比较结果。

> 当前状态：M0–M5 已完成。M3 已具备受控浏览器、京东搜索/详情解析、事务化观察存储、严格领域转换和官方证据核验；M4 已完成确定性规范化、成本/证据/预算/Pareto 评分；M5 已完成可审计 Markdown、独立安全 HTML、OpenAI/DeepSeek 可选解释层及报告 MCP 工具。M6 已开始：显式配置的京东服务器可从持久化状态运行或恢复完整决策管线；默认服务器仍不启用平台访问，真实页面采集仍须由调用方提供受安全策略约束的采集器和官方证据提供器。跨平台报价、下单和支付尚未实现。

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
uv run playwright install chromium
uv run python -m personal_shopping_agent
uv run personal-shopping-agent migrate
uv run personal-shopping-agent doctor
uv run personal-shopping-agent-mcp
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
uv run python scripts/verify_wheel.py dist
```

健康检查成功时会输出：

```json
{"service": "personal-shopping-agent", "status": "ok", "version": "0.1.0"}
```

首次启动 MCP 前必须运行 `personal-shopping-agent migrate`。该命令使用 wheel 内随附的 Alembic 历史，
只执行向前迁移；`personal-shopping-agent doctor` 只检查数据库文件和版本，不会创建缺失数据库。两者
默认读取 `PERSONAL_SHOPPING_DATABASE_URL`，未设置时使用
`sqlite:///data/personal-shopping-agent.db`，也可显式传入 `--database-url`。MCP 入口发现数据库缺失或
版本落后时会拒绝启动并提示先迁移，不会在工具调用期间临时改表。

## 可选 LLM 解释器

OpenAI 与 DeepSeek 只为已经生成的确定性报告补充自然语言解释。适配器不会读取原始 HTML、截图或
浏览器会话，只接收有限候选的结构化事实；输出还要通过 Pydantic、报告哈希、候选顺序和事实 ID
白名单校验。模型未配置、API 不可用、返回空内容或输出越界时，应用会保留原 Markdown 报告并返回
`deterministic_fallback`。

API 密钥只在运行时传入，不写入配置文件、数据库或报告。模型名要求显式提供，以免代码把会变化的
供应商默认值固定下来。默认 `PERSONAL_SHOPPING_LLM_PROVIDER=disabled`；只有明确选择 `openai` 或
`deepseek` 时才读取对应密钥。示例：

```python
from personal_shopping_agent.llm import create_report_presentation_service_from_environment

presentation_service = create_report_presentation_service_from_environment()
presentation = presentation_service.present(rendered_report)
```

OpenAI 运行变量：

```text
PERSONAL_SHOPPING_LLM_PROVIDER=openai
OPENAI_API_KEY=<runtime secret>
PERSONAL_SHOPPING_OPENAI_MODEL=<explicit model name>
```

DeepSeek 运行变量：

```text
PERSONAL_SHOPPING_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=<runtime secret>
PERSONAL_SHOPPING_DEEPSEEK_MODEL=<explicit model name>
```

两者共用的可选限制为 `PERSONAL_SHOPPING_LLM_TIMEOUT_SECONDS`（默认 30，最大 120）、
`PERSONAL_SHOPPING_LLM_MAX_OUTPUT_TOKENS`（默认 2000，范围 256–4096）和
`PERSONAL_SHOPPING_LLM_MAX_CANDIDATES`（默认 3，范围 1–10）。非法配置只返回脱敏错误码，不发起
API 请求。成功解释会作为经过 Markdown 转义、带事实 ID 与内容哈希的附加章节；关闭模型或调用失败
时，最终展示内容与原确定性报告逐字节一致。

DeepSeek 适配器是纯文本解释器，因此不要求模型具备视觉能力。商品图片或页面视觉信息若未来需要
处理，应由受控采集/视觉层先转换为带来源的结构化事实，再进入报告；解释器不能直接看图后改写排名。

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

M2 增加官方 MCP Python SDK v2 服务和可审计状态机。AI 主机现在可以创建结构化购物任务、查询任务进度和检查当前能力；任务只能按设计顺序推进，不能跳过阶段。M3/M4 接入平台数据和排序后，内部编排器会在实际步骤成功后推进状态。

M3 的第一个安全切片增加受控 Playwright Chromium 生命周期和安全导航策略：浏览器使用未纳入版本控制的专用本地资料目录，只允许显式白名单中的公开 HTTPS 主机，拒绝私网地址、非标准端口、URL 内嵌凭据、下载以及结算/订单/支付路径。页面只以有大小上限的 HTML 快照交给后续平台解析器；测试使用替身，不访问真实购物网站。

第二个 M3 切片选择京东作为 v1.0 的唯一购物平台。它增加平台无关的 `PlatformCandidate`、搜索适配器端口和候选发现用例，以及基于脱敏 fixture 的京东搜索解析器。解析器只保留搜索页实际观察到的 SKU、标题、展示价、店铺、评价摘要和徽标；这些候选在详情与证据核验前不会被提升为已确认的 `Product` 或 `Offer`。遇到验证码、安全验证或访问频率限制时立即停止。

第三个 M3 切片增加平台无关的详情观察契约和京东详情解析器。它分别保留标价、展示价和条件价，绑定详情 URL、SKU、变体选项、卖家、店铺分类依据、地区、库存原文、下柜状态、促销条件和采集时间；规格仍以原始键值保存。页面营销文字不会被执行，含糊库存保持未知，条件价不会冒充无条件到手价，店铺名称也不会自动证明商品真伪。

第四个 M3 切片增加追加式的结构化平台观察存储。搜索结果和详情结果都绑定发起它们的购物请求，并按采集时间保留历史冲突；数据库读取会重新执行严格模型校验。该存储不包含原始 HTML、截图、Cookie 或浏览器会话，也不会把搜索候选或未验证详情自动提升为 Product/Offer，更不会仅因写入成功而推进工作流。

第五个 M3 切片把搜索与详情接入请求级 SQLite 工作单元。一次采集只使用一个 Session/连接；搜索观察、受限数量的详情观察和 `candidates_discovered` 审计事件全部成功后才共同提交。搜索无结果、详情解析失败、数据库错误或并发修订冲突会回滚整个批次并关闭连接，工作流保持在 `request_validated`。`offers_collected` 仍保留给后续真正生成 Product/Offer 的步骤。

第六个 M3 切片增加严格的详情领域转换。只有请求关联、品牌/型号、卖家、地区、库存语境、SKU 和可见价格层完整的详情才能形成 Product、Offer 与 Evidence；条件价、变体和上下架状态必须内部一致。相同品牌/型号在批次内共用 Product，不同时间、卖家、SKU、变体和地区仍分别形成 Offer。页面证据回指原详情观察，置信度在交叉核验前保持未评估。所有领域实体和 `offers_collected` 事件在一个事务中提交，失败时全部回滚。

第七个 M3 切片增加平台无关的官方证据提供端口和字段级核验事务。制造商观察先以品牌/型号精确绑定 Product，再把官方 Evidence、两侧原始值、来源 URL 与一致/冲突/单侧缺失状态追加保存；官方来源完全缺失也会形成明确记录。当前比较只归一化大小写和空白，不做单位换算或模糊匹配。整个批次与 `evidence_cross_checked` 事件原子提交，任一产品的来源、身份、校验或持久化失败都会回滚。真实厂商适配器尚未启用，测试只使用离线替身。

第八个 M3 切片增加平台无关的访问协调器。同一主机默认至少间隔 3 秒，单次请求默认最多尝试 2 次、硬上限 3 次，并只对脱敏瞬时导航故障和明确的临时 HTTP 状态进行有限退避；401、403、429、普通 4xx、导航策略拒绝以及解析器识别出的验证码或访问限制都不会重试。截图请求只尝试一次，非成功响应不保存截图。相同 URL 仅在同时进行时合并，结束后立即释放页面，不启用跨请求 HTML 缓存。

M4 的第一个切片增加确定性规格规范化及原子工作流事务。它把电池容量、重量、屏幕尺寸、存储容量和运行内存的声明别名转换到 `mAh`、`g`、`inch` 或 `GB`，同时保留所有原字段、原值和证据 ID。只有完整的“数字 + 支持单位”表达才会换算；多值冲突、含糊值和未知字段分别明确记录，不会因规范化覆盖证据或猜测缺失单位。规范结果与 `data_normalized` 事件共同提交，失败时完整回滚。

M4 的第二个切片增加不依赖 LLM 的纯决策基础。用户条件只接受方向明确、非负且单位与规范事实一致的数值边界；每项保留满足、未满足、缺失、冲突或无法解析状态，硬性条件在排序前单独设闸。报价继续遵循既有价格层降级顺序，并用可解释的店铺、价格层、库存和地区不确定性系数形成比较成本；缺货、币种不一致及缺少条件说明的条件价不会进入最佳报价。该基础本身不是最终推荐，由后续置信度与排名切片消费。

M4 的第三个切片增加纯证据置信度评估。它按条件权重分别计算事实完整度、来源可靠性、新鲜度和一致性，保留实际参与计算的 Evidence ID 与原因码，并用四项乘积调整上一阶段效用。缺失或冲突事实不会被猜测；不同 URL 的证据只有在规范化后得到同一确定值时才算多来源一致；未来时间记录、跨请求数据和重复证据 ID 会被拒绝。来源类型和规格年龄只提供已文档化的默认先验，显式评估值可以覆盖但不会写回原 Evidence。该评估保持为纯计算输入，由最终排名事务负责持久化完整结果。

M4 的第四个切片完成最终候选评分。它先执行硬性条件、可比报价、正成本、证据置信度和预算上限闸门，再按正常预算与可选弹性预算分层；预算层由页面选定价格判断，风险调整成本只参与价值、Pareto 和风险惩罚。每个预算层独立计算 Pareto 前沿，并用综合分、每百元价值、有效成本和稳定 Product ID 决胜。所有中间基础、原因码、最终指标和时间都保存在 `candidate_scores` 完整快照中，与 `candidates_scored` 审计事件使用同一 SQLite 事务，任一外键、校验或并发错误都会整体回滚。

M5 的第一个切片增加模型无关的确定性 Markdown 报告。报告直接嵌入原始购物请求、候选分数、Product、实际最佳 Offer 和参与置信度计算的 Evidence，展示预算层、价格、风险、综合分、条件明细、来源链接、排除原因和购买前复核提示。所有页面来源文字先做 Markdown 转义，报告内容使用 SHA-256 绑定完整快照，并与 `report_rendered` 审计事件在同一事务提交。此能力不调用 ChatGPT 或任何其他 LLM；后续解释器只能读取该快照并返回经过类型校验的文字，不能改变事实或名次。

M5 的第二个切片增加模型无关的 `ReportExplanationProvider` 端口，以及 OpenAI Responses Structured
Outputs 和 DeepSeek OpenAI 兼容 JSON Output 适配器。默认请求只投影报告中的前三个候选，并为预算、
价格、综合分、证据分、风险、排除码和逐项条件建立稳定事实 ID；模型生成的总览、候选说明和风险
提醒必须引用这些 ID。OpenAI 返回 Pydantic 结构，DeepSeek 返回 JSON 后在本地执行同一结构校验；
两者都还要通过报告 ID/哈希、候选范围与顺序、跨商品引用和固定免责声明检查。该解释是非持久化的
可选覆盖层，不推进工作流、不替代确定性报告，也不获得浏览、下单或支付能力。

M5 的第三个切片增加默认关闭的运行时配置和安全展示服务。系统通过
`PERSONAL_SHOPPING_LLM_PROVIDER` 在关闭、OpenAI 与 DeepSeek 之间显式选择，只读取被选中提供方的
密钥和模型名；密钥使用 `SecretStr` 留在内存，并从模型导出和对象表示中排除。配置合法后，组合工厂
创建相应适配器、事实请求构建器和展示服务。经过验证的模型文本会先转义，再作为带事实 ID、提供方、
模型名和新 SHA-256 的 Markdown 章节追加在确定性报告之后；回退展示必须与原报告内容及哈希完全一致。
第三个切片本身不注册 MCP 工具；真实 API 只有在运行时明确配置提供方后才可能调用。

M5 的第四个切片把报告能力接入 MCP。SQLite 读取器会重新校验完整报告快照，并核对数据库中的报告、
请求、工作流、格式、正文和内容哈希索引。`render_shopping_report` 只为已经完成确定性评分的工作流生成
并持久化报告；`get_shopping_report` 始终是纯本地、幂等读取，不会调用模型；只有显式调用
`explain_shopping_report` 才可能访问已配置的 OpenAI 或 DeepSeek。解释仍不持久化、不推进工作流，
提供方关闭或失败时返回逐字节相同的确定性报告。

M5 的第五个切片完成独立 HTML 报告。规范 Markdown 仍是数据库中唯一持久化表示；HTML 从同一已验证
报告快照按需派生，拥有独立格式标识与 SHA-256，不写回数据库或推进工作流。Jinja2 模板使用沙箱、
严格未定义检查和自动转义，输出不包含 JavaScript、表单、图片或外部资源，并通过 CSP 和
`no-referrer` 限制打开后的能力。HTML 包含与 Markdown 相同的需求、预算、排名、价格、风险、条件、
证据、排除原因和限制，但不混入非确定性的模型解释。

M6 的第一个切片装配可恢复的京东决策管线。`ShoppingDecisionPipelineService` 读取持久化工作流状态，
只执行尚未提交的真实用例：搜索/详情采集、Offer 转换、官方证据核验、规格规范化、候选评分、确定性
报告及完成事件。每个阶段继续使用自己的事务边界；错误不会伪造后续状态，重试从最后一个已提交状态
继续。采集上限严格受约束，截图强制关闭；完成后的再次调用只读取原报告，不重复访问平台。

该管线不会出现在默认 `create_server_for_database` 中。只有调用方显式使用
`create_jd_pipeline_server_for_database` 并注入受控 `PageCollector` 与 `OfficialEvidenceProvider` 时，
服务器才增加 `run_shopping_pipeline` 并报告 M6 能力。离线时可显式使用
`NoOfficialEvidenceProvider`，系统会把官方来源记为缺失而不是猜测。结构化 MCP 输出还统一把 Decimal
编码为无指数的精确字符串，以保持返回值与公开 JSON Schema 一致。

M6 的第二个切片补齐安装与数据库启动边界。所有 Alembic 迁移和 HTML 模板都进入 wheel；新的
`personal-shopping-agent health|doctor|migrate` 管理入口提供稳定 JSON 和退出码，错误不回显数据库 URL
或内部异常。默认 MCP 进程只接受已经达到随包 head 的数据库。CI 除完整静态检查与测试外，还构建
sdist/wheel，并验证 wheel 中的迁移、模板和命令入口。

M6 的第三个切片收紧发行一致性。项目版本只在无依赖的 `__about__.py` 中声明，Hatch 构建元数据、
Python 包、健康检查与 MCP 服务器共同读取该值，避免分别维护后漂移。CI 不再仅解压 wheel：它会创建
与源码环境隔离的全新虚拟环境，安装 wheel 及其依赖，实际执行健康检查、迁移前的拒绝检查、迁移、
迁移后的就绪检查，并从安装位置组合默认 MCP 服务器。验证脚本接收 `dist` 目录并自动定位唯一 wheel，
因此发布版本变化时无需改写 CI 文件名。

淘宝/天猫和拼多多只作为未来适配器候选保留，不在 v1.0 同时接入。当前顺序是先把京东搜索、详情、地区价格、库存语境和证据链做完整，再评估第二个平台，避免在多套不稳定页面结构上过早摊薄测试与维护投入。

当前暴露的 MCP 工具：

- `shopping_agent_status`：查询已实现与尚未实现的能力；
- `start_shopping_workflow`：把已结构化需求保存为本地购物任务；
- `get_shopping_workflow`：读取请求、当前状态和完整审计事件；
- `render_shopping_report`：为已有 `candidates_scored` 结果生成并原子保存确定性报告；
- `get_shopping_report`：只从本地读取并校验确定性报告，绝不调用 LLM；
- `get_shopping_report_html`：从同一报告快照生成自动转义、无脚本的独立 HTML；
- `explain_shopping_report`：显式请求非持久化的可选 AI 解释，失败时安全回退。
- `run_shopping_pipeline`：仅在显式配置的京东服务器中出现，运行或恢复剩余真实阶段。

默认服务器的前七个工具不会访问购物平台、下单或支付。解释工具仅在显式配置模型提供方时可能访问
模型 API，并标记为只读、非幂等且可能访问开放网络；普通报告读取为只读、幂等且仅本地。条件注册的
管线工具会通过调用方提供的采集端口访问公开页面，因此标记为写入、非破坏、非幂等且可能访问开放
网络；它仍不提供任意 URL、任意状态推进、下单或支付能力。

默认组合没有启用真实平台白名单、浏览器生命周期或厂商适配器，因此
`shopping_agent_status` 继续报告 `end_to_end_pipeline=false` 和 `platform_collection=false`。显式京东
组合只接受调用方拥有的最小采集/证据端口；在保留期/删除机制和真实环境人工验收完成前，不提供一个
自动开启真实浏览器的默认入口。

## 许可

本项目目前为私有项目，未授予公开使用许可。

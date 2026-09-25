# 京东实网验收记录

本文记录在用户电脑（Windows）上进行的真实京东验收结果与未解决问题。原始页面、截图和浏览器资料只保存在
本机被 Git 忽略的目录中，这里只记录脱敏后的结构结论。

## 2026-09-25 首次验收

需求：3 个条件（电池容量为硬性条件，存储、内存为加分项），预算 3000、可上浮至 3300，地区北京市；
`run_shopping_pipeline` 使用 `maximum_candidates=10`、`maximum_details=3`。

| 步骤 | 结果 |
|---|---|
| `playwright install chromium` | 成功 |
| `shopping_agent_status` | `platform_collection=true`、`end_to_end_pipeline=true`、`chip_benchmark=false` |
| `review_shopping_request` | 无问题，`ready_to_start=true` |
| `start_shopping_workflow` | 成功 |
| `run_shopping_pipeline`（未登录） | 修复前返回 `Only HTTPS navigation is allowed.`；修复后返回 `sign_in_required` |
| `login jd` | 修复前登录窗口显示“当前页面异常”；修复后可完成登录并跳转到 `www.jd.com`，但登录页仍可能短暂显示该提示 |
| `run_shopping_pipeline`（已登录） | 搜索页被京东风控拦截：页面显示“抱歉由于访问频繁导致无法搜索”，没有任何商品卡片 |
| `benchmark refresh` | `benchmark_page_unrecognized` |

### 已修复

1. **被拦截的脚本跳转掩盖真实原因。** 京东搜索页先正常加载，随后由页面脚本跳转到登录页；白名单拦下
   该跳转并记录 `sign_in_required`，但浏览器随即显示 `chrome-error://` 错误页，最终 URL 校验先报出
   `scheme_not_allowed`。现在已记录的主框架拦截优先返回。
2. **登录窗口拦截了账号安全验证。** 登录后京东会跳转到 `aq.jd.com` 做人工验证，登录页风控脚本来自
   `jrsecstatic.jdpay.com`。登录策略现放行这两者；采集策略仍不加载 `jdpay.com`，遇到 `aq.jd.com` 时以
   `sign_in_required` 停止。
3. **未识别新版风控文案。** 2026 版搜索页用“访问频繁导致无法搜索”代替结果列表，旧标记“访问过于频繁”
   不能匹配，导致误报“没有候选”。现已加入“访问频繁”标记，真实页面离线重放返回 `jd_access_restricted`。

### 未解决

1. **京东风控拒绝自动化会话的搜索。** 账号处于登录状态，但搜索结果被替换为风控提示。推测 Playwright 自带
   Chromium 被识别为自动化浏览器（未证实）。按安全规则不做指纹伪装或其他绕过。可选方向：
   - 用户辅助模式：用户在自己的浏览器中搜索，程序只解析用户提供的页面或商品链接；
   - 使用本机已安装的 Chrome 通道（仍属自动化，不保证通过）；
   - 京东开放平台等官方接口（需申请权限）。
2. **登录跨站同步仍被拦截。** `sso.jd.hk`、`sso.jdcloud.com` 等十余个关联站点的登录同步请求被拦截，
   可能导致登录页短暂显示“当前页面异常”；主站登录不受影响。登录页还会探测 `127.0.0.1`，应继续拦截。
3. **SOCPK 排行页已下线。** `https://www.socpk.com/allperf/?brand=phone` 现跳转到首页，站点改为按类别
   展示（如 `/category/mobile-soc`），芯片性能评分需要重新选择数据源并重写解析器。
4. **MCP 工具错误丢失稳定错误码。** 管线异常只以英文消息返回给宿主，工作流 `error_code` 保持为空，
   `improvement-case` 只能由调用方猜测错误码。
5. **新版搜索页结构尚无 fixture。** 搜索页已改为 React/Vite 单页应用（`#searchCenter`、带哈希后缀的
   CSS Module 类名），本次页面中没有旧解析器依赖的 `gl-item`、`p-name`、`p-price`。由于结果区被风控
   提示替代，尚不能确认正常结果卡片的结构；预计搜索解析器需要基于新版脱敏 fixture 重写。

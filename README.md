# 园区能耗数据产品 SLO 与事故影响管理平台

工业园区能源管理中心的**数据质量与消费影响**管理平台。平台不操作楼宇设备、不调整真实负荷、不办理能源结算，只回答一个问题：

> 一条迟到、缺失或未核验的能耗数据，是否让某个数据产品对某类消费者不可用？谁该等、谁可以先用带缺口的曲线、事故何时能关闭？

另有一个独立的**峰值需量证据数据产品**（见 6.5）：以表计实际采样覆盖与合同容量为依据，给出每栋楼报告期内已确认峰值、缺口格与合同超限的**可证实 / 未知**范围，供容量复核，不执行限电或计费。

四个模块联动决定一份数据能否被标记为可用：

| 模块 | 职责 |
| --- | --- |
| 来源登记 | 每类数据的采样承诺：表计有效安装期、计划频率、允许迟到、维护生效区间、产品依赖 |
| 产品运行 | 生成两类产品：**近实时能耗曲线**（带截止点的暂定版本）与**核验日汇总**（完整重算后合格） |
| 消费目录 | 物业大屏、月度结算、节能考核各自登记的产品字段 / 楼栋 / 时段依赖与阈值 |
| 事故指挥 | 根据违约事实与消费影响驱动处置，恢复评估通过后才允许关闭 |
| 峰值需量证据 | 15 分钟结算格归集或带时间戳区间积分，逐格区分完整/部分/无覆盖，输出已确认峰值、风险区间与合同状态版本 |

## 1. 技术栈

- **前端**：React 18 + TypeScript + Vite + React Router（自绘 SVG 曲线，无图表库依赖）
- **后端**：FastAPI 0.115 + SQLAlchemy 2.0 + Pydantic v2
- **数据库**：PostgreSQL 18（JSONB 保存评估明细）
- **测试**：pytest（后端 38 项，含峰值需量 16 项）+ jsdom 渲染断言（前端 33 项）

## 2. 一键启动（Docker Compose）

```bash
docker compose up --build
```

> 本交付环境内没有安装 Docker 守护进程，因此 Compose 配置未能在本机实际执行；已通过等价路径验证同一镜像行为：先 `npm run build`，再以 `SPA_DIST=frontend/dist` 启动后端，确认 `/`、前端路由与 `/api/*` 在同一 8000 端口下均可访问。

- 控制台：<http://localhost:8000>
- OpenAPI 文档：<http://localhost:8000/docs>（Swagger UI） · <http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/healthz>

启动后自动建表并生成种子数据：模拟日 `2026-11-10`、模拟时钟 `06:00`，以及 18:00（前一日）至 06:00 的表计读数。首次打开建议先点「验收报告 → 重放验收场景」。

## 3. 本地开发启动（不使用 Docker）

容器内已预装运行时，三个进程分别启动：

```bash
# 1) 依赖
cd /workspace/52
python3 -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend/requirements.txt
npm --prefix frontend install --registry https://registry.npmmirror.com

# 2) 后端（建表 + 种子都在启动时完成），端口 8000
cd backend
DATABASE_URL=postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev \
  ../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3) 前端开发服务器，端口 5173，/api 代理到 8000
cd frontend
npm run dev
```

打开 <http://localhost:5173>。数据库连接可用 `DATABASE_URL` 覆盖；默认是容器自带的 `127.0.0.1:5432/ccdev`。

> 单容器模式：`cd frontend && npm run build`，然后以 `SPA_DIST=$PWD/../frontend/dist` 启动后端，即可在 8000 端口同时提供 API 与前端静态资源（与 Docker 镜像行为一致）。

## 4. 测试账号

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `ops` | `ops123` | 值班调度（operator） |
| `admin` | `admin123` | 平台管理员 |

登录为演示态鉴权（前端携带 token，后端校验用户名口令哈希）；本平台不承担园区统一身份认证职责。

## 5. 领域模型与计算规则

### 5.1 观察窗口与槽位

- 观察窗口固定 **6 小时**（`00:00`、`06:00`、`12:00`、`18:00` 起），槽位 **15 分钟**。
- 预期槽位**按窗口实际推导**，不是常量：
  - 按采样承诺（`meter_commitments`）的有效区间与**计划频率**分段生成；
  - 频率或安装关系在窗口内变化时**分别拆段**，各自计算分母（例如 15min×8 + 30min×8 = 16）；
  - 维护窗口仅在**规则允许扣除**（`allowed_deduction`）且**完整覆盖该槽位**时从分母扣除；
  - 截止点之后尚未结束的槽位标记 `beyond_cutoff`，**不进入分母、不算缺失、不填零**；
  - 重复到达只占一个槽位；声明零用量（`is_declared_zero`）是有效值。

### 5.2 健康状态

```
breached  ← 存在缺失槽位 或 核验缺口
pending   ← 尚有未到期槽位 / 窗口未结束
partial   ← 覆盖率不足但有数据
degraded  ← 覆盖与核验都通过，但存在超出允许迟到的槽位
healthy   ← 覆盖 100%、核验通过、无超时到达
```

合格（`certified`）需要同时满足：覆盖完整、核验通过、无超时到达、截止点晚于窗口结束、所有计划槽位均已到期。健康状态**不由采集任务存活或人工关闭事故决定**。

### 5.3 核验缺口（单列，不计为节能）

| 类型 | 触发 |
| --- | --- |
| `reset` | 累计表读数回退，负差不可计为节能 |
| `ratio` | 换表后新表首读数与旧表末读数 × 声明倍率偏差 > 5% |
| `bridge` | 表码差跨过未覆盖槽位，无法归因 |
| `baseline` | 累计表缺少可衔接的基线读数 |

### 5.4 违约事实（追加式）

`breach_facts` 只追加，不删除。迟到补传或事后补录维护会把事实标记为 `resolved_at` 并保留 `retroactive_maintenance_id` 备注，但**当时的违约事实仍然存在**。

### 5.5 消费影响

- 依赖按 `(产品, 字段, 楼栋, 有效区间)` 登记，消费者各自持有 `tolerance_minutes` / `min_coverage` / `max_gap_slots` / `require_certified`。
- **同一版本、不同阈值 → 不同结论**：槽位结束超过某消费者容忍时间仍未取得数据才计入该消费者的影响；物业可继续看受限曲线，结算与考核按自己的规则等待。
- 一个消费者依赖多个受影响字段时，影响区间**合并去重**，同一缺口只计一次。
- 下游日汇总失效会追溯到**源字段/楼栋的缺失区间**；缺失表计不会牵连无关楼栋（按楼栋分别计算）。
- 阈值预览不落库、不修改原始质量事实，只重算判定。

### 5.6 事故生命周期

```
open → (extended) → recovering → [恢复评估] → closed
                     ↑               |
                     └── 关闭前复查发现新缺口（reopened，旧结论作废）
closed ──同因再发──▶ 新事故区间（prior_incident_id 关联，两段间正常时段不计为连续故障）
```

- 关闭必须存在该事故的**已完成恢复评估**（`recovery_checks`），且窗口内无未解决事实；
- 关闭前复查若发现新缺口，拒绝关闭并把事故改回 `open`；
- 事故关联影响区间、违约事实与消费者影响证据；相同原因关闭后再缺失时，建立**新区间**并关联既往事故。

### 5.7 重算与缓存

- 每个观察窗口**完整重算**：事件（到达、补传、维护、换频、时钟推进）只决定**重算哪些窗口**，不会就地补一个槽位。
- 结果未变化的重复重算复用同一 `revision` 并累加 `recompute_count`；结果变化才追加新 revision，历史版本可追溯（`/api/health/evaluations/{id}` 返回 `revisions` 链）。

## 6. 界面

| 页面 | 内容 |
| --- | --- |
| 产品健康 | 窗口 × 来源的分子/分母、覆盖率条、新鲜度、核验状态；表计下钻（频率分段、维护扣除、核验缺口、槽位矩阵）；带截止点的曲线 |
| 产品运行 | 近实时曲线与核验日汇总的运行版本、版本谱系、合格范围、消费者引用与判定 |
| 来源登记 | 表计安装期/频率/允许迟到/维护窗口/依赖；登记维护与变更频率后自动重算 |
| 消费影响 | 三个消费者的卡片与窗口判定、依赖分解、按楼栋结果、**阈值敏感性预览**（只改判定不改事实） |
| 事故指挥 | 事故列表、状态机、恢复评估记录、影响区间、时间线；执行恢复评估 / 尝试关闭 / 记录备注 |
| 模拟与验收 | 推进时钟、延迟/丢弃样本、迟到补传、登记维护、换频、审计时间线、重置基线 |
| 峰值需量证据 | 每栋楼 × 两种口径（15 分钟格 / 区间积分）的已确认峰值、风险区间、缺口格与候选峰值格、合同状态与版本谱系；逐格证据表；一键按 00:00 / 02:00 截止点发布，展示迟到读数带来的峰值结论变化 |
| 验收报告 | 从模拟表计实际到达重放的 15 个步骤，每步给出数据库读回的观测；另有「重放峰值需量验收」7 个步骤 |

## 6.5 峰值需量证据与风险区间（数据产品）

园区容量管理依赖**每月最高 15 分钟平均需量**。本产品把"可证实"与"未知"分开，供容量复核；**不执行限电、不直接计费**。

### 6.5.1 输入与口径

- 计量点：每栋楼一个 `M-DMD-{楼栋}-01` 峰值需量计量点（独立 `demand` 来源，不参与 SLO 采样承诺），登记在 `demand_meter_configs`：计量模式（`energy_interval` 区间电量 / `register` 累计表码 / `power` 平均功率）、15 分钟格数据流、区间积分数据流、设备额定上限、相邻读数最大可信变化率。
- 合同容量：`contract_capacities` 按楼栋 + 字段登记生效区间，来源与备注一并保存。
- 读数：`demand_readings` 保存 `measured_from` / `measured_to`（**采样时段**）、`value`、`stream`、`received_at`（到达时间）与备注。
- **采样时间 vs 接收时间**：归格、平均需量与跨格分摊只用采样时间；`received_at` 只用于判断该读数是否在本次数据截止点之前到达。迟到读数不会改写旧版本，只会在新截止点形成新报告版本。

### 6.5.2 两条路线

| 路线 | 规则 | 代价 |
| --- | --- | --- |
| `grid` 15 分钟结算格 | 读数先归一到固定 15 分钟格；只有格内证据完整铺满 900 秒且无跨格读数，才计算平均需量 = 格内电量 ÷ 0.25h | 口径稳定、可与结算周期逐格核对；边界采样不齐时留下更多未知格 |
| `integration` 区间积分 | 对带采样时段的高频读数按重叠时长积分，跨格事件精确分摊；覆盖满一格才确认 | 能利用不规则采样并精确处理跨格事件；每个读数的代表时段必须可信，断档期间不插值 |

两条路线都把格分成 **完整覆盖 `full` / 部分覆盖 `partial` / 无覆盖 `none`**：
- `partial` 只给"观测部分"的 `observed_kw`，作为**保守下界**，绝不按比例放大成 15 分钟平均；
- `none` 是未知格，不填零；
- 累计表（`register`）遇到回退、抄见间隔跨格或非单调，一律列未知，**不产生负需量**；
- 重复报文按 `(stream, 起始, 结束)` 去重，只保留截止点前最新一条，其余计入重复条数、不累加。

### 6.5.3 确认峰值、风险区间与合同状态

- **已确认峰值** = 所有 `full` 格中最高的 15 分钟平均需量，并给出时间格。
- **风险区间** `[lower_kw, upper_kw]`：下界为 MAX(已确认峰值, 各未知格观测下界)；上界只有在**每个未知格都有明确来源的保守上界**时才确定：
  - 设备额定上限（`rated_kw`），或
  - 明确登记的相邻读数最大变化率 × 与最近 `full` 格的距离（`neighbor_step_kw`）。
  - 若某未知格两者都没有，上界为 `null`（未知）。**不用历史最大值**假设未来缺口不可能更高。
- 合同状态：

| 状态 | 条件 |
| --- | --- |
| `exceeded` 已证实超限 | 已确认峰值 > 合同，或某未完整覆盖格的观测下界 > 合同（非负负荷下观测只会低估） |
| `not_exceeded` 已证实未超限 | 全部格完整；或所有未知格都有保守上界且整体上界 ≤ 合同 |
| `not_proven` 尚未证实超限 | 最高已确认格 < 合同，但存在无上界/上界高于合同的未知格 |
| `no_contract` 未登记合同 | 只输出范围，不给合同结论 |

`candidate_peak_slots` 列出**可能隐藏超限**的未知格（无上界，或上界 > 合同）；`peak_candidates` 列出已确认格前 5。

### 6.5.4 数据截止点与发布

- 读数规范化、时间格覆盖、需量计算、合同阈值与报告发布**绑定同一个 `data_cutoff`**：`demand_reports` 唯一键为 `(楼栋, 字段, 口径, 报告期, 截止点, revision)`。
- 截止点之后才到达的读数不进入旧版本；用更晚截止点重新发布会生成新的 `revision` / `version`，并在 `delta` 中给出：峰值时间格前后变化、已确认峰值前后变化、合同状态前后变化、未知格数量变化与 `conclusion_changed`。旧版本保留可追溯（`/api/demand/reports/{id}` 返回 `versions` 链）。
- 截止点之后尚未结束的结算格**不进入分母、不算未知、不填零**。

## 7. 验证方式

### 7.1 后端测试（38 项）

```bash
cd backend
DATABASE_URL=postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev_test \
  ../.venv/bin/python -m pytest tests -q
```

测试会自建 `ccdev_test` 库（若不存在）。覆盖：

- 预期槽位与维护扣除、未到截止槽位不填零、声明零用量有效
- 重复到达只占一槽、频率变更拆段、安装期中断拆段
- 事后补录维护：分母扣除但违约事实保留并标注
- 累计表回零、换表倍率不衔接 → 核验缺口、无负节能
- 迟到重传同时更新曲线与完整性
- 同版本不同容忍度 → 不同判定；缺失不牵连无关楼栋
- 多产品同一缺口合并一次、阈值预览只改判定
- 无恢复评估拒绝关闭、关闭前新缺口重开（含跨窗口新缺口）、恢复后再发关联既往事故、区间不含中间正常时段
- 峰值需量：完整/部分/无覆盖格逐格验证、重复报文不累加、跨格读数不产生完整格、区间积分恢复格归集未覆盖的峰值、累计表回退为非负未知、满覆盖/明确保守界才可证实未超限、缺口格存在时只给"尚未证实超限"、无合同时不给结论、按采样时间而非接收时间归格、截止点之后的格不算未知、截止点太早（无到期格）不给合同结论、迟到读数生成新版本并标出结论变化、7 步峰值验收全部通过

最近一次实际执行结果：`38 passed`。

### 7.2 前端渲染断言（33 项）

```bash
# 先启动后端（8000）
cd frontend
API_BASE=http://127.0.0.1:8000 npm run test:ui
```

脚本先调用 `/api/acceptance/run` 重放验收，再用 jsdom 挂载各页面到真实 API，断言页面出现真实数据（表计编号、消费者名、事故编号、峰值需量表的「已证实超限 / 尚未证实超限 / 风险区间 / M-DMD-」等），并点击「预览判定变化」验证阈值预览渲染。最近一次实际执行结果：`UI smoke OK (33 assertions)`。

另有峰值需量的交互断言（切换区间积分、核对补回的 961.4 kW 峰值、重放 7 步峰值验收并检查渲染）：

```bash
cd frontend
API_BASE=http://127.0.0.1:8000 npm run test:demand-ui
```

最近一次实际执行结果：`Demand UI check OK (12 assertions)`。

> 说明：容器内缺少 Chromium 所需的 `libatk` 等系统库且无 root 权限安装，因此未做浏览器截图验证；改用 jsdom 做真实数据渲染断言。前端 `npm run build`（tsc + vite）已实际执行通过，`/` 与前端路由均由后端 SPA 挂载实际返回 200。

### 7.3 手工验收（推荐路径）

1. 登录 `ops / ops123`，进入「验收报告」→「重放验收场景」，逐个展开 15 个步骤。
2. 重点核对：
   - `single_meter_delayed`：电力 `113/116`，物业 `restricted`、结算 `unavailable`；
   - `tolerance_split`：同一产品版本，同一 45 分钟超时槽位，物业（容忍 60）`ok`、结算（容忍 30）`unavailable`、考核 `ok`（B4 不在考核依赖范围）；
   - `missing_interval` / `late_backfill`：缺口解除后违约事实仍保留；补传后物业曲线恢复 `ok`，而结算/考核因该窗口迟到未获核验资格仍为 `unavailable`（影响槽位为 0，原因是核验资格而非缺口）；
   - `cumulative_reset`：蒸汽核验缺口，负差未计入总量；
   - `close_blocked_new_gap`：关闭被拒绝且事故回到 `open`；
   - `recurrence_after_recovery`：新事故关联既往事故，区间只有 `13:00–14:00`。
3. 在「事故指挥」手动执行恢复评估 → 关闭，观察时间线与恢复证据。
4. 在「消费影响」调整容忍范围：以 12:00–18:00 窗口为例，把容忍调到 600 分钟后，物业从 `restricted`（4 槽）变为 `ok`（0 槽），结算/考核仍为 `unavailable` 但影响槽位由 5 降到 1——判定随阈值变化，原始质量事实不变。

### 7.4 峰值需量手工验收

1. 登录后进入「峰值需量证据」。默认报告期为模拟日前一天 `2026-11-09 00:00 → 2026-11-10 00:00`，按当前模拟时钟（06:00）为截止点。
2. 切换「15 分钟格 / 区间积分」，逐项核对：

| 楼栋 | 格归集 | 区间积分 | 说明 |
| --- | --- | --- | --- |
| B1 研发楼 | 峰值 891.3 kW（14:00），96/96 完整，**已证实未超限** | 同 | 合同 900 kW；重复报文 09:00 只计一次 |
| B2 生产车间 | 已确认 801.4 kW，午间 4 格缺口，**尚未证实超限**，候选峰值格 12:00–13:00 | 确认 961.4 kW（12:00），**已证实超限** | 合同 850 kW；5 分钟读数铺满全天，积分补回格归集缺失的峰值 |
| B3 仓储中心 | 峰值 1135.4 kW，10:30 累计表回退列为未知，**已证实超限** | 同 | 合同 1000 kW；回退未产生负需量 |
| B4 综合办公楼 | 全部格跨边界 → 0 完整 / 96 部分，**尚未证实超限** | 95 完整 + 1 部分，相邻变化率上界 ≤ 700 kW，**已证实未超限** | 合同 700 kW；跨格采样使固定格留下更多未知格 |

3. 在右侧「按 02:00 截止点发布（含迟到读数）」：B2 格归集生成新版本，`delta` 显示峰值由 801.4 → 960.0 kW、峰值格 08:30 → 12:00、合同状态 `not_proven → exceeded`；旧版本仍在版本谱系中。
4. 「重放峰值验收」会重置并重跑 7 个场景（格覆盖、午间缺口、边界采样、累计表回退、区间积分、保守界、迟到峰值），每步展示从数据库读回的检查结果。

### 7.5 API 验收（curl）

```bash
curl -s -X POST localhost:8000/api/acceptance/reset
curl -s -X POST localhost:8000/api/acceptance/run -H 'content-type: application/json' -d '{}' | python3 -m json.tool | head -60
curl -s localhost:8000/api/incidents | python3 -m json.tool | head -40
# 峰值需量
curl -s localhost:8000/api/demand/meta | python3 -m json.tool | head -40
curl -s 'localhost:8000/api/demand/reports?recompute=true' | python3 -m json.tool | head -60
curl -s -X POST localhost:8000/api/demand/acceptance/run -H 'content-type: application/json' -d '{}' | python3 -m json.tool | head -40
curl -s -X POST localhost:8000/api/demand/reports -H 'content-type: application/json' -d '{"cutoff":"2026-11-10T02:00:00"}' | python3 -m json.tool | head -50
```

## 8. 数据契约（主要接口）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 演示登录 |
| GET | `/api/meta` | 时钟、楼栋、来源、产品、消费者阈值 |
| GET | `/api/sources` | 表计、采样承诺、维护窗口、产品依赖 |
| POST | `/api/sources/{code}/maintenance` | 登记维护（`recorded_at` 晚于槽位截止即事后补录） |
| POST | `/api/sources/{code}/commitments` | 变更采样频率（按段计算分母） |
| GET | `/api/health/evaluations` | 窗口评估列表（分子/分母/状态/核验） |
| GET | `/api/health/evaluations/{id}` | 逐表计明细、槽位状态、核验缺口、revision 链、违约事实 |
| POST | `/api/health/reevaluate` | 触发当前日完整重算 |
| GET | `/api/products` · `/api/products/{id}` | 两类产品的运行版本与合格范围、消费者引用 |
| GET | `/api/consumers` · `/api/consumers/{code}` | 消费目录与逐窗口影响 |
| POST | `/api/consumers/{code}/preview` | 阈值预览（不落库） |
| GET | `/api/incidents` · `/api/incidents/{id}` | 事故列表 / 时间线与证据 |
| POST | `/api/incidents/{id}/recovery-check` | 执行恢复评估 |
| POST | `/api/incidents/{id}/close` | 尝试关闭（校验恢复评估与新缺口） |
| POST | `/api/sim/action` | 样本到达/延迟/丢弃/补传/维护/换频 |
| POST | `/api/sim/clock` | 推进/设置模拟时钟（表计继续发射） |
| GET | `/api/sim/timeline` | 审计时间线 |
| POST | `/api/acceptance/run` · `/api/acceptance/reset` | 重放验收 / 重置基线 |
| GET | `/api/demand/meta` | 报告期、两种口径、楼栋合同容量、计量点模式与物理边界来源 |
| GET | `/api/demand/reports` | 按报告期发布/读取每栋楼 × 口径的报告摘要、风险区间、缺口与候选峰值格；`recompute=false` 只读 |
| POST | `/api/demand/reports` | 按显式 `cutoff` 发布报告版本（迟到读数生成新版本并给出 `delta`） |
| GET | `/api/demand/reports/{id}` | 报告逐格明细、计量点单位口径、版本谱系与结论变化 |
| GET | `/api/demand/readings` | 带采样时段/到达时间的原始读数（可按表计或楼栋） |
| GET | `/api/demand/acceptance/steps` · POST `/api/demand/acceptance/run` · `/api/demand/acceptance/reset` | 峰值需量 7 步验收的说明、重放与重置 |

完整 OpenAPI 3.1 定义见 `/openapi.json`，Swagger UI 见 `/docs`。

## 9. 目录结构

```
backend/
  app/
    config.py            # 数据库地址、窗口/槽位常量
    db.py                # 引擎、会话、初始化与种子入口
    models.py            # 来源/承诺/维护/样本/评估/事实/产品/消费者/影响/事故/恢复/审计
                         # + 峰值需量：合同容量、计量点配置、读数、报告
    seed.py              # 楼栋、来源、表计、消费者依赖、演示账号、峰值需量读数
    timeutil.py          # 槽位/窗口对齐工具
    routers/api.py       # SLO / 产品 / 影响 / 事故 / 模拟 / 验收 REST 接口
    routers/demand_api.py# 峰值需量报告、读数与验收接口
    spa.py               # 单容器模式下挂载前端构建产物
    services/
      ingest.py          # 模拟表计发射与样本生成
      health.py          # 窗口完整重算（核心评估引擎）
      products.py        # 近实时曲线 / 核验日汇总
      impact.py          # 消费影响与阈值预览
      incidents.py       # 事故状态机、恢复评估、关闭守卫
      simulator.py       # 时钟、事件入口、调度检查
      acceptance.py      # 验收叙事重放与观测读回
      demand.py          # 峰值需量：格归集 / 区间积分、覆盖分类、风险区间、合同状态、发布版本
      demand_acceptance.py # 峰值需量 7 步验收重放
  tests/                 # 38 项规则测试（含 test_demand.py 16 项）
frontend/
  src/pages/             # 产品健康、产品运行、峰值需量证据、来源登记、消费影响、事故指挥、模拟、验收、登录
  src/components/        # 布局、状态标签、SVG 曲线、槽位矩阵
  scripts/ui-smoke.tsx   # jsdom 真实数据渲染断言
Dockerfile               # 两阶段：Node 构建前端 → Python 运行 API + 静态资源
docker-compose.yml       # PostgreSQL 18 + 应用
```

## 10. 边界说明

- 平台只管理数据质量与消费影响；不控制楼宇设备、不调整真实负荷、不办理结算。
- 鉴权为演示级别，不用于生产。
- 模拟时钟为平台状态，推进时钟时表计按确定性模型继续发射样本，用于复现验收叙事。
- 峰值需量产品只输出可证实/未知范围供容量复核：`exceeded` 表示数据已足以证实超限，不触发限电；任何状态下都不生成计费金额或阶梯电价。

# 冷链温度事件分析 API

基于 **FastAPI + SQLite** 的冷链温度监控服务：接收设备按时间上报的温度读数，
按每个设备配置的上下限与容忍时长识别**连续超温事件**，返回事件起止时间、
峰值温度、累计超限时长，以及识别该事件时使用的**规则版本**。

## 功能特性

- 设备级规则配置：温度上下限、容忍时长、最大上报间隔
- **计划停运窗口**：
  - 批量创建设备停运窗口，窗口使用 **UTC 半开区间 `[start, end)`**
    （`start` 时刻属于窗口，`end` 时刻不属于）
  - 同一设备**重叠或首尾相接**的窗口持久化为合并后的区间
  - 支持按设备、按时间范围查询窗口（与查询区间有交集即返回）
  - 分析事件时窗口内读数不参与判定；窗口边界强制闭合既有事件、
    清空恢复/容忍状态，窗口前后的超限**绝不合并**；
    窗口结束后的首条超限可开启新事件
- **规则多版本机制**：
  - 更新规则时可提交 `effective_at`（生效时间），同一设备的各版本按生效时间排序
  - 查询任意时段事件时，对每条读数应用**当时已生效**的规则版本
  - 规则切换点必须结束旧事件，**禁止跨版本合并**（短暂恢复不能跨越版本边界）
  - 事件结果返回 `rule_version` 与 `rule_effective_at`
  - 提供规则版本列表接口；拒绝生效时间早于该设备最新版本的倒序写入（`409`）
  - 不传 `effective_at` 的更新保持旧行为：立即生效
- 批量温度读数上报（同一时间重复上报幂等覆盖）
- 连续超温事件识别：
  - **短暂恢复不切断**：恢复正常不超过容忍时长后再次超限，仍算同一事件
  - **间隔过长则分段**：相邻读数间隔超过 `max_gap_seconds` 强制分段
  - 恢复超过容忍时长后再超限，自动开启新事件
- 事件输出：规则版本、起止时间、事件类型（high/low/mixed）、峰值温度与时间、
  累计超限时长、是否仍在持续
- 按时间范围查询单设备或全部设备的事件

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

启动后服务地址为 **http://localhost:8000** ：

- 服务根地址：http://localhost:8000
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据持久化在命名卷 `coldchain-data`（容器内路径 `/data/coldchain.db`），
重启不丢数据。

## 示例请求

### 1. 配置设备规则

不传 `effective_at` 时与旧版本一致——立即生效：

```bash
curl -X PUT http://localhost:8000/api/v1/devices/truck-01/rule \
  -H 'Content-Type: application/json' \
  -d '{
    "upper_limit": 8.0,
    "lower_limit": 2.0,
    "tolerance_seconds": 300,
    "max_gap_seconds": 1800
  }'
```

| 字段 | 说明 |
| --- | --- |
| `upper_limit` / `lower_limit` | 温度上下限（℃），超出即超限 |
| `tolerance_seconds` | 容忍时长：恢复正常不超过该时长再超限，仍算同一事件（默认 300） |
| `max_gap_seconds` | 最大上报间隔：相邻读数间隔超过该值则强制分段（默认 1800） |
| `effective_at` | 可选。规则版本生效时间（ISO 8601，无时区按 UTC）；**不传则立即生效** |

响应包含版本号与生效时间：

```json
{
  "device_id": "truck-01",
  "version": 1,
  "upper_limit": 8.0,
  "lower_limit": 2.0,
  "tolerance_seconds": 300,
  "max_gap_seconds": 1800,
  "effective_at": "0001-01-01T00:00:00Z",
  "created_at": "2026-09-18T10:00:00+00:00",
  "updated_at": "2026-09-18T10:00:00+00:00"
}
```

> 设备的首个版本生效时间取 UTC 起点，因此它对全部历史读数都生效，
> 与引入版本机制之前“配置规则后历史读数按该规则分析”的行为一致。

### 1b. 增加一个定时生效的规则版本

```bash
curl -X PUT http://localhost:8000/api/v1/devices/truck-01/rule \
  -H 'Content-Type: application/json' \
  -d '{
    "upper_limit": 10.0,
    "lower_limit": 2.0,
    "tolerance_seconds": 300,
    "max_gap_seconds": 1800,
    "effective_at": "2026-09-18T10:00:00Z"
  }'
```

- 成功返回 `200`，`version` 递增为 `2`。
- `effective_at` 若**早于或等于**该设备最新版本的生效时间，返回 `409 Conflict`，
  即拒绝倒序写入；不会产生新版本。

### 1c. 查询规则版本列表

```bash
curl http://localhost:8000/api/v1/devices/truck-01/rule/versions
```

按 `effective_at` 升序返回该设备的全部版本：

```json
[
  {"device_id": "truck-01", "version": 1, "upper_limit": 8.0, "lower_limit": 2.0,
   "tolerance_seconds": 300, "max_gap_seconds": 1800,
   "effective_at": "0001-01-01T00:00:00Z", "created_at": "...", "updated_at": "..."},
  {"device_id": "truck-01", "version": 2, "upper_limit": 10.0, "lower_limit": 2.0,
   "tolerance_seconds": 300, "max_gap_seconds": 1800,
   "effective_at": "2026-09-18T10:00:00Z", "created_at": "...", "updated_at": "..."}
]
```

### 2. 批量创建计划停运窗口

```bash
curl -X POST http://localhost:8000/api/v1/outage-windows \
  -H 'Content-Type: application/json' \
  -d '{
    "windows": [
      {"device_id": "truck-01",
       "start": "2026-09-18T09:00:00Z",
       "end": "2026-09-18T10:00:00Z",
       "note": "车厢检修"},
      {"device_id": "truck-02",
       "start": "2026-09-18T09:30:00Z",
       "end": "2026-09-18T10:30:00Z"}
    ]
  }'
```

- 窗口为 **UTC 半开区间 `[start, end)`**：`start` 时刻属于窗口（该时刻读数
  被忽略），`end` 时刻不属于窗口（该时刻读数正常参与判定）。
- 同一设备在同一批或不同批次提交的窗口，只要**重叠或首尾相接**，
  持久化时都会合并成一个区间；不同设备互不影响。
- 成功返回 `201`，响应为受影响设备合并后的全部窗口，例如：

```json
[
  {"id": 1, "device_id": "truck-01",
   "start": "2026-09-18T09:00:00Z", "end": "2026-09-18T10:00:00Z",
   "note": "车厢检修", "created_at": "2026-09-18T08:00:00+00:00"},
  {"id": 2, "device_id": "truck-02",
   "start": "2026-09-18T09:30:00Z", "end": "2026-09-18T10:30:00Z",
   "note": "", "created_at": "2026-09-18T08:00:00+00:00"}
]
```

### 2b. 按时间查询停运窗口

```bash
# 全部设备
curl "http://localhost:8000/api/v1/outage-windows?start=2026-09-18T00:00:00Z&end=2026-09-19T00:00:00Z"

# 单个设备
curl "http://localhost:8000/api/v1/devices/truck-01/outage-windows"
```

`start` / `end` 均为可选，按半开区间 `[start, end)` 做重叠匹配：
窗口与查询区间有交集即返回（仅首尾相接不算相交）。

停运窗口对事件分析的影响：

- 窗口**内**的读数不参与任何判定（既不算超限也不算恢复，直接忽略）；
- 窗口边界是**硬边界**：窗口前的既有事件必须在边界处闭合，
  恢复/容忍状态一并清空——即使窗口两侧温度持续超限、上报间隔很短，
  窗口前后的超限也**不会合并**；
- 窗口结束后（`end` 时刻起）的首条超限可以开启一个全新事件；
- 单设备事件接口（`/devices/{id}/events`）与全量事件接口（`/events`）
  使用完全相同的逻辑。

### 3. 批量上报温度读数

```bash
curl -X POST http://localhost:8000/api/v1/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "readings": [
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:00:00Z", "temperature": 5.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:10:00Z", "temperature": 9.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:20:00Z", "temperature": 10.2},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:24:00Z", "temperature": 6.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:28:00Z", "temperature": 9.5},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:40:00Z", "temperature": 5.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T12:00:00Z", "temperature": 1.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T12:10:00Z", "temperature": 4.0}
    ]
  }'
```

时间戳为 ISO 8601 格式，无时区时按 UTC 处理。

### 4. 按时间范围查询事件

```bash
curl "http://localhost:8000/api/v1/devices/truck-01/events?start=2026-09-18T00:00:00Z&end=2026-09-19T00:00:00Z"
```

响应示例（08:24 的短暂恢复未切断第一个事件；12:00 的低温因间隔过长单独成段）。
每个事件都带有识别它时所用的规则版本 `rule_version` 及该版本的
`rule_effective_at`：

```json
[
  {
    "device_id": "truck-01",
    "rule_version": 1,
    "rule_effective_at": "0001-01-01T00:00:00Z",
    "start_time": "2026-09-18T08:10:00Z",
    "end_time": "2026-09-18T08:40:00Z",
    "ongoing": false,
    "duration_seconds": 1800.0,
    "violation_seconds": 1560.0,
    "event_type": "high",
    "peak_temp": 10.2,
    "peak_time": "2026-09-18T08:20:00Z",
    "max_temp": 10.2,
    "min_temp": 5.0,
    "reading_count": 5
  },
  {
    "device_id": "truck-01",
    "rule_version": 1,
    "rule_effective_at": "0001-01-01T00:00:00Z",
    "start_time": "2026-09-18T12:00:00Z",
    "end_time": "2026-09-18T12:10:00Z",
    "ongoing": false,
    "duration_seconds": 600.0,
    "violation_seconds": 600.0,
    "event_type": "low",
    "peak_temp": 1.0,
    "peak_time": "2026-09-18T12:00:00Z",
    "max_temp": 4.0,
    "min_temp": 1.0,
    "reading_count": 2
  }
]
```

跨越规则切换点的连续超限会被切成两个事件（即便中间没有恢复、
或恢复时长短于容忍时长，也不会跨版本合并），分别携带各自的
`rule_version` / `rule_effective_at`。

### 5. 查询全部设备的事件

```bash
curl "http://localhost:8000/api/v1/events?start=2026-09-18T00:00:00Z&end=2026-09-19T00:00:00Z"
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/api/v1/devices/{device_id}/rule` | 创建/追加设备规则版本（带 `effective_at` 定时生效，不带则立即生效） |
| `GET` | `/api/v1/devices` | 列出全部设备的最新规则版本 |
| `GET` | `/api/v1/devices/{device_id}/rule` | 查询单个设备的最新规则版本 |
| `GET` | `/api/v1/devices/{device_id}/rule/versions` | 按生效时间升序列出设备的全部规则版本 |
| `DELETE` | `/api/v1/devices/{device_id}` | 删除设备全部规则版本及其读数 |
| `POST` | `/api/v1/readings` | 批量上报温度读数 |
| `GET` | `/api/v1/devices/{device_id}/readings` | 按时间范围查原始读数 |
| `POST` | `/api/v1/outage-windows` | 批量创建计划停运窗口（重叠/首尾相接自动合并） |
| `GET` | `/api/v1/outage-windows` | 按时间范围查全部设备的停运窗口 |
| `GET` | `/api/v1/devices/{device_id}/outage-windows` | 按时间范围查单设备停运窗口 |
| `GET` | `/api/v1/devices/{device_id}/events` | 按时间范围查单设备事件（按当时生效的规则版本识别，窗口内读数不参与判定） |
| `GET` | `/api/v1/events` | 按时间范围查全部设备事件（与单设备接口相同逻辑） |
| `GET` | `/health` | 健康检查 |

## 事件判定与时长口径

- **超限**：`temperature > upper_limit` 或 `temperature < lower_limit`（边界值不算超限）。
- **按读数时刻选版本**：每条读数使用其时间戳时刻已生效的最新规则版本判定；
  早于首个版本生效时间的读数没有适用规则，不参与事件识别。
- **版本切换硬切断**：版本切换点强制结束旧事件，切换点两侧分别独立识别，
  容忍时长与短暂恢复合并均不能跨越版本边界。
- **计划停运窗口**：窗口为 UTC 半开区间 `[start, end)`。窗口内读数不参与
  判定；窗口边界与版本切换同为硬切断——闭合窗口前既有事件、清空恢复/容忍
  状态，窗口前后的超限不得合并；窗口结束后（`end` 起）首条超限开启新事件。
  跨越窗口边界的相邻读数区间不计入任何一侧的累计超限时长。
- **事件起点**：首个超限读数的时间。
- **事件终点**：最后一次超限之后首个恢复正常读数的时间；若之后没有读数，
  终点取最后超限读数时间且 `ongoing=true`。切换点（版本或停运窗口）闭合的
  旧事件，终点取该片段内最后超限读数时间，`ongoing=true`。
- **累计超限时长** `violation_seconds`：同一片段内，凡左端点超限的相邻读数
  区间长度之和；跨越版本或停运窗口边界的区间不计入任何一侧。
- **峰值** `peak_temp`：同一片段内偏离限值最大的读数温度。
- 事件在**查询时**基于全部规则版本与停运窗口实时计算，新增定时版本或
  停运窗口后重新查询即可生效。

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/          # 运行测试
uvicorn app.main:app --reload    # 本地启动（默认 coldchain.db，可用 DB_PATH 覆盖）
```

## 项目结构

```
app/
  main.py       # FastAPI 路由
  events.py     # 事件识别算法（纯函数，可单测）
  database.py   # SQLite 存储层
  schemas.py    # Pydantic 模型
tests/
  test_events.py  # 算法单元测试
  test_api.py     # API 集成测试
Dockerfile
docker-compose.yml
```

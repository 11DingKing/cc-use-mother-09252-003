# 跨国师资互访名额编排

在签证周期、课程空档、专业匹配与经费来源同时受限的条件下，安排教师跨国互访的
纯服务端系统。维护候选人、接收院校、时间窗、预算与替补顺序；生成**可解释**的
匹配方案并支持**分批确认**；临时取消时按序释放名额并触发替补；已出票安排不
自动挪动；签证拒绝、经费冻结时执行有序补位并记录责任方。

## 运行

```bash
# 默认数据库在工作目录 data/ 下（不写入源码目录）
python3 -m service_09252_003

# 可选环境变量
VISIT_HOST=0.0.0.0 VISIT_PORT=8080 VISIT_DB=/var/lib/visit/visit.db \
  python3 -m service_09252_003
```

## 测试与编译检查

```bash
python3 -m unittest discover -s tests -v     # 44 个用例
python3 -m compileall -q service_09252_003 tests
```

## 分层结构

| 模块 | 职责 |
| --- | --- |
| `models.py` | 领域模型与状态机（申请 / 名额 / 方案槽位 / 角色 / 取消原因 / 责任方） |
| `timeutil.py` | 可注入时钟、IANA 时区与日界线换算；领域时间一律为带偏移感知时间 |
| `matching.py` | 四项硬约束 + 0~100 评分分解，落选原因与得分逐项可解释 |
| `repository.py` | SQLite（WAL）持久化、`BEGIN IMMEDIATE` 串行化事务、事件流水、幂等表 |
| `services.py` | 应用服务：申请、评审、方案、占位、确认、补位、冻结、报到、结项、超时回收 |
| `api.py` | 标准库 HTTP 边界（线程服务器、身份头、幂等头、后台超时回收线程） |
| `app.py` | 装配入口；重启时先同步执行一轮超时释放并清理崩溃残留的幂等占位 |

## 状态机

```
申请 submitted → under_review → approved（可参与匹配）
     submitted/under_review → rejected | withdrawn

槽位 open（主选）→ held（占位，带 held_until）→ confirmed → checked_in → completed
     open/held/confirmed → cancelled（记录原因与责任方，释放座位并触发补位）
     held → expired（占位超时，系统自动释放并补位）
     waitlisted（有序替补）→ held（被递补）
```

## 匹配规则

硬约束（任一不满足即落选，返回具体原因）：

1. **签证周期**：候选人签证材料齐备（`visa_ready`）；
2. **时间窗**：课程空档与接收窗口按双方时区换算为 UTC 绝对区间后，交集不少于
   最短访问时长（默认 24 小时）——奥克兰（UTC+13）与洛杉矶（UTC-7）跨日界线
   的本地日期会被正确对齐；
3. **专业匹配**：候选人方向包含名额要求方向（同族方向可入选但降分）；
4. **经费**：申请预算 ≤ 名额每人预算。

评分分解：时间重叠 40 + 专业契合 25 + 预算余量 20 + 签证就绪 15。前 `seats`
名为主选，其余按分数构成**有序替补**；同分按候选人 id 稳定排序。

## 关键语义

- **并发占位**：所有写命令在单一线程化事务内完成“读—判定—写—记事件”，
  并发抢同一名额不会超卖（有专门多线程测试）。
- **幂等命令**：写请求可带 `Idempotency-Key`；同键同体回放首次结果
  （响应头 `X-Idempotent-Replayed: true`），同键不同体报 400，处理中崩溃
  残留的 pending 记录在服务重启时清理。状态层同样幂等（重复确认/报到/
  结项/取消返回既成结果）。
- **分批确认**：`POST /confirmations/batch` 逐项汇报成败，单项失败不回滚整批。
- **临时取消**：签证拒绝等取消未出票安排时，名额立即释放，替补按 rank 顺序
  递补（递补前复核硬约束，不合格者跳过并记录原因）；递补者获得全新占位期限。
- **已出票**：`ticketed=true` 的安排不参与任何自动挪动/取消；此时上报签证
  拒绝只记录责任事件，等待人工改票/废票。
- **经费冻结**：冻结期间禁止新增占位与确认；已出票安排保留，未出票安排退回
  替补序列（责任记 `funding_body`），解冻后按分数顺序有序递补。
- **超时释放**：`held_until` 到期由后台回收线程（默认 5 秒一轮）处理；
  **服务重启时同步先跑一轮**，崩溃期间到期的占位在恢复时立即释放并补位。
- **责任记录**：每次取消/冻结/超时均写 `events` 流水，含原因与责任方
  （申请人 / 派出校 / 接收校 / 经费方 / 领事馆 / 系统）。
- **权限隔离**：角色 `applicant / io / finance / admin`；国际处只能操作
  本校（`X-Actor-School`）名额；申请人只能看到本人的申请与槽位。

## HTTP 接口

身份通过请求头传递：`X-Actor-Id`、`X-Actor-Role`、`X-Actor-School`、
`X-Actor-Name`；写请求可用 `Idempotency-Key`。

| 阶段 | 接口 |
| --- | --- |
| 建档 | `POST /admin/candidates`、`POST /admin/quotas`、`GET /quotas`、`GET /candidates` |
| 申请 | `POST /applications`、`GET /applications`、`GET /applications/{id}`、`POST /applications/{id}/withdraw` |
| 评审 | `POST /applications/{id}/review`（decision=start/approve/reject） |
| 方案 | `GET /quotas/{id}/match-report`、`POST /quotas/{id}/plans`、`GET /plans/{id}`、`GET /quotas/{id}/roster` |
| 占位/锁定 | `POST /quotas/{id}/lock` |
| 确认 | `POST /slots/{id}/confirm`、`POST /confirmations/batch` |
| 替补 | `POST /slots/{id}/cancel`、`POST /slots/{id}/visa-rejection`、`POST /quotas/{id}/backfill` |
| 经费 | `POST /quotas/{id}/funding` |
| 报到/结项 | `POST /slots/{id}/check-in`、`POST /slots/{id}/complete` |
| 审计 | `GET /slots/{id}`、`GET /events?quota_id=...` |
| 运维 | `POST /system/expire-holds`、`GET /health` |

### 一分钟示例

```bash
H='-H Content-Type:application/json -H X-Actor-Id:admin -H X-Actor-Role:admin'
curl -s $H -X POST localhost:8080/admin/quotas -d '{
  "id":"Q1","host_school":"MIT","discipline":"cs","seats":1,
  "budget_per_seat":10000,"funding_source":"CSC",
  "window_start":"2026-10-01T00:00","window_end":"2026-10-15T00:00",
  "tz":"America/Los_Angeles"}'
```

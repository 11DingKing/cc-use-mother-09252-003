# 跨国师资互访名额编排

本项目用于建设面向业务人员的纯服务端系统。代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入应通过可替换端口接入，以便稳定复现状态变化。运行数据与本地配置不得写入源码目录。

在签证周期、课程空档、专业匹配和经费来源同时受限的场景下编排教师互访名额；
临时取消、签证拒绝或经费冻结时按替补顺序自动补位并记录责任归属。

## 架构

```
service_09252_003/
├── models.py     # 领域枚举：角色、席位状态机、申请状态、责任归属、评分权重
├── timeutil.py   # UTC 瞬间（微秒整数）与跨时区时间窗解析（zoneinfo）
├── matcher.py    # 可解释匹配引擎：专业/空档/经费/签证四类约束 + 加权评分
├── storage.py    # SQLite 持久化：BEGIN IMMEDIATE 事务 + 条件更新（并发占位）
├── service.py    # 应用服务：申请/评审/锁定/出票/报到/结项/取消/级联/补位
├── api.py        # HTTP JSON 接口（标准库 http.server），认证与幂等键
├── reaper.py     # 超时释放回收器（后台线程）
├── ports.py      # 可替换端口：时钟与 ID 生成器（测试注入 FakeClock）
└── __main__.py   # 服务入口
```

关键设计：

- **时间**：内部一律使用 UTC 微秒瞬间；时间窗支持带偏移 RFC3339 或
  `{"start","end","timezone"}`（IANA 时区）两种输入，跨日界线比较在 UTC 轴上进行。
- **名额即槽位**：建名额时物化 `capacity` 个槽位行，占位/锁定/出票都是
  槽状态翻转（`available→held→locked→ticketed→checked_in→completed`），
  用条件更新保证并发占位安全。
- **已出票不可挪动**：`ticketed` 及之后状态不会被取消、签证拒绝或经费冻结
  自动改动，只记录 `ticketed_protected` 责任事件，等待人工处理。
- **有序补位**：席位释放（取消/拒签/冻结/超时）后，先给替补队列中“已批准”
  的申请，再给“待评审”的，各自按申请时间排序；补位前重新评估约束。
- **超时释放**：占位/锁定带 TTL，后台回收器周期释放并补位；服务启动时
  先清扫一轮，重启后继续执行。
- **幂等**：所有写命令支持 `Idempotency-Key`（同键同请求重放响应，同键不同
  请求 409）；同人同名额重复申请天然去重；状态机重复转换返回 `already`。
- **责任记录**：所有状态变化写入事件流（操作者、责任归属、实体、数据），
  可按实体查询。
- **权限隔离**：国际处（coordinator）、教师（teacher，仅本人）、
  派出院校管理员（home_admin，仅本院校）、接收院校管理员（host_admin，
  仅本院校名额）、经费（finance，冻结与审计）。

## 运行

```bash
python3 -m service_09252_003 --db /path/to/exchange.db --port 8080
# 数据库路径也可用环境变量 EXCHANGE_DB_PATH 指定，默认在用户数据目录
```

首次启动会初始化演示用户（token：`token-coordinator` / `token-finance` /
`token-home-admin` / `token-host-admin`）。

## API 一览

认证：除 `GET /health` 外均需 `X-Token` 头；写命令可带 `Idempotency-Key` 头。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/candidates` | 登记候选人（专业、课程空档窗、签证状态、经费来源） |
| GET | `/candidates` `/candidates/{id}` | 查询（按角色隔离） |
| POST | `/candidates/{id}/visa-rejected` | 签证拒绝：级联取消未出票安排并补位 |
| POST | `/candidates/{id}/funding-frozen` | 经费冻结：同上，责任归经费方 |
| POST | `/quotas` | 登记名额（接收院校、专业、时间窗、容量、经费来源、TTL） |
| GET | `/quotas` `/quotas/{id}` | 查询名额与槽位 |
| POST | `/quotas/{id}/close` | 关闭名额（停止新申请与补位） |
| GET | `/quotas/{id}/proposal` | 可解释匹配方案（逐项约束、评分、推荐理由） |
| POST | `/quotas/{id}/batch-lock` | 分批锁定：逐项结算，支持部分确认 |
| POST | `/applications` | 申请（自动占位或进入替补队列；重复申请去重） |
| GET | `/applications` `/applications/{id}` | 查询（按角色隔离） |
| POST | `/applications/{id}/review` | 评审 `{"decision":"approve"\|"reject"}` |
| POST | `/applications/{id}/ticket` | 出票（仅锁定状态；之后不可自动挪动） |
| POST | `/applications/{id}/checkin` | 报到 |
| POST | `/applications/{id}/closeout` | 结项（附结项报告） |
| POST | `/applications/{id}/cancel` | 取消并触发补位 |
| GET | `/slots/{id}` | 席位详情 |
| GET | `/events?entity_type=&entity_id=` | 责任事件流 |
| GET/POST | `/users` | 用户管理（仅国际处） |

错误响应统一为 `{"error": {"code", "message", "details?"}}`；
时间字段同时给出 UTC 微秒整数与 `*_iso` 可读形式。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：跨时区日界线、匹配可解释性、全生命周期、取消/拒签/冻结级联补位、
并发占位、分批/部分确认、幂等键与天然幂等、超时释放与服务重启恢复、
角色权限隔离、HTTP 端到端。

## 编译检查

```bash
python3 -m compileall -q service_09252_003 tests
```

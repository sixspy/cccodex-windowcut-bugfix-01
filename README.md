# 业务窗关账

> 遵守 `ops/底座生成前必读_效率与技术栈.md`（**≥3× 厚度**）。  
> FastAPI + Vue 3 + SQLite；交付须 `docker compose up -d --build` 一键起。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立。

## 骨架一句话

事件按业务窗裁切，迟到进旁路或拒收，关窗后物化结果并与窗口对账。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 业务窗关账 |
| 目录 | `08-windowcut` |
| 接口 | http://127.0.0.1:11026 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 厚度（相对薄壳 ≥3×）

| 项 | 目标 |
|----|------|
| 业务 LOC | ≥900（目标 1000+） |
| 后端具名模块 | `db` / `auth` / `locks` / `windows_svc` / `ingest_svc` / `late_svc` / `materialize_svc` / `reconcile_svc` / `audit_svc` / `hold_svc` / `reopen_svc` |
| 前端视图 | 业务窗 / 入账 / 迟到桶 / 物化 / 对账 / 审计争用 |
| 空桩 | hold 晋升、迟到补入、分区物化、窗重开 |

## 为何能出多题

开窗关窗、迟到策略、窗重开、部分物化失败回滚、旁路暂存 vs 拒收、争用锁与审计。

## 互斥切口候选

- 迟到拒收 vs 进迟到桶
- 物化失败整窗回滚 vs 保留成功分区
- 窗重开作废重算 vs 增量补丁合并
- 切版旁路暂存 vs 开始瞬间拒收

## 关窗禁写入册（本仓落地口径）

关窗后入账的写路径已钉死为 **乙路径**，并写入 `write_policy_ledger`（当日唯一口径，当天禁止改口）：

- 已关闭窗后入账 → 同一事务写 `events(disposition='迟到桶', window_id=NULL)` + `late_bucket`；任一腿失败两侧回滚，整笔不落。
- 不进窗内合计、不写拒收凭证；「窗外且窗仍开启」的拒收是另一条规则，与本口径不混用。
- 请求若显式携带与入册口径不同的 `write_policy`，整笔拒写，审计记 `write_policy_deviation`（禁写偏了）。
- 关窗时按「窗 + 世代」封存口径到 `window_seal_ledger`；改口请求 `POST /api/write-policy/change` 当日一律 409。
- 两人同时对同一已关窗入账须占 `ingest:<窗名>` 锁；后到只记争用（`contention_log`），零业务写入。

五个面同态：事件列表 / 迟到桶 / 拒收凭证 / 对账 / 审计。
口径查看：`GET /api/write-policy`。

## 启动（一键）

```powershell
cd projects/08-windowcut
docker compose up -d --build
```

浏览器：http://127.0.0.1:11026/  
健康检查：GET http://127.0.0.1:11026/api/health

本机开发：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10605
python -m pytest -q
```

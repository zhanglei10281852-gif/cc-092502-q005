# 考古研究协作基础服务

这是一个供考古项目扩展业务模块的纯后端基础服务，提供研究项目登记、成员与角色、会话认证、审计事件、幂等请求和可恢复后台任务。服务使用 FastAPI 与 SQLite，不依赖另行部署的数据库、缓存或队列。

## 环境与安装

运行环境为 Python 3.11。安装开发依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

基础接口包括 `/api/system/health`、`/api/projects`、`/api/users`、`/api/sessions`、`/api/audit` 和 `/api/jobs`。首次启动后可用命令行创建管理员，也可以通过测试夹具构造隔离数据库。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内检查根路径、健康接口、数据库外键和 WAL 配置。

## 扩展约定

新研究模块应通过独立路由、服务和仓储接入，跨表写入放在即时事务中。外部标识、幂等键和审计载荷应保存原始值及规范化值；后台任务使用 SQLite 租约，不允许依赖外部队列。用户口令和会话令牌只保存摘要，审计事件会过滤密码、令牌等敏感字段。

## 动物遗存鉴定与量化模块（app/zooarch）

独立的动物考古模块，路由挂在 `/api/zooarch/projects/{project_id}/...`，复用项目成员角色：记录员及以上可录入，审核角色（owner/researcher/reviewer）可修订与确认拼合，viewer 只读。

### 鉴定记录

每条记录保存分类层级（`taxon_path`/`taxon_rank`）、骨骼部位（`element`）、左右侧（`side`）、年龄阶段（`age_stage`）、保存比例（`portion`，0–1]）、烧灼与切割痕迹（`burned`/`cut_marks`）、空间来源（`unit`/`layer`/`square`/`bag`）、鉴定置信度（`confidence`，0–1）与碎片数。来源不完整（缺 `unit` 或 `layer`）的记录状态为 `staged`（暂存），不进入已发布统计；补全后经修订自动转为 `published`。`record_key` 在项目内唯一，相同内容重复提交幂等返回，内容不同返回 409。

### 量化口径（NISP/MNE/MNI）

- NISP（碎片数）：纳入统计记录的碎片数之和。
- MNE（最小单元数）：按（部位 × 侧 × 年龄阶段）统计单元；未确认的碎片各自计为一个单元，经审核确认的拼合组件合并为一个单元。
- MNI（最小个体数）：按 分组 × 分类单元 × 年龄类 计算可解释下界——左右配对只在同一年龄类内进行（年龄不相容不合并），未知侧单元先与已知侧配对、剩余两两配对，唯一部位（规则集 `unique_elements`）不配对；分类单元 MNI 为各年龄类内取部位最大值后跨年龄类求和。结果含每个部位的配对数、未配对数、驱动部位等贡献明细。

分组范围由调用方通过 `group_by`（`unit`/`layer`/`square`/`bag` 的任意子集）选定；量化规则（最低置信度、唯一部位、年龄类映射、拼合同单位要求）保存在可版本化的 `rulesets` 中，版本不可变。暂存与低置信记录计入 `excluded` 明细但不参与统计。

### 修订与报告

鉴定修订通过 `POST .../records/{id}/revisions` 以审核事件追加（乐观锁 `base_version`，冲突返回 409），每次写入生成完整版本快照。`POST .../reports` 创建报告时固定当时的记录版本、规则快照与已确认拼合，`POST .../reports/{id}/recompute` 始终按固定版本重算并校验哈希，历史报告不受后续修订影响。

### 跨袋拼合

`POST .../refits/scan` 按同分类、同部位、侧别相容、年龄类相容、保存比例互补（和 ≤ 1）且不同袋的规则生成候选，只建议不合并。确认某候选时，共享同一记录的其他候选被互斥排除；撤销确认后恢复。确认前会按记录当前状态重新校验，已失效的候选返回 409。

### 可复现

量化核心 `app/zooarch/mni.py` 是纯函数，HTTP 与命令行共用：

```bash
python -m app.zooarch.cli quantify --input input.json   # 固定 JSON 输入复现计算
python -m app.zooarch.cli report --project-id 1 --name 季报 --group-by unit,layer
python -m app.zooarch.cli recompute --report-id 1       # 按固定版本重算并校验
```

输入 JSON 形如 `{"records": [...], "ruleset": {...}, "group_by": ["layer"], "refit_groups": [[1, 2]]}`，输出与 HTTP 量化接口一致的 `input_hash`/`result_hash`。

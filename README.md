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

## 动物遗存鉴定与量化模块

在基础服务之上提供动物考古业务接口，挂载于 `/api/projects/{project_id}/zoo/`：

- **鉴定记录**：每条记录保存分类层级、骨骼部位、左右侧、年龄阶段、保存比例、烧灼/切割痕迹、空间来源（遗迹单位、层位、探方、袋号）与鉴定置信度。来源字段（遗迹单位、层位）不完整的记录只能暂存（`staged`），不得进入已发布统计；只有 `published` 记录参与量化。
- **版本化修订**：记录不可变版本化，修订通过审核事件（`create`/`revise`/`publish`/`unpublish`）追加；修订须携带 `base_version_no`，过期提交返回 409，保证并发审核下恰有一方成功。
- **量化报告**：`POST /reports` 按用户选定的分组范围（`layer`、`context_unit`、`grid`、`bag` 的任意组合）与指定规则版本计算 NISP（碎片数）、MNE（最小单元数）、MNI（最小个体数），并返回贡献明细（单元成员、年龄分层、取值依据、被排除的低置信记录）。MNI 下界由左右配对、年龄不相容与唯一部位三条可解释规则合成。报告生成时冻结记录版本快照与输入/结果摘要，`POST /reports/{id}/verify` 按当时版本重算校验，记录后续修订不影响历史报告。
- **量化规则**：`POST /rules` 按 `rule_code` 追加版本，旧版本自动标记 `superseded`；每份报告引用固定规则版本，参数含最低置信度、保存比例装箱、年龄秩次与容差、侧别未知处理、每部位个体数与唯一部位列表。
- **拼合候选**：`POST /refits/detect` 识别跨袋、同部位同侧（或侧别未知）、保存比例互补、年龄相容的候选对，只标记不合并；确认具有互斥性（共享记录的其它候选自动阻塞），`confirm`/`reject`/`undo` 决策均记录事件，撤销后互斥阻塞自动解除。

### 命令行复现

```bash
python -m app.zoo_cli quant --records records.json --rules rules.json --grouping layer,context_unit
python -m app.zoo_cli verify-report --project-id 1 --report-id 1
```

`quant` 对固定输入文件执行与 HTTP 接口完全相同的计算并输出输入/结果摘要；同一批记录经 HTTP 报告与 CLI 计算得到的 `result_hash` 一致。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入、后台任务领取与完成，以及动物遗存模块的分组边界、低置信记录排除、暂存/发布门禁、版本化修订与并发审核冲突、报告历史版本重算、规则版本冻结、拼合候选互斥与撤销、SQLite 重启前后一致性和命令行复现。

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

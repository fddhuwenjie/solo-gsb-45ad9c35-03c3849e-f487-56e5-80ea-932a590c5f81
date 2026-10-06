# 学分互认 HTTP API（Python · FastAPI · SQLite）

外校课程学分计入本校培养要求的登记、批量互认、成绩更正、复核与撤销服务。
成绩更正使互认失效时自动转为**待复核**并停止计分但保留两端占用；恢复后必须**显式复核**才能重新计分。

## 一条命令启动

```bash
docker compose up --build
```

- 监听端口：**8000**（http://localhost:8000 ）
- 交互式文档：http://localhost:8000/docs
- SQLite 数据文件位于命名卷 `credits-data`（容器内 `/data/credits.db`），
  **重启 / 重建容器后记录仍在**；表结构在应用启动时自动初始化。

本地裸机运行（可选）：

```bash
pip install -r requirements.txt
DB_PATH=./credits.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 核心规则

| 状态 | 含义 | 是否计入进度 | 是否占用源课/要求 |
| --- | --- | --- | --- |
| `approved`（已认） | 外课学分足额、成绩达到替代最低线 | 是，按**要求所需学分**计入 | 是 |
| `pending`（待复核） | 成绩更正后不再达标 | 否 | **是**（名额不被别的申请抢走） |
| 空缺（vacant） | 要求尚无已认或待复核记录 | 否 | 否 |

- **批量原子性**：一次提交多条互认，仅当每条都满足
  ① 课程与要求均已登记 ② 存在可替代关系 ③ 成绩 ≥ 最低成绩 ④ 外课学分 ≥ 要求所需学分，
  且源课与要求在**本批内**与**现存已认/待复核记录**中均未重复时，整批才写入；
  任一条件不满足则整批不写（422 资格问题 / 409 占用冲突）。
- **成绩更正**：已认互认因此不合格 → `approved → pending`，停止计分、保留占用；
  成绩恢复**不会**自动重新计分，须显式复核。
- **复核**：重新判定，合格 → `approved` 并恢复计分；仍不合格 → 维持 `pending`（422）。
- **撤销**：删除记录（已认/待复核均可），立即释放两端占用，可重新申请。
- **并发安全**：写事务使用 `BEGIN IMMEDIATE` 串行化；
  活跃记录对源课、要求各有唯一部分索引兜底，不允许重复使用或读取中间态。

## 可执行 HTTP 示例

```bash
# 1) 登记外校课程：编号 C1，4 学分，88 分（成绩必须 0~100）
curl -sS -X PUT http://localhost:8000/students/2026001/courses/C1 \
  -H 'Content-Type: application/json' -d '{"credits": 4, "score": 88}'

# 2) 登记本校要求：编号 R1，需要 4 学分
curl -sS -X PUT http://localhost:8000/students/2026001/requirements/R1 \
  -H 'Content-Type: application/json' -d '{"required_credits": 4}'

# 3) 登记可替代关系：C1 可替代 R1，最低成绩 60
curl -sS -X POST http://localhost:8000/students/2026001/courses/C1/substitutions/R1 \
  -H 'Content-Type: application/json' -d '{"min_score": 60}'

# 4) 整批提交互认（可多条；全部满足才整批确认）
curl -sS -X POST http://localhost:8000/students/2026001/recognitions \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"course_code":"C1","requirement_code":"R1"}]}'

# 5) 查询进度：区分 approved / pending / vacant
curl -sS http://localhost:8000/students/2026001/progress
curl -sS http://localhost:8000/students/2026001/recognitions?status=approved
curl -sS http://localhost:8000/students/2026001/recognitions?status=pending
curl -sS http://localhost:8000/students/2026001/vacancies

# 6) 成绩更正为 55 分（< 60）：已认自动转待复核，停止计分但保留占用
curl -sS -X PATCH http://localhost:8000/students/2026001/courses/C1/score \
  -H 'Content-Type: application/json' -d '{"score": 55}'

# 7) 成绩恢复后显式复核（恢复为合格成绩后调用；仍不合格则维持 pending）
curl -sS -X PATCH http://localhost:8000/students/2026001/courses/C1/score \
  -H 'Content-Type: application/json' -d '{"score": 90}'
curl -sS -X POST http://localhost:8000/recognitions/1/review \
  -H 'Content-Type: application/json' -d '{}'

# 8) 撤销互认：释放源课与要求占用（id 来自第 4 步返回）
curl -sS -i -X DELETE http://localhost:8000/recognitions/1
```

> Windows cmd/PowerShell 下请把 JSON 中的单引号换成双引号并转义内部引号。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/students/{sid}/courses/{code}` | 登记外校课程（学分正整数、成绩 0~100），重复 409 |
| PUT | `/students/{sid}/requirements/{code}` | 登记本校要求（所需学分为正整数），重复 409 |
| POST | `/students/{sid}/courses/{cc}/substitutions/{rc}` | 登记可替代关系与最低成绩 |
| POST | `/students/{sid}/recognitions` | 整批互认申请：全合格且无占用冲突才整批 201 |
| PATCH | `/students/{sid}/courses/{code}/score` | 成绩更正，驱动已认→待复核 |
| POST | `/recognitions/{id}/review` | 显式复核：合格恢复已认，不合格维持待复核(422) |
| DELETE | `/recognitions/{id}` | 撤销，释放占用（204） |
| GET | `/students/{sid}/recognitions?status=` | 已认 / 待复核 / 全部 |
| GET | `/students/{sid}/progress` | 按要求汇总：approved / pending / vacant 与已计学分 |
| GET | `/students/{sid}/vacancies` | 空缺要求列表 |
| GET | `/health` | 健康检查 |

## 测试

端到端测试通过真实 HTTP 接口运行，覆盖批量原子性、状态流转、并发申请与更正/复核交错、重启持久化：

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. python3 -m tests.test_flow     # 或 pytest -q
```

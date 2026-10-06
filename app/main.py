"""学分互认 HTTP API（FastAPI + SQLite）。

业务规则速览：
- recognitions 只有 approved / pending 两个活跃状态；撤销=硬删除，释放两端占用。
- approved：达标且足额，按要求所需学分计入进度。
- pending ：成绩更正后不再达标——不计分，但继续占用源课与要求；
            恢复后必须显式复核（POST review）才可能重新计分；
            复核仍不合格维持 pending。
- 批量互认：批内源课/要求不重复、与现存已认或待复核记录不冲突、
            每条都“已登记 + 可替代 + 成绩达标 + 外课学分足额”才整批写入，
            否则整批不写。
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from . import db
from .schemas import (
    CourseIn,
    RecognitionBatchIn,
    RecognitionItem,
    RequirementIn,
    ReviewIn,
    ScoreIn,
    SubstitutionIn,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    # 数据库初始化：Dockerfile/Compose 部署时容器一启动即建表，
    # 数据文件位于挂载卷 /data/credits.db，重启后记录仍在。
    db.init_db()
    yield


app = FastAPI(title="学分互认 API", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #

def _get_course(conn: sqlite3.Connection, student_id: str, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM courses WHERE student_id=? AND course_code=?",
        (student_id, code),
    ).fetchone()


def _get_requirement(
    conn: sqlite3.Connection, student_id: str, code: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM requirements WHERE student_id=? AND requirement_code=?",
        (student_id, code),
    ).fetchone()


def _get_substitution(
    conn: sqlite3.Connection, student_id: str, course_code: str, requirement_code: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM substitutions "
        "WHERE student_id=? AND course_code=? AND requirement_code=?",
        (student_id, course_code, requirement_code),
    ).fetchone()


def _evaluate(
    conn: sqlite3.Connection, student_id: str, course_code: str, requirement_code: str
) -> tuple[bool | None, str | None]:
    """基于当前课程成绩/学分、要求所需学分与替代最低成绩重新判定。

    返回 (是否合格, 原因)：任一实体缺失时原因描述缺失项。
    """
    course = _get_course(conn, student_id, course_code)
    requirement = _get_requirement(conn, student_id, requirement_code)
    if course is None:
        return None, "course_not_registered"
    if requirement is None:
        return None, "requirement_not_registered"
    sub = _get_substitution(conn, student_id, course_code, requirement_code)
    if sub is None:
        return False, "no_substitution"
    if course["score"] < sub["min_score"]:
        return False, "score_below_minimum"
    if course["credits"] < requirement["required_credits"]:
        return False, "insufficient_credits"
    return True, None


def _recognition_dict(row: sqlite3.Row, qualified: bool | None = None,
                      reason: str | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "student_id": row["student_id"],
        "course_code": row["course_code"],
        "requirement_code": row["requirement_code"],
        "status": row["status"],
        "reason": row["reason"],
        "qualified_now": qualified,
        "live_reason": reason,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --------------------------------------------------------------------------- #
# 登记：外校课程 / 本校要求 / 可替代关系
# --------------------------------------------------------------------------- #

@app.put("/students/{student_id}/courses/{course_code}", status_code=201)
def register_course(
    student_id: str, course_code: str, body: CourseIn
) -> dict[str, Any]:
    """登记外校课程（编号、正整数学分、0~100 成绩）。重复登记返回 409。"""
    with db.write_tx() as conn:
        try:
            conn.execute(
                "INSERT INTO courses (student_id, course_code, credits, score) "
                "VALUES (?,?,?,?)",
                (student_id, course_code, body.credits, body.score),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail={"code": "course_already_registered",
                        "course_code": course_code},
            )
    return {"student_id": student_id, "course_code": course_code,
            "credits": body.credits, "score": body.score}


@app.put("/students/{student_id}/requirements/{requirement_code}", status_code=201)
def register_requirement(
    student_id: str, requirement_code: str, body: RequirementIn
) -> dict[str, Any]:
    """登记本校毕业/培养要求（编号、所需学分）。重复登记返回 409。"""
    with db.write_tx() as conn:
        try:
            conn.execute(
                "INSERT INTO requirements (student_id, requirement_code, required_credits) "
                "VALUES (?,?,?)",
                (student_id, requirement_code, body.required_credits),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail={"code": "requirement_already_registered",
                        "requirement_code": requirement_code},
            )
    return {"student_id": student_id, "requirement_code": requirement_code,
            "required_credits": body.required_credits}


@app.post(
    "/students/{student_id}/courses/{course_code}/substitutions/{requirement_code}",
    status_code=201,
)
def register_substitution(
    student_id: str, course_code: str, requirement_code: str, body: SubstitutionIn
) -> dict[str, Any]:
    """登记“课程可替代某要求”及其最低成绩。重复登记返回 409。"""
    with db.write_tx() as conn:
        if _get_course(conn, student_id, course_code) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "course_not_registered", "course_code": course_code},
            )
        if _get_requirement(conn, student_id, requirement_code) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "requirement_not_registered",
                        "requirement_code": requirement_code},
            )
        try:
            conn.execute(
                "INSERT INTO substitutions "
                "(student_id, course_code, requirement_code, min_score) "
                "VALUES (?,?,?,?)",
                (student_id, course_code, requirement_code, body.min_score),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail={"code": "substitution_already_registered",
                        "course_code": course_code,
                        "requirement_code": requirement_code},
            )
    return {"student_id": student_id, "course_code": course_code,
            "requirement_code": requirement_code, "min_score": body.min_score}


# --------------------------------------------------------------------------- #
# 批量互认申请
# --------------------------------------------------------------------------- #

@app.post("/students/{student_id}/recognitions", status_code=201)
def submit_recognitions(
    student_id: str, body: RecognitionBatchIn
) -> dict[str, Any]:
    """一次提交多条互认。

    仅当每条都：实体齐全、存在替代关系、成绩达标、外课学分足额，
    且源课与要求在本批内部及现存“已认/待复核”记录中均未被占用，
    才整批确认（approved）并计入进度；否则整批不写。
    """
    items: list[RecognitionItem] = body.items

    # 批内重复检查（源课与要求均不得重复）。
    batch_courses: set[str] = set()
    batch_requirements: set[str] = set()
    batch_pairs: set[tuple[str, str]] = set()
    for item in items:
        if item.course_code in batch_courses:
            raise HTTPException(
                status_code=409,
                detail={"code": "duplicate_course_in_batch",
                        "course_code": item.course_code},
            )
        if item.requirement_code in batch_requirements:
            raise HTTPException(
                status_code=409,
                detail={"code": "duplicate_requirement_in_batch",
                        "requirement_code": item.requirement_code},
            )
        if (item.course_code, item.requirement_code) in batch_pairs:
            raise HTTPException(
                status_code=409,
                detail={"code": "duplicate_recognition_in_batch",
                        "course_code": item.course_code,
                        "requirement_code": item.requirement_code},
            )
        batch_courses.add(item.course_code)
        batch_requirements.add(item.requirement_code)
        batch_pairs.add((item.course_code, item.requirement_code))

    with db.write_tx() as conn:
        # 1) 与现存已认或待复核记录冲突检查（占用优先：名额不可被抢走，
        #    即使新申请自身不合格，也先报冲突）。
        for item in items:
            row = conn.execute(
                "SELECT status, course_code, requirement_code FROM recognitions "
                "WHERE student_id=? AND status IN ('approved','pending') "
                "AND (course_code=? OR requirement_code=?)",
                (student_id, item.course_code, item.requirement_code),
            ).fetchone()
            if row is not None:
                occupied = (
                    "course_code" if row["course_code"] == item.course_code
                    else "requirement_code"
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "occupied",
                        "conflict_on": occupied,
                        "existing_status": row["status"],
                        "course_code": item.course_code,
                        "requirement_code": item.requirement_code,
                    },
                )

        # 2) 资格判定（实体存在 / 替代关系 / 达标 / 足额）。
        for item in items:
            ok, why = _evaluate(
                conn, student_id, item.course_code, item.requirement_code
            )
            if not ok:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "item_not_eligible",
                        "reason": why,
                        "course_code": item.course_code,
                        "requirement_code": item.requirement_code,
                    },
                )

        # 3) 整批插入。唯一部分索引做最后防线（并发下返回 409，整批回滚）。
        created: list[dict[str, Any]] = []
        try:
            for item in items:
                cur = conn.execute(
                    "INSERT INTO recognitions "
                    "(student_id, course_code, requirement_code, status, reason) "
                    "VALUES (?,?,?,'approved',NULL)",
                    (student_id, item.course_code, item.requirement_code),
                )
                row = conn.execute(
                    "SELECT * FROM recognitions WHERE id=?", (cur.lastrowid,)
                ).fetchone()
                created.append(_recognition_dict(row, qualified=True, reason=None))
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail={"code": "occupied", "reason": "concurrent_conflict"},
            )

    return {"student_id": student_id, "recognitions": created}


# --------------------------------------------------------------------------- #
# 成绩更正（驱动已认 -> 待复核）
# --------------------------------------------------------------------------- #

@app.patch("/students/{student_id}/courses/{course_code}/score")
def correct_score(
    student_id: str, course_code: str, body: ScoreIn
) -> dict[str, Any]:
    """更正外课成绩。

    更正使某条已认互认不再达标时：approved -> pending，停止计分，
    但保留两端占用（其他申请仍不能抢走源课或要求）。
    pending 记录仅刷新不合格原因。成绩恢复不会自动重新 approved。
    """
    with db.write_tx() as conn:
        course = _get_course(conn, student_id, course_code)
        if course is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "course_not_registered", "course_code": course_code},
            )

        conn.execute(
            "UPDATE courses SET score=? WHERE student_id=? AND course_code=?",
            (body.score, student_id, course_code),
        )

        actives = conn.execute(
            "SELECT * FROM recognitions "
            "WHERE student_id=? AND course_code=? AND status IN ('approved','pending')",
            (student_id, course_code),
        ).fetchall()

        transitions: list[dict[str, Any]] = []
        for row in actives:
            qualified, why = _evaluate(
                conn, student_id, row["course_code"], row["requirement_code"]
            )
            if row["status"] == "approved" and not qualified:
                conn.execute(
                    "UPDATE recognitions SET status='pending', reason=?, "
                    "updated_at=datetime('now') WHERE id=?",
                    (why, row["id"]),
                )
                new_row = conn.execute(
                    "SELECT * FROM recognitions WHERE id=?", (row["id"],)
                ).fetchone()
                transitions.append(
                    {"id": row["id"],
                     "requirement_code": row["requirement_code"],
                     "from_status": "approved", "to_status": "pending",
                     "reason": why,
                     "recognition": _recognition_dict(new_row, False, why)}
                )
            elif row["status"] == "pending":
                # 占用保留；刷新原因（可能恢复合格——仍不自动计分）。
                conn.execute(
                    "UPDATE recognitions SET reason=?, updated_at=datetime('now') "
                    "WHERE id=?",
                    (why, row["id"]),
                )
                new_row = conn.execute(
                    "SELECT * FROM recognitions WHERE id=?", (row["id"],)
                ).fetchone()
                transitions.append(
                    {"id": row["id"],
                     "requirement_code": row["requirement_code"],
                     "from_status": "pending", "to_status": "pending",
                     "reason": why,
                     "recognition": _recognition_dict(new_row, qualified, why)}
                )

    return {"student_id": student_id, "course_code": course_code,
            "score": body.score, "transitions": transitions}


# --------------------------------------------------------------------------- #
# 显式复核与撤销
# --------------------------------------------------------------------------- #

@app.post("/recognitions/{recognition_id}/review")
def review_recognition(
    recognition_id: int, body: ReviewIn
) -> dict[str, Any]:
    """显式复核待复核记录。

    重新判定仍不合格 -> 维持 pending（422）；合格 -> approved 并重新计分。
    已经是 approved 的视为幂等成功。
    """
    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM recognitions WHERE id=?", (recognition_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "recognition_not_found", "id": recognition_id},
            )

        qualified, why = _evaluate(
            conn, row["student_id"], row["course_code"], row["requirement_code"]
        )
        if not qualified:
            conn.execute(
                "UPDATE recognitions SET reason=?, updated_at=datetime('now') "
                "WHERE id=?",
                (why, recognition_id),
            )
            fresh = conn.execute(
                "SELECT * FROM recognitions WHERE id=?", (recognition_id,)
            ).fetchone()
            raise HTTPException(
                status_code=422,
                detail={"code": "still_not_qualified", "reason": why,
                        "status": fresh["status"],
                        "recognition": _recognition_dict(fresh, False, why)},
            )

        conn.execute(
            "UPDATE recognitions SET status='approved', reason=NULL, "
            "updated_at=datetime('now') WHERE id=?",
            (recognition_id,),
        )
        fresh = conn.execute(
            "SELECT * FROM recognitions WHERE id=?", (recognition_id,)
        ).fetchone()

    return {"status": fresh["status"], "recognition": _recognition_dict(fresh, True)}


@app.delete("/recognitions/{recognition_id}", status_code=204)
def revoke_recognition(recognition_id: int) -> None:
    """撤销互认（approved 或 pending 均可）：硬删除，立即释放两端占用。"""
    with db.write_tx() as conn:
        cur = conn.execute("DELETE FROM recognitions WHERE id=?", (recognition_id,))
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail={"code": "recognition_not_found", "id": recognition_id},
            )


# --------------------------------------------------------------------------- #
# 查询：已认 / 待复核 / 空缺
# --------------------------------------------------------------------------- #

def _list_records(conn: sqlite3.Connection, student_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*, s.min_score,
               CASE WHEN s.min_score IS NULL THEN 0 ELSE 1 END AS has_sub
          FROM recognitions r
          LEFT JOIN substitutions s
            ON s.student_id = r.student_id
           AND s.course_code = r.course_code
           AND s.requirement_code = r.requirement_code
         WHERE r.student_id = ?
         ORDER BY r.id
        """,
        (student_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        course = _get_course(conn, student_id, row["course_code"])
        req = _get_requirement(conn, student_id, row["requirement_code"])
        if course is None or req is None or not row["has_sub"]:
            qualified, why = False, "missing_entity_or_substitution"
        elif course["score"] < row["min_score"]:
            qualified, why = False, "score_below_minimum"
        elif course["credits"] < req["required_credits"]:
            qualified, why = False, "insufficient_credits"
        else:
            qualified, why = True, None
        result.append(_recognition_dict(row, qualified, why))
    return result


@app.get("/students/{student_id}/recognitions")
def list_recognitions(
    student_id: str,
    status_filter: str | None = Query(
        None, alias="status", pattern="^(approved|pending)$",
        description="approved=已认；pending=待复核；不传=全部",
    ),
) -> dict[str, Any]:
    """列出互认记录，可按已认/待复核过滤。"""
    with db.read_tx() as conn:
        records = _list_records(conn, student_id)
    if status_filter:
        records = [r for r in records if r["status"] == status_filter]
    return {"student_id": student_id, "recognitions": records}


@app.get("/students/{student_id}/progress")
def get_progress(student_id: str) -> dict[str, Any]:
    """按本校要求汇总进度，明确区分 approved / pending / vacant。"""
    with db.read_tx() as conn:
        req_rows = conn.execute(
            "SELECT * FROM requirements WHERE student_id=? ORDER BY requirement_code",
            (student_id,),
        ).fetchall()
        records = _list_records(conn, student_id)

    by_req = {r["requirement_code"]: r for r in records}
    progress: list[dict[str, Any]] = []
    totals = {"approved": 0, "pending": 0, "vacant": 0,
              "required_credits": 0, "awarded_credits": 0}

    for req in req_rows:
        rec = by_req.get(req["requirement_code"])
        totals["required_credits"] += req["required_credits"]
        if rec is None:
            state = "vacant"
            entry = {
                "requirement_code": req["requirement_code"],
                "required_credits": req["required_credits"],
                "state": state,
                "awarded_credits": 0,
                "recognition": None,
            }
        else:
            state = "approved" if rec["status"] == "approved" else "pending"
            awarded = req["required_credits"] if state == "approved" else 0
            totals["awarded_credits"] += awarded
            entry = {
                "requirement_code": req["requirement_code"],
                "required_credits": req["required_credits"],
                "state": state,
                "awarded_credits": awarded,
                "recognition": rec,
            }
        totals[state] += 1
        progress.append(entry)

    return {"student_id": student_id, "progress": progress, "totals": totals}


@app.get("/students/{student_id}/vacancies")
def list_vacancies(student_id: str) -> dict[str, Any]:
    """空缺：尚没有任何已认或待复核占用的本校要求。"""
    with db.read_tx() as conn:
        rows = conn.execute(
            """
            SELECT q.* FROM requirements q
             WHERE q.student_id = ?
               AND NOT EXISTS (
                     SELECT 1 FROM recognitions r
                      WHERE r.student_id = q.student_id
                        AND r.requirement_code = q.requirement_code
                        AND r.status IN ('approved','pending'))
             ORDER BY q.requirement_code
            """,
            (student_id,),
        ).fetchall()
    return {
        "student_id": student_id,
        "vacancies": [
            {"requirement_code": r["requirement_code"],
             "required_credits": r["required_credits"]}
            for r in rows
        ],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

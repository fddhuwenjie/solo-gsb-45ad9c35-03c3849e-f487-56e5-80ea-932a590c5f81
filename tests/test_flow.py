"""端到端测试：真实 HTTP + SQLite。

直接运行：  PYTHONPATH=. python3 -m tests.test_flow
也可 pytest：pytest -q
覆盖：登记校验、批量原子性、重复占用、成绩更正转待复核、保留占用、
显式复核、复核仍不合格、撤销释放、空缺查询、并发申请/更正/复核、重启持久化。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time

import httpx
import uvicorn

# 在导入应用前指定独立测试数据库
_TMPDIR = tempfile.mkdtemp(prefix="credits-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")

from app.main import app  # noqa: E402

BASE = "http://127.0.0.1:8000"
_client: httpx.Client | None = None
_failures: list[str] = []


def check(name: str, cond: bool, extra: object = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + ("" if cond or not extra else f"  -> {extra}"))
    if not cond:
        _failures.append(name)


def req(method: str, path: str, **kwargs) -> httpx.Response:
    assert _client is not None
    r = _client.request(method, BASE + path, timeout=30, **kwargs)
    return r


def _start_server() -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            if httpx.get(BASE + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    return server


# --------------------------------------------------------------------------- #

def test_registration_validation() -> None:
    s = "stu-reg"
    r = req("PUT", f"/students/{s}/courses/C1", json={"credits": 4, "score": 88})
    check("登记外课 201", r.status_code == 201, r.text)
    r = req("PUT", f"/students/{s}/courses/C2", json={"credits": 2, "score": 95})
    check("登记第二门外课", r.status_code == 201, r.text)
    r = req("PUT", f"/students/{s}/courses/C1", json={"credits": 3, "score": 70})
    check("重复登记 409", r.status_code == 409, r.status_code)

    r = req("PUT", f"/students/{s}/courses/CX", json={"credits": 0, "score": 80})
    check("学分非正整数 422", r.status_code == 422, r.status_code)
    r = req("PUT", f"/students/{s}/courses/CY", json={"credits": 3, "score": 101})
    check("成绩>100 422", r.status_code == 422, r.status_code)
    r = req("PUT", f"/students/{s}/courses/CZ", json={"credits": 3, "score": -1})
    check("成绩<0 422", r.status_code == 422, r.status_code)

    r = req("PUT", f"/students/{s}/requirements/R1", json={"required_credits": 4})
    check("登记本校要求 201", r.status_code == 201, r.text)
    r = req("PUT", f"/students/{s}/requirements/R2", json={"required_credits": 2})
    check("登记要求二 201", r.status_code == 201, r.text)
    r = req("PUT", f"/students/{s}/requirements/R3", json={"required_credits": 6})
    check("登记要求三（学分不足场景）", r.status_code == 201, r.text)

    r = req("POST", f"/students/{s}/courses/C1/substitutions/R1",
            json={"min_score": 60})
    check("登记替代关系 201", r.status_code == 201, r.text)
    r = req("POST", f"/students/{s}/courses/C2/substitutions/R2",
            json={"min_score": 90})
    check("登记替代关系二 201", r.status_code == 201, r.text)
    r = req("POST", f"/students/{s}/courses/C1/substitutions/R1",
            json={"min_score": 50})
    check("重复替代关系 409", r.status_code == 409, r.status_code)
    r = req("POST", f"/students/{s}/courses/NOPE/substitutions/R1",
            json={"min_score": 50})
    check("替代关系引用不存在课程 404", r.status_code == 404, r.status_code)
    r = req("POST", f"/students/{s}/courses/C1/substitutions/NOPE",
            json={"min_score": 50})
    check("替代关系引用不存在要求 404", r.status_code == 404, r.status_code)


def test_batch_atomicity() -> None:
    s = "stu-batch"
    for code, cr, sc in [("C1", 4, 88), ("C2", 3, 91), ("C3", 4, 59)]:
        req("PUT", f"/students/{s}/courses/{code}", json={"credits": cr, "score": sc})
    for code, need in [("R1", 4), ("R2", 2), ("R3", 4)]:
        req("PUT", f"/students/{s}/requirements/{code}", json={"required_credits": need})
    req("POST", f"/students/{s}/courses/C1/substitutions/R1", json={"min_score": 60})
    req("POST", f"/students/{s}/courses/C2/substitutions/R2", json={"min_score": 60})
    req("POST", f"/students/{s}/courses/C2/substitutions/R3", json={"min_score": 60})
    req("POST", f"/students/{s}/courses/C3/substitutions/R3", json={"min_score": 60})

    # C3 成绩不达标 -> 整批失败，全部不写
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"},
        {"course_code": "C3", "requirement_code": "R3"},
    ]})
    check("整批含不合格项 -> 422", r.status_code == 422, (r.status_code, r.text))
    prog = req("GET", f"/students/{s}/progress").json()
    check("失败整批不写（仍全空缺）",
          all(e["state"] == "vacant" for e in prog["progress"]), prog)

    # 学分不足场景：课程学分 < 要求所需学分
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C2", "requirement_code": "R3"},
    ]})
    check("外课学分不足 -> 422", r.status_code == 422 and
          r.json()["detail"]["reason"] == "insufficient_credits", r.text)

    # 批内源课重复 / 要求重复
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"},
        {"course_code": "C1", "requirement_code": "R2"},
    ]})
    check("批内源课重复 -> 409", r.status_code == 409, r.text)
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"},
        {"course_code": "C2", "requirement_code": "R1"},
    ]})
    check("批内要求重复 -> 409", r.status_code == 409, r.text)

    # 合法整批确认
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"},
        {"course_code": "C2", "requirement_code": "R2"},
    ]})
    check("合法整批 201", r.status_code == 201, r.text)
    check("整批两条均 approved",
          all(x["status"] == "approved" for x in r.json()["recognitions"]), r.text)

    prog = req("GET", f"/students/{s}/progress").json()
    states = {e["requirement_code"]: e["state"] for e in prog["progress"]}
    check("进度 R1/R2 fulfilled、R3 vacant",
          states == {"R1": "approved", "R2": "approved", "R3": "vacant"}, states)
    check("已认按要求所需学分计入（4+2=6）",
          prog["totals"]["awarded_credits"] == 6, prog["totals"])

    # 现存占用：另一申请不能抢走已认或待复核名额（先备齐资格，确保走到占用检查）
    req("POST", f"/students/{s}/courses/C1/substitutions/R3", json={"min_score": 60})
    req("POST", f"/students/{s}/courses/C3/substitutions/R1", json={"min_score": 50})
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R3"},
    ]})
    check("已认源课再申请 -> 409 occupied", r.status_code == 409 and
          r.json()["detail"]["code"] == "occupied", r.text)
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C3", "requirement_code": "R1"},
    ]})
    check("已认要求被抢 -> 409 occupied", r.status_code == 409, r.text)


def test_correction_and_review() -> None:
    s = "stu-flow"
    req("PUT", f"/students/{s}/courses/C1", json={"credits": 4, "score": 88})
    req("PUT", f"/students/{s}/courses/C2", json={"credits": 3, "score": 91})
    req("PUT", f"/students/{s}/requirements/R1", json={"required_credits": 4})
    req("PUT", f"/students/{s}/requirements/R2", json={"required_credits": 2})
    req("POST", f"/students/{s}/courses/C1/substitutions/R1", json={"min_score": 60})
    req("POST", f"/students/{s}/courses/C2/substitutions/R2", json={"min_score": 90})
    req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"},
        {"course_code": "C2", "requirement_code": "R2"},
    ]})

    # 更正 C1 成绩至不合格（55 < 60）
    r = req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 55})
    check("成绩更正 200", r.status_code == 200, r.text)
    tr = r.json()["transitions"]
    check("已认 -> 待复核",
          len(tr) == 1 and tr[0]["from_status"] == "approved"
          and tr[0]["to_status"] == "pending", tr)

    prog = req("GET", f"/students/{s}/progress").json()
    states = {e["requirement_code"]: e["state"] for e in prog["progress"]}
    check("R1 pending 且停止计分（不计 4 学分）",
          states["R1"] == "pending" and prog["totals"]["awarded_credits"] == 2,
          (states, prog["totals"]))

    # 待复核仍保留两端占用：别的申请抢不走
    req("PUT", f"/students/{s}/courses/C9", json={"credits": 4, "score": 99})
    req("PUT", f"/students/{s}/requirements/R9", json={"required_credits": 4})
    req("POST", f"/students/{s}/courses/C9/substitutions/R1", json={"min_score": 60})
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C9", "requirement_code": "R1"},
    ]})
    check("待复核的要求不被抢走 409", r.status_code == 409, r.text)
    req("POST", f"/students/{s}/courses/C1/substitutions/R9", json={"min_score": 50})
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R9"},
    ]})
    check("待复核的源课不被抢走 409", r.status_code == 409, r.text)

    # 列表按状态过滤；空缺查询
    pend = req("GET", f"/students/{s}/recognitions?status=pending").json()
    check("pending 过滤仅含 R1",
          [x["requirement_code"] for x in pend["recognitions"]] == ["R1"], pend)
    appr = req("GET", f"/students/{s}/recognitions?status=approved").json()
    check("approved 过滤仅含 R2",
          [x["requirement_code"] for x in appr["recognitions"]] == ["R2"], appr)
    vac = req("GET", f"/students/{s}/vacancies").json()
    check("空缺仅含未占用的 R9",
          [x["requirement_code"] for x in vac["vacancies"]] == ["R9"], vac)

    rid = tr[0]["id"]

    # 显式复核：仍不合格 -> 422 维持 pending
    r = req("POST", f"/recognitions/{rid}/review", json={})
    check("复核仍不合格 422", r.status_code == 422 and
          r.json()["detail"]["status"] == "pending", r.text)

    # 成绩恢复后不会自动计分
    r = req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 90})
    check("成绩恢复 200", r.status_code == 200, r.text)
    prog = req("GET", f"/students/{s}/progress").json()
    states = {e["requirement_code"]: e["state"] for e in prog["progress"]}
    check("恢复后仍 pending（不自动计分）", states["R1"] == "pending", states)

    # 显式复核通过 -> 重新计分
    r = req("POST", f"/recognitions/{rid}/review", json={})
    check("复核合格 -> approved 200", r.status_code == 200 and
          r.json()["status"] == "approved", r.text)
    prog = req("GET", f"/students/{s}/progress").json()
    check("复核后恢复计分（6 学分）",
          prog["totals"]["awarded_credits"] == 6, prog["totals"])


def test_revoke_releases() -> None:
    s = "stu-revoke"
    req("PUT", f"/students/{s}/courses/C1", json={"credits": 4, "score": 50})
    req("PUT", f"/students/{s}/requirements/R1", json={"required_credits": 4})
    req("POST", f"/students/{s}/courses/C1/substitutions/R1", json={"min_score": 60})

    # 先达标认上，再更正到 pending
    req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 80})
    rec = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"}]}).json()["recognitions"][0]
    rid = rec["id"]
    req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 50})

    r = req("DELETE", f"/recognitions/{rid}")
    check("撤销待复核 204", r.status_code == 204, r.status_code)
    r = req("DELETE", f"/recognitions/{rid}")
    check("重复撤销 404", r.status_code == 404, r.status_code)

    vac = req("GET", f"/students/{s}/vacancies").json()
    check("撤销后要求空缺", [x["requirement_code"] for x in vac["vacancies"]] == ["R1"], vac)

    # 恢复达标后可以重新申请（占用已释放）
    req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 88})
    r = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"}]}).json()
    check("释放后可重新申请 approved",
          r["recognitions"][0]["status"] == "approved", r)


def test_concurrent_applications() -> None:
    s = "stu-conc"
    req("PUT", f"/students/{s}/courses/C1", json={"credits": 4, "score": 90})
    req("PUT", f"/students/{s}/requirements/R1", json={"required_credits": 4})
    req("POST", f"/students/{s}/courses/C1/substitutions/R1", json={"min_score": 60})

    results: list[int] = []
    barrier = threading.Barrier(16)

    def fire() -> None:
        barrier.wait()
        r = req("POST", f"/students/{s}/recognitions", json={"items": [
            {"course_code": "C1", "requirement_code": "R1"}]})
        results.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("并发 16 申请：恰好 1 个 201", results.count(201) == 1, results)
    check("并发 16 申请：其余均 409", results.count(409) == 15, results)

    recs = req("GET", f"/students/{s}/recognitions").json()["recognitions"]
    check("无重复活跃记录（仅 1 条）", len(recs) == 1, len(recs))


def test_concurrent_correction_and_review() -> None:
    s = "stu-conc2"
    req("PUT", f"/students/{s}/courses/C1", json={"credits": 4, "score": 90})
    req("PUT", f"/students/{s}/requirements/R1", json={"required_credits": 4})
    req("POST", f"/students/{s}/courses/C1/substitutions/R1", json={"min_score": 60})
    rid = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"}]}).json()["recognitions"][0]["id"]

    def correct_low() -> None:
        req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 50})

    def correct_high() -> None:
        req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 95})

    def review() -> None:
        req("POST", f"/recognitions/{rid}/review", json={})

    jobs = [correct_low, review, correct_high, review, correct_low, review,
            correct_high, review]
    threads = [threading.Thread(target=j) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    recs = req("GET", f"/students/{s}/recognitions").json()["recognitions"]
    check("交错更正/复核后仍仅 1 条活跃记录", len(recs) == 1, len(recs))
    rec = recs[0]
    # 最终成绩 95：若 approved 必然合格；若 pending 则不得计入进度
    prog = req("GET", f"/students/{s}/progress").json()
    entry = next(e for e in prog["progress"] if e["requirement_code"] == "R1")
    if rec["status"] == "approved":
        check("approved 记录必须实时合格", rec["qualified_now"] is True, rec)
        check("approved 时计入 4 学分", entry["awarded_credits"] == 4, entry)
    else:
        check("最终态为 pending（最后一次复核发生在恢复前，允许）",
              rec["status"] == "pending", rec["status"])
        check("pending 不得计分", entry["awarded_credits"] == 0, entry)
        # 显式复核一次必然恢复
        r = req("POST", f"/recognitions/{rid}/review", json={})
        check("再复核恢复 approved", r.status_code == 200 and
              r.json()["status"] == "approved", r.text)


def test_persistence_across_restart(db_path: str) -> None:
    s = "stu-persist"
    req("PUT", f"/students/{s}/courses/C1", json={"credits": 4, "score": 77})
    req("PUT", f"/students/{s}/requirements/R1", json={"required_credits": 4})
    req("POST", f"/students/{s}/courses/C1/substitutions/R1", json={"min_score": 60})
    rid = req("POST", f"/students/{s}/recognitions", json={"items": [
        {"course_code": "C1", "requirement_code": "R1"}]}).json()["recognitions"][0]["id"]
    req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 55})  # -> pending

    assert os.path.exists(db_path), "数据库文件应落盘"
    before = req("GET", f"/students/{s}/progress").json()
    check("重启前 R1 pending",
          next(e for e in before["progress"] if e["requirement_code"] == "R1")[
              "state"] == "pending", before)
    return rid


def test_after_restart(rid: int) -> None:
    s = "stu-persist"
    prog = req("GET", f"/students/{s}/progress").json()
    states = {e["requirement_code"]: e["state"] for e in prog["progress"]}
    check("重启后记录仍在且状态保持 pending", states.get("R1") == "pending", states)
    r = req("POST", f"/recognitions/{rid}/review", json={})
    check("重启后复核仍不合格(55<60) -> 422", r.status_code == 422, r.text)
    req("PATCH", f"/students/{s}/courses/C1/score", json={"score": 80})
    r = req("POST", f"/recognitions/{rid}/review", json={})
    check("重启后恢复成绩再复核 -> approved", r.status_code == 200 and
          r.json()["status"] == "approved", r.text)


def main() -> int:
    global _client
    server = _start_server()
    _client = httpx.Client()
    rid = None
    try:
        test_registration_validation()
        test_batch_atomicity()
        test_correction_and_review()
        test_revoke_releases()
        test_concurrent_applications()
        test_concurrent_correction_and_review()
        rid = test_persistence_across_restart(os.environ["DB_PATH"])
    finally:
        _client.close()
        server.should_exit = True
        time.sleep(0.5)

    # “重启”：用同一 DB 文件重新起一个全新服务实例
    server2 = _start_server()
    _client = httpx.Client()
    try:
        test_after_restart(rid)
    finally:
        _client.close()
        server2.should_exit = True
        time.sleep(0.3)

    print("\n" + ("全部通过 ✅" if not _failures else f"{len(_failures)} 项失败: {_failures}"))
    return 0 if not _failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

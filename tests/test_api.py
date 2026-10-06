import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with TestClient(app) as c:
        yield c


def seed(client, courses, requirements, rules):
    for cid, credits, score in courses:
        assert client.post(
            "/courses", json={"id": cid, "credits": credits, "score": score}
        ).status_code == 201
    for rid, credits in requirements:
        assert client.post(
            "/requirements", json={"id": rid, "required_credits": credits}
        ).status_code == 201
    for cid, rid, min_score in rules:
        assert client.post(
            "/substitutions",
            json={"course_id": cid, "requirement_id": rid, "min_score": min_score},
        ).status_code == 201


def base_seed(client):
    seed(
        client,
        courses=[("C1", 4, 90), ("C2", 6, 75), ("C3", 2, 95)],
        requirements=[("R1", 4), ("R2", 6)],
        rules=[("C1", "R1", 60), ("C2", "R2", 70), ("C3", "R1", 60)],
    )


# ------------------------------------------------------------ 登记校验


def test_register_validation(client):
    assert client.post(
        "/courses", json={"id": "X", "credits": 0, "score": 80}
    ).status_code == 422
    assert client.post(
        "/courses", json={"id": "X", "credits": 3, "score": 101}
    ).status_code == 422
    assert client.post(
        "/requirements", json={"id": "X", "required_credits": -1}
    ).status_code == 422


def test_register_duplicates_conflict(client):
    base_seed(client)
    assert client.post(
        "/courses", json={"id": "C1", "credits": 4, "score": 90}
    ).status_code == 409
    assert client.post(
        "/requirements", json={"id": "R1", "required_credits": 4}
    ).status_code == 409
    assert client.post(
        "/substitutions",
        json={"course_id": "C1", "requirement_id": "R1", "min_score": 60},
    ).status_code == 409


# ------------------------------------------------------------ 批量互认


def test_batch_confirm_and_progress(client):
    base_seed(client)
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C2", "requirement_id": "R2"},
        ]},
    )
    assert resp.status_code == 201
    assert all(r["status"] == "confirmed" for r in resp.json()["created"])

    progress = client.get("/progress").json()
    assert progress["earned_credits"] == 10
    assert progress["remaining_credits"] == 0
    by_req = {r["requirement_id"]: r for r in progress["requirements"]}
    assert by_req["R1"]["status"] == "confirmed"
    assert by_req["R2"]["status"] == "confirmed"


def test_batch_all_or_nothing(client):
    base_seed(client)
    # C3 学分 2 < R1 所需 4 -> 整批失败，C2/R2 也不写入
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C2", "requirement_id": "R2"},
            {"course_id": "C3", "requirement_id": "R1"},
        ]},
    )
    assert resp.status_code == 409
    assert client.get("/recognitions").json() == []
    assert client.get("/progress").json()["earned_credits"] == 0


def test_batch_rejects_score_below_min_and_missing_rule(client):
    seed(
        client,
        courses=[("L", 4, 50), ("M", 4, 90)],
        requirements=[("RQ", 4)],
        rules=[("L", "RQ", 60)],  # M 没有规则
    )
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "L", "requirement_id": "RQ"}]}
    )
    assert resp.status_code == 409
    assert "min_score" in resp.json()["detail"][0]
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "M", "requirement_id": "RQ"}]}
    )
    assert resp.status_code == 409
    assert "no substitution rule" in resp.json()["detail"][0]
    assert client.get("/recognitions").json() == []


def test_batch_rejects_duplicates_within_batch(client):
    base_seed(client)
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C1", "requirement_id": "R2"},
        ]},
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C3", "requirement_id": "R1"},
        ]},
    )
    assert resp.status_code == 409
    assert client.get("/recognitions").json() == []


def test_batch_rejects_conflict_with_existing(client):
    base_seed(client)
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    ).status_code == 201
    # 同一要求被其它课程抢占 -> 409；同一课程复用 -> 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R2"}]}
    )
    assert resp.status_code == 409
    assert len(client.get("/recognitions").json()) == 1


# ------------------------------------------------------------ 更正 -> 待复核 -> 复核/撤销


def test_correction_moves_to_pending_and_keeps_occupation(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    assert client.get("/progress").json()["earned_credits"] == 4

    # 成绩更正到不达标 -> 待复核、停止计分
    resp = client.put("/courses/C1", json={"score": 55})
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == [1]
    progress = client.get("/progress").json()
    assert progress["earned_credits"] == 0
    assert progress["requirements"][0]["status"] == "pending"
    assert client.get("/recognitions", params={"status": "pending"}).json()[0]["id"] == 1

    # 占用保留：其它申请无法抢走该要求或该课程
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R2"}]}
    )
    assert resp.status_code == 409


def test_review_required_to_recount(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    client.put("/courses/C1", json={"score": 55})

    # 复核仍不合格 -> 维持待复核
    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": False, "status": "pending"}
    assert client.get("/progress").json()["earned_credits"] == 0

    # 成绩恢复后不会自动计分，须显式复核
    client.put("/courses/C1", json={"score": 88})
    assert client.get("/recognitions", params={"status": "pending"}).json()[0]["id"] == 1
    assert client.get("/progress").json()["earned_credits"] == 0

    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": True, "status": "confirmed"}
    assert client.get("/progress").json()["earned_credits"] == 4

    # 已认记录不能再复核
    assert client.post("/recognitions/1/review").status_code == 409


def test_revoke_releases_occupation(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    client.put("/courses/C1", json={"score": 40})  # -> pending

    resp = client.delete("/recognitions/1")
    assert resp.status_code == 200
    assert resp.json()["released"] == {"course_id": "C1", "requirement_id": "R1"}
    assert client.get("/recognitions").json() == []
    progress = client.get("/progress").json()
    assert progress["requirements"][0]["status"] == "vacant"

    # 释放后其它课程可以申请该要求
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409  # C3 学分 2 < 4，业务规则拒绝
    seed(client, [("C4", 4, 80)], [], [("C4", "R1", 60)])
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "C4", "requirement_id": "R1"}]}
    ).status_code == 201


def test_correction_of_credits_also_triggers_pending(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    resp = client.put("/courses/C1", json={"credits": 3})  # 学分不再足额
    assert resp.json()["moved_to_pending"] == [1]
    assert client.get("/progress").json()["earned_credits"] == 0


def test_not_found_and_empty_batch(client):
    base_seed(client)
    assert client.put("/courses/NOPE", json={"score": 1}).status_code == 404
    assert client.post("/recognitions/99/review").status_code == 404
    assert client.delete("/recognitions/99").status_code == 404
    assert client.post("/recognitions", json={"items": []}).status_code == 422
    assert client.put("/courses/C1", json={}).status_code == 422


# ------------------------------------------------------------ 并发


def test_concurrent_batches_no_double_use(client):
    base_seed(client)

    def submit(i):
        with TestClient(app) as c:
            course = "C1" if i % 2 == 0 else "C3"
            return c.post(
                "/recognitions",
                json={"items": [{"course_id": course, "requirement_id": "R1"}]},
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(submit, range(16)))

    assert codes.count(201) == 1
    assert codes.count(409) == 15
    recs = client.get("/recognitions").json()
    assert len(recs) == 1
    assert client.get("/progress").json()["earned_credits"] == 4


def test_concurrent_correction_and_submit_consistent(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )

    def correct(i):
        with TestClient(app) as c:
            return c.put("/courses/C1", json={"score": 50 if i % 2 else 95}).status_code

    def submit(_):
        with TestClient(app) as c:
            return c.post(
                "/recognitions",
                json={"items": [{"course_id": "C3", "requirement_id": "R1"}]},
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda i: correct(i) if i % 2 else submit(i), range(20)))

    assert all(code in (200, 409) for code in results)
    # 无论交错顺序如何，R1 始终只有一条占用记录
    recs = client.get("/recognitions").json()
    assert len([r for r in recs if r["requirement_id"] == "R1"]) == 1


# ------------------------------------------------------------ 持久化


def test_records_survive_restart(client, tmp_path, monkeypatch):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    # 模拟重启：重新初始化（幂等）并新建客户端，同一数据库文件
    db.init_db()
    with TestClient(app) as c2:
        assert len(c2.get("/recognitions").json()) == 1
        assert c2.get("/progress").json()["earned_credits"] == 4

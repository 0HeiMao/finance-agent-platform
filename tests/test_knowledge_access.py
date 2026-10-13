from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.models import User
from backend.rag.pgvector_store import DocumentChunk

pytestmark = pytest.mark.anyio


@pytest.fixture
def knowledge_environment(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.api_rag.get_embedding_service", lambda: SimpleNamespace(
        model_name="access-test", encode=lambda texts: [[1.0] + [0.0] * 383 for _ in texts]))
    monkeypatch.setattr("backend.api_rag.get_settings", lambda: SimpleNamespace(rag_upload_dir=str(tmp_path)))
    return tmp_path


@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize("admin,departments,target,public,status", [
    (False, [], "A", False, 403),
    (False, ["A"], "A", False, 403),
    (False, [], None, True, 403),
    (True, ["A"], "B", False, 403),
    (True, ["A"], None, True, 403),
    (True, [], None, False, 422),
    (True, [], "A", True, 422),
    (True, ["A"], " A ", False, 200),
    (True, [], None, True, 200),
])
async def test_knowledge_write_policy(anon_client, auth_headers, db_session, credentials,
                                      knowledge_environment, upload, admin, departments, target, public, status):
    user = db_session.scalar(select(User).where(User.username == credentials["username"]))
    user.is_admin = admin
    user.departments = departments
    user.department = None
    db_session.commit()
    if upload:
        data = {"publish_public": str(public).lower()}
        if target is not None:
            data["department"] = target
        result = await anon_client.post("/api/v1/rag/documents/upload", headers=auth_headers,
                                        data=data, files={"file": ("policy.txt", b"policy", "text/plain")})
    else:
        result = await anon_client.post("/api/v1/rag/documents", headers=auth_headers,
                                        json={"source": "policy.txt", "content": "policy",
                                              "department": target, "publish_public": public})
    assert result.status_code == status, result.text
    rows = list(db_session.scalars(select(DocumentChunk).where(DocumentChunk.source == "policy.txt")))
    if status == 200:
        assert len(rows) == 1
        assert rows[0].metadata_json["department"] == ((target or "").strip() or None)
    else:
        assert rows == []
        assert list(knowledge_environment.iterdir()) == []


async def test_scoped_admin_cannot_list_or_delete_other_departments(anon_client, auth_headers, db_session, credentials):
    user = db_session.scalar(select(User).where(User.username == credentials["username"]))
    user.departments = ["A"]
    for department in ("A", "B", None):
        db_session.add(DocumentChunk(source="shared-name.txt", content=f"private-{department}",
                                      embedding=[1.0] + [0.0] * 383,
                                      metadata_json={"department": department}))
    db_session.commit()
    listing = await anon_client.get("/api/v1/admin/knowledge?source=shared-name.txt", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["matched_chunks"] == 1
    assert listing.json()["data"][0]["preview"] == "private-A"
    deleted = await anon_client.delete("/api/v1/admin/knowledge?source=shared-name.txt", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted_chunks"] == 1
    remaining = list(db_session.scalars(select(DocumentChunk).where(DocumentChunk.source == "shared-name.txt")))
    assert {row.metadata_json["department"] for row in remaining} == {"B", None}
    denied = await anon_client.delete("/api/v1/admin/knowledge?source=shared-name.txt", headers=auth_headers)
    assert denied.status_code == 404

import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "services" / "content_service" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from content_service.knowledge import LibraryKnowledgeProvider
from content_service.models import Base, ResourceChunk, ResourceEntry, ResourceVersion
from content_service.providers import KnowledgeQuery


def _seed(session: Session, department: str) -> None:
    entry = ResourceEntry(
        owner_id=f"owner-{department}",
        resource_kind="document",
        title=department,
        category="product",
        data_level="restricted",
        department_id=department,
        allowed_roles_json='["editor"]',
    )
    session.add(entry)
    session.flush()
    version = ResourceVersion(
        entry_id=entry.id,
        version=1,
        text_content=f"{department} fact",
        created_by=f"owner-{department}",
    )
    session.add(version)
    session.flush()
    entry.current_version_id = version.id
    session.add(
        ResourceChunk(version_id=version.id, sequence=0, text=f"{department} fact", char_count=12)
    )
    session.commit()


def test_department_and_role_scope_isolated_in_shared_knowledge_base():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed(session, "department-a")
        _seed(session, "department-b")
        provider = LibraryKnowledgeProvider(session)
        common = {
            "text": "fact",
            "requester_id": "user-a",
            "task_id": "t",
            "categories": (),
            "allowed_data_levels": frozenset({"restricted"}),
            "limit": 10,
        }
        a = provider.search(
            KnowledgeQuery(
                **common, requester_department="department-a", requester_roles=frozenset({"editor"})
            )
        )
        assert [item.title for item in a] == ["department-a"]
        b = provider.search(
            KnowledgeQuery(
                **common, requester_department="department-b", requester_roles=frozenset({"editor"})
            )
        )
        assert [item.title for item in b] == ["department-b"]
        denied = provider.search(
            KnowledgeQuery(
                **common, requester_department="department-a", requester_roles=frozenset({"viewer"})
            )
        )
        assert denied == []

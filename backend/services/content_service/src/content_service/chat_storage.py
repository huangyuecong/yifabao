"""Private, process-safe chat persistence independent of the content database dialect."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier() -> str:
    return uuid4().hex


class ChatStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "private" / "chat"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "chat.sqlite3"

    @contextmanager
    def connect(self, *, write=False):
        connection = None
        try:
            connection = sqlite3.connect(self.database, timeout=15)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                  model_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  operation_id TEXT, operation_until TEXT);
                CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(owner, updated_at);
                CREATE TABLE IF NOT EXISTS messages (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                  role TEXT NOT NULL, content TEXT NOT NULL, model_id TEXT NOT NULL,
                  created_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                  attachments_json TEXT NOT NULL DEFAULT '[]',
                  request_json TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, sequence);
                CREATE TABLE IF NOT EXISTS attachments (
                  id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_id TEXT,
                  name TEXT NOT NULL, kind TEXT NOT NULL, mime_type TEXT NOT NULL,
                  size INTEGER NOT NULL, created_at TEXT NOT NULL, metadata_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attachments_owner ON attachments(owner);
                CREATE TABLE IF NOT EXISTS artifacts (
                  id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                  message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                  name TEXT NOT NULL, kind TEXT NOT NULL, mime_type TEXT NOT NULL,
                  size INTEGER NOT NULL, created_at TEXT NOT NULL, filename TEXT NOT NULL,
                  metadata_json TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS artifacts_message ON artifacts(message_id);
            """)
            if "request_json" not in {
                row[1] for row in connection.execute("PRAGMA table_info(messages)")
            }:
                # Upgrade only private chat storage. Existing Dameng tables are untouched.
                connection.execute("BEGIN IMMEDIATE")
                if "request_json" not in {
                    row[1] for row in connection.execute("PRAGMA table_info(messages)")
                }:
                    connection.execute(
                        "ALTER TABLE messages ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'"
                    )
                connection.commit()
            if os.name != "nt":
                os.chmod(self.database, 0o600)
                for suffix in ("-wal", "-shm"):
                    path = Path(str(self.database) + suffix)
                    if path.exists():
                        os.chmod(path, 0o600)
            if write:
                connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                if write:
                    connection.commit()
            except Exception:
                connection.rollback()
                raise
        except sqlite3.Error:
            raise HTTPException(503, "AI 对话存储暂不可用，请稍后重试") from None
        finally:
            if connection is not None:
                connection.close()

    def owned(self, connection, conversation_id: str, owner: str):
        row = connection.execute(
            "SELECT * FROM conversations WHERE id=? AND owner=?", (conversation_id, owner)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "对话不存在")
        return row

    def recover(self, connection, conversation):
        if conversation["operation_id"] and conversation["operation_until"] < now():
            connection.execute(
                "UPDATE messages SET status='failed', error=? WHERE conversation_id=? AND status='pending'",
                ("上次请求未完成，请重试", conversation["id"]),
            )
            connection.execute(
                "UPDATE conversations SET operation_id=NULL, operation_until=NULL WHERE id=?",
                (conversation["id"],),
            )
            return self.owned(connection, conversation["id"], conversation["owner"])
        return conversation

    def attachment(self, connection, attachment_id: str, owner: str):
        row = connection.execute(
            "SELECT * FROM attachments WHERE id=? AND owner=?", (attachment_id, owner)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "附件不存在或已被删除")
        return row

    def attachment_document(self, row):
        meta = json.loads(row["metadata_json"])
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "mime_type": row["mime_type"],
            "size": row["size"],
            "status": "ready",
            "url": f"/api/v1/chat/attachments/{row['id']}/content",
            **{
                key: meta[key]
                for key in ("text_preview", "frame_count", "duration_seconds", "notice")
                if key in meta
            },
        }

    def artifact(self, connection, artifact_id, owner, conversation_id=None):
        row = connection.execute(
            "SELECT * FROM artifacts WHERE id=? AND owner=?", (artifact_id, owner)
        ).fetchone()
        if row is None or (
            conversation_id is not None and row["conversation_id"] != conversation_id
        ):
            raise HTTPException(404, "生成文件不存在或不属于当前对话")
        return row

    def artifact_document(self, row):
        result = {
            **{key: row[key] for key in ("id", "name", "kind", "mime_type", "size", "created_at")},
            "status": "ready",
            "url": f"/api/v1/chat/artifacts/{row['id']}/content",
        }
        if row["kind"] in {"doc", "docx", "pdf"}:
            result["text_preview"] = json.loads(row["metadata_json"]).get("text", "")
        return result

    def document(self, connection, conversation):
        messages = []
        for row in connection.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY sequence",
            (conversation["id"],),
        ):
            item = {
                key: row[key]
                for key in ("id", "role", "content", "model_id", "created_at", "status", "error")
            }
            item["attachments"] = [
                self.attachment_document(self.attachment(connection, aid, conversation["owner"]))
                for aid in json.loads(row["attachments_json"])
            ]
            item["artifacts"] = [
                self.artifact_document(artifact)
                for artifact in connection.execute(
                    "SELECT * FROM artifacts WHERE message_id=? ORDER BY created_at,id",
                    (row["id"],),
                )
            ]
            request = json.loads(row["request_json"])
            item["output"] = request.get("output", "auto")
            if request.get("routing"):
                item["routing"] = request["routing"]
            if row["role"] == "assistant" and request.get("agent"):
                item["agent"] = request["agent"]
            messages.append(item)
        return {
            **{
                key: conversation[key]
                for key in ("id", "title", "model_id", "created_at", "updated_at")
            },
            "messages": messages,
            "message_count": len(messages),
            "busy": bool(conversation["operation_id"]),
        }

    def history(self, connection, conversation, *, exclude_id=None):
        result = []
        for row in connection.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY sequence",
            (conversation["id"],),
        ):
            retained_reply = (
                row["role"] == "assistant" and row["status"] == "failed" and bool(row["content"])
            )
            if row["id"] == exclude_id or (row["status"] != "completed" and not retained_reply):
                continue
            result.append(
                {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "attachments": [
                        dict(self.attachment(connection, aid, conversation["owner"]))
                        for aid in json.loads(row["attachments_json"])
                    ],
                    "request": json.loads(row["request_json"]),
                    "artifacts": [
                        dict(artifact)
                        for artifact in connection.execute(
                            "SELECT * FROM artifacts WHERE message_id=? AND owner=? ORDER BY created_at,id",
                            (row["id"], conversation["owner"]),
                        )
                    ],
                }
            )
        return result

    def begin(
        self,
        conversation_id,
        owner,
        model_id,
        content,
        attachment_ids,
        *,
        retry=False,
        lease_seconds=300,
        request=None,
    ):
        with self.connect(write=True) as connection:
            conversation = self.recover(connection, self.owned(connection, conversation_id, owner))
            if conversation["operation_id"]:
                raise HTTPException(409, "当前对话正在生成回复，请稍候")
            if retry:
                last = connection.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY sequence DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if (
                    last is None
                    or last["role"] != "assistant"
                    or last["status"] not in {"failed", "completed"}
                ):
                    raise HTTPException(409, "暂无可重试的回复")
                assistant_id = last["id"]
                connection.execute(
                    "UPDATE messages SET status='pending',error=NULL,model_id=? WHERE id=?",
                    (model_id, assistant_id),
                )
            else:
                request = request or {}
                if request.get("reference_artifact_id"):
                    self.artifact(
                        connection, request["reference_artifact_id"], owner, conversation_id
                    )
                for aid in attachment_ids:
                    attachment = self.attachment(connection, aid, owner)
                    if attachment["conversation_id"] not in (None, conversation_id):
                        raise HTTPException(422, "该附件已用于其他对话，请重新上传")
                count = connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)
                ).fetchone()[0]
                if count >= 200:
                    raise HTTPException(422, "当前对话已达到 100 轮上限，请开启新对话")
                created = now()
                connection.execute(
                    "INSERT INTO messages(id,conversation_id,role,content,model_id,created_at,status,attachments_json,request_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identifier(),
                        conversation_id,
                        "user",
                        content,
                        model_id,
                        created,
                        "completed",
                        json.dumps(attachment_ids),
                        json.dumps(request),
                    ),
                )
                for aid in attachment_ids:
                    connection.execute(
                        "UPDATE attachments SET conversation_id=? WHERE id=?",
                        (conversation_id, aid),
                    )
                assistant_id = identifier()
                connection.execute(
                    "INSERT INTO messages(id,conversation_id,role,content,model_id,created_at,status) VALUES(?,?,?,?,?,?,?)",
                    (assistant_id, conversation_id, "assistant", "", model_id, created, "pending"),
                )
                if count == 0:
                    title = (content.strip() or "附件对话")[:40]
                    connection.execute(
                        "UPDATE conversations SET title=? WHERE id=? AND title='新对话'",
                        (title, conversation_id),
                    )
            operation_id = identifier()
            connection.execute(
                "UPDATE conversations SET model_id=?,updated_at=?,operation_id=?,operation_until=? WHERE id=?",
                (
                    model_id,
                    now(),
                    operation_id,
                    (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(),
                    conversation_id,
                ),
            )
            history = self.history(connection, conversation, exclude_id=assistant_id)
        return operation_id, assistant_id, history

    def finish(
        self,
        conversation_id,
        operation_id,
        assistant_id,
        *,
        content=None,
        error=None,
        artifacts=(),
        trace=None,
    ):
        with self.connect(write=True) as connection:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE id=? AND operation_id=?",
                (conversation_id, operation_id),
            ).fetchone()
            if conversation is None:
                return False
            if error:
                connection.execute(
                    "UPDATE messages SET status='failed',error=? WHERE id=?", (error, assistant_id)
                )
            else:
                for item in artifacts:
                    connection.execute(
                        "INSERT INTO artifacts(id,owner,conversation_id,message_id,name,kind,mime_type,size,created_at,filename,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            item["id"],
                            conversation["owner"],
                            conversation_id,
                            assistant_id,
                            item["name"],
                            item["kind"],
                            item["mime_type"],
                            item["size"],
                            now(),
                            item["filename"],
                            json.dumps(item.get("metadata", {}), ensure_ascii=False),
                        ),
                    )
                connection.execute(
                    "UPDATE messages SET status='completed',content=?,error=NULL,request_json=? WHERE id=?",
                    (
                        content,
                        json.dumps({"agent": trace} if trace else {}, ensure_ascii=False),
                        assistant_id,
                    ),
                )
            connection.execute(
                "UPDATE conversations SET operation_id=NULL,operation_until=NULL,updated_at=? WHERE id=?",
                (now(), conversation_id),
            )
            return True

    def renew(self, conversation_id, operation_id, *, lease_seconds=120):
        with self.connect(write=True) as connection:
            return bool(
                connection.execute(
                    "UPDATE conversations SET operation_until=? WHERE id=? AND operation_id=?",
                    (
                        (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(),
                        conversation_id,
                        operation_id,
                    ),
                ).rowcount
            )

"""Project discussion data migration, without constructing execution services."""
from agilecode.adapters.store import decode
from agilecode.core.types import uid


def discussion_title(text: str) -> str:
    clean = " ".join(text.strip().split())
    return "New discussion" if not clean else clean if len(clean) <= 64 else clean[:61].rstrip() + "..."


async def promote_legacy_discussion(store):
    """Idempotent startup promotion under the database write reservation."""
    def promote(c):
        existing = c.execute("SELECT * FROM discussions WHERE session_id='project-discussion'").fetchone()
        if existing:
            return dict(existing)
        rows = c.execute("SELECT * FROM messages WHERE session_id='project-discussion' ORDER BY seq").fetchall()
        if not rows:
            return None
        user = next((decode(row["body"]).get("content", "") for row in rows
                     if decode(row["body"]).get("role") == "user"), "")
        data = {"id": uid(), "title": discussion_title(user) if user else "Project discussion",
                "session_id": "project-discussion", "archived": 0,
                "created": rows[0]["created"], "updated": rows[-1]["created"]}
        c.execute("INSERT INTO discussions(id,title,session_id,archived,created,updated) VALUES(?,?,?,?,?,?)", tuple(data.values()))
        return data
    return await store.transaction(promote)

from __future__ import annotations

from dataclasses import dataclass

from .index import KnowledgeBase

INSUFFICIENT_EVIDENCE = "未在已审核知识库中找到足够依据。"


@dataclass(frozen=True)
class Message:
    user_id: str
    chat_id: str
    chat_type: str
    text: str


class ReadOnlyQa:
    def __init__(self, kb: KnowledgeBase, allowed_users: set[str], allowed_chats: set[str]):
        if not allowed_users or not allowed_chats:
            raise ValueError("non-empty user and chat allowlists are required")
        self._kb = kb
        self._users = frozenset(allowed_users)
        self._chats = frozenset(allowed_chats)
        self._sessions: dict[tuple[str, str, str], list[str]] = {}

    def session_key(self, message: Message) -> tuple[str, str, str]:
        if message.chat_type not in {"direct", "group"}:
            raise ValueError("unsupported chat type")
        return message.chat_type, message.chat_id, message.user_id

    def handle(self, message: Message) -> dict:
        if message.user_id not in self._users or message.chat_id not in self._chats:
            return {"status": "denied", "answer": "无权访问该知识库。"}
        key = self.session_key(message)
        self._sessions.setdefault(key, []).append(message.text)
        hits = self._kb.dek_kb_search(message.text, limit=3)
        if not hits:
            return {"status": "insufficient_evidence", "answer": INSUFFICIENT_EVIDENCE, "citations": []}
        citations = [
            {"path": hit["path"], "official_urls": hit["official_urls"]}
            for hit in hits
        ]
        return {"status": "evidence_found", "hits": hits, "citations": citations}

    def session_messages(self, message: Message) -> tuple[str, ...]:
        return tuple(self._sessions.get(self.session_key(message), ()))

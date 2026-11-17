from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class Message:
    index: int
    role: str            # "user" | "assistant"
    content: str = ""
    thinking: Optional[str] = None
    hint: Optional[str] = None

    def to_dict(self):
        return asdict(self)


@dataclass
class Conversation:
    title: Optional[str] = None
    url: Optional[str] = None
    messages: List[Message] = field(default_factory=list)

    @classmethod
    def from_json(cls, data):
        msgs = []
        for m in data.get("messages", []) or []:
            msgs.append(Message(
                index=int(m.get("index", len(msgs))),
                role=str(m.get("role", "")),
                content=str(m.get("content") or ""),
                thinking=m.get("thinking") or None,
                hint=m.get("hint") or None,
            ))
        return cls(title=data.get("title"), url=data.get("url"), messages=msgs)

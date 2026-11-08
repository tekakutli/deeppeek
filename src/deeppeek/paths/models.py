from dataclasses import dataclass, field


@dataclass
class PathMatch:
    path: str
    kind: str = "file"        # "file" | "dir" | "module"
    tier: int = 4
    source: str = "unknown"
    location: str = ""
    resolved: bool = field(default=False, compare=False)

    def normalized(self) -> str:
        s = self.path.replace("\\", "/")
        while s.startswith("./"):
            s = s[2:]
        return s.rstrip("/") if self.kind == "dir" else s

    def to_dict(self) -> dict:
        d = {
            "path": self.path,
            "kind": self.kind,
            "tier": self.tier,
            "source": self.source,
            "location": self.location,
        }
        if self.resolved:
            d["resolved"] = True
        return d
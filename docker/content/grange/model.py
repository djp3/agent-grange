"""Data model (requirements section 4) and the validation every incoming body
passes before any field is used (R9.10).

A wrong type or malformed JSON is a 400; exceeding a size or count limit is a
413. Unknown fields are dropped, never rejected and never relayed, so nothing
but `owner`, `name`, and `state` can leave a machine for an agent (R4.4, R9.11).
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple, Optional

from .config import (MAX_AGENTS, MAX_BODIES_PER_MESSAGE, MAX_DIGEST_ENTRIES,
                     MAX_STRING_BYTES, PROTO)

INT_LIMIT = 1 << 63


class ValidationError(ValueError):
    """Carries the HTTP status the API answers with: 400 or 413."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def bad(message: str) -> ValidationError:
    return ValidationError(400, message)


def too_big(message: str) -> ValidationError:
    return ValidationError(413, message)


# ------------------------------------------------------------------------- JSON

def _reject_constant(name: str):
    raise bad(f"{name} is not valid JSON")


def decode_json(raw: bytes):
    """Strict JSON: UTF-8, no NaN or Infinity."""
    try:
        text = raw.decode("utf-8")
        return json.loads(text, parse_constant=_reject_constant)
    except ValidationError:
        raise
    except (UnicodeDecodeError, ValueError) as e:
        raise bad(f"body is not valid JSON: {e}") from None


def encode_json(obj) -> bytes:
    """Compact UTF-8 JSON, as sent on the wire."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def json_size(obj) -> int:
    return len(encode_json(obj))


# ----------------------------------------------------------------- field checks

def _string(value, what: str) -> str:
    if not isinstance(value, str):
        raise bad(f"{what} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:              # a lone surrogate, which JSON can spell as \ud800
        raise bad(f"{what} is not valid Unicode") from None
    if size > MAX_STRING_BYTES:
        raise too_big(f"{what} is longer than {MAX_STRING_BYTES} bytes")
    return value


def _id(value, what: str) -> str:
    value = _string(value, what)
    if not value:
        raise bad(f"{what} must not be empty")
    return value


def _int(value, what: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise bad(f"{what} must be an integer")
    if not minimum <= value < INT_LIMIT:
        raise bad(f"{what} is out of range")
    return value


def _bool(value, what: str) -> bool:
    if not isinstance(value, bool):
        raise bad(f"{what} must be true or false")
    return value


def _age(value, what: str) -> Optional[float]:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise bad(f"{what} must be a number or null")
    if not 0 <= value < 1e12:
        raise bad(f"{what} is out of range")
    return float(value)


def _object(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise bad(f"{what} must be a JSON object")
    return value


def _list(value, what: str) -> list:
    if not isinstance(value, list):
        raise bad(f"{what} must be a list")
    return value


# ------------------------------------------------------------------------ agent

@dataclass(frozen=True)
class Agent:
    """One Claude Code session as farm sees it (4.1). `state` is opaque (R4.1)."""
    owner: str
    name: str
    state: str

    def to_json(self) -> dict:
        return {"owner": self.owner, "name": self.name, "state": self.state}


def parse_agent(value, what: str = "agent") -> Agent:
    obj = _object(value, what)
    fields = {}
    for key in ("owner", "name", "state"):
        if key not in obj:
            raise bad(f"{what} lacks {key!r}")
        fields[key] = _string(obj[key], f"{what}.{key}")
    return Agent(**fields)              # any other field is dropped here (R4.4)


def parse_agents(value, what: str = "agents") -> tuple:
    items = _list(value, what)
    if len(items) > MAX_AGENTS:
        raise too_big(f"{what} has {len(items)} entries; at most {MAX_AGENTS}")
    return tuple(parse_agent(item, f"{what}[{i}]") for i, item in enumerate(items))


def parse_farm_push(value) -> tuple:
    """PUT /v1/farm body: {"agents": [...]} (5.1)."""
    obj = _object(value, "body")
    if "agents" not in obj:
        raise bad("body lacks 'agents'")
    return parse_agents(obj["agents"])


def same_agents(a, b) -> bool:
    """Same entries the same number of times, in any order (R5.3)."""
    return Counter(a) == Counter(b)


# ----------------------------------------------------------------------- record

@dataclass(frozen=True)
class Record:
    """One grange's body plus the versions it travels with (4.2).

    On the wire `counter` and `farm_age_s` are the sender's stored liveness for
    that grange, which may be later than what the origin put in (4.2).
    """
    grange: str
    incarnation: int
    counter: int
    rev: int
    farm_age_s: Optional[float]
    farm_stale: bool
    leaving: bool
    agents: tuple
    proto: int = PROTO
    group: str = "default"

    def content_version(self) -> tuple:
        return (self.incarnation, self.rev)

    def liveness_version(self) -> tuple:
        return (self.incarnation, self.counter)

    def to_json(self) -> dict:
        return {
            "proto": self.proto,
            "group": self.group,
            "grange": self.grange,
            "incarnation": self.incarnation,
            "counter": self.counter,
            "rev": self.rev,
            "farm_age_s": None if self.farm_age_s is None else round(self.farm_age_s, 1),
            "farm_stale": self.farm_stale,
            "leaving": self.leaving,
            "agents": [a.to_json() for a in self.agents],
        }


def parse_record(value, what: str = "record") -> Record:
    obj = _object(value, what)
    required = ("proto", "group", "grange", "incarnation", "counter", "rev",
                "farm_age_s", "farm_stale", "leaving", "agents")
    for key in required:
        if key not in obj:
            raise bad(f"{what} lacks {key!r}")
    return Record(
        proto=_int(obj["proto"], f"{what}.proto"),
        group=_string(obj["group"], f"{what}.group"),
        grange=_id(obj["grange"], f"{what}.grange"),
        incarnation=_int(obj["incarnation"], f"{what}.incarnation"),
        counter=_int(obj["counter"], f"{what}.counter", minimum=1),
        rev=_int(obj["rev"], f"{what}.rev", minimum=1),
        farm_age_s=_age(obj["farm_age_s"], f"{what}.farm_age_s"),
        farm_stale=_bool(obj["farm_stale"], f"{what}.farm_stale"),
        leaving=_bool(obj["leaving"], f"{what}.leaving"),
        agents=parse_agents(obj["agents"], f"{what}.agents"),
    )


# ----------------------------------------------------------------------- digest

class DigestEntry(NamedTuple):
    """[incarnation, counter, rev, farm_age_s] for one grange id (5.3)."""
    incarnation: int
    counter: int
    rev: int
    farm_age_s: Optional[float]

    def to_json(self) -> list:
        age = None if self.farm_age_s is None else round(self.farm_age_s, 1)
        return [self.incarnation, self.counter, self.rev, age]

    def content_version(self) -> tuple:
        return (self.incarnation, self.rev)

    def liveness_version(self) -> tuple:
        return (self.incarnation, self.counter)


def parse_digest(value, what: str = "digest") -> dict:
    obj = _object(value, what)
    if len(obj) > MAX_DIGEST_ENTRIES:
        raise too_big(f"{what} has {len(obj)} entries; at most {MAX_DIGEST_ENTRIES}")
    out = {}
    for gid, entry in obj.items():
        gid = _id(gid, f"{what} key")
        items = _list(entry, f"{what}[{gid!r}]")
        if len(items) != 4:
            raise bad(f"{what}[{gid!r}] must be [incarnation, counter, rev, farm_age_s]")
        out[gid] = DigestEntry(
            _int(items[0], f"{what}[{gid!r}].incarnation"),
            _int(items[1], f"{what}[{gid!r}].counter", minimum=1),
            _int(items[2], f"{what}[{gid!r}].rev", minimum=1),
            _age(items[3], f"{what}[{gid!r}].farm_age_s"),
        )
    return out


# -------------------------------------------------------------- gossip message

@dataclass
class GossipMessage:
    """POST /v1/gossip body (5.3). A push has records and no digest; a repair
    request has a digest and no records; a repair response has both."""
    proto: int
    group: str
    sender: str
    digest: Optional[dict]
    records: list

    @property
    def is_repair(self) -> bool:
        return self.digest is not None

    def to_json(self) -> dict:
        body = {"proto": self.proto, "group": self.group, "from": self.sender}
        if self.digest is not None:
            body["digest"] = {gid: e.to_json() for gid, e in self.digest.items()}
        body["records"] = [r.to_json() for r in self.records]
        return body


def gossip_header(value):
    """(proto, group) as sent, before anything else is checked, so a mismatch
    can be answered with 409 (R5.11) even when the rest would not parse."""
    obj = _object(value, "body")
    return obj.get("proto"), obj.get("group")


def parse_gossip(value) -> GossipMessage:
    obj = _object(value, "body")
    for key in ("proto", "group", "from"):
        if key not in obj:
            raise bad(f"body lacks {key!r}")
    records = _list(obj.get("records", []), "records")
    if len(records) > MAX_BODIES_PER_MESSAGE:
        raise too_big(f"records has {len(records)} bodies; at most {MAX_BODIES_PER_MESSAGE}")
    digest = obj.get("digest")
    return GossipMessage(
        proto=_int(obj["proto"], "proto"),
        group=_string(obj["group"], "group"),
        sender=_id(obj["from"], "from"),
        digest=None if digest is None else parse_digest(digest),
        records=[parse_record(r, f"records[{i}]") for i, r in enumerate(records)],
    )

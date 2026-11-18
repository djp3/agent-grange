"""Shared test helpers: Stores on a fake clock, wired by direct calls."""
from __future__ import annotations

from grange.clock import FakeClock
from grange.config import Config
from grange.model import Agent
from grange.state import Store


class Logs(list):
    def __call__(self, msg: str) -> None:
        self.append(msg)


def make_store(gid: str, clock: FakeClock | None = None, **cfg) -> Store:
    clock = clock or FakeClock()
    store = Store(Config(grange_id=gid, **cfg), clock, log=Logs())
    store.events = []
    store.subscribe(store.events.append)
    return store


def agent(name: str, state: str = "busy", owner: str = "djp3") -> Agent:
    return Agent(owner=owner, name=name, state=state)


def repair(requester: Store, responder: Store, req_addr: str = None, resp_addr: str = None) -> None:
    """One repair exchange, R5.10: request, response, follow-up push."""
    req_addr = req_addr or requester.grange_id
    resp_addr = resp_addr or responder.grange_id
    request = requester.repair_request()
    response = responder.answer_repair(request, source=req_addr)
    followup = requester.take_repair_response(response, source=resp_addr)
    if followup:
        responder.take_push(requester.push_message(followup), source=req_addr)


def push(sender: Store, receiver: Store, record=None, addr: str = None) -> list:
    """One push of the sender's own body (or a given record)."""
    record = record if record is not None else sender.own_body()
    return receiver.take_push(sender.push_message([record]), source=addr or sender.grange_id)


def board(store: Store) -> dict:
    """grange id -> entry of the picture."""
    return {g["grange"]: g for g in store.picture()["granges"]}

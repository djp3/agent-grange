"""Model and validation (4.1, 4.2, 5.3, R4.3, R4.4, R5.17, R9.10)."""
import unittest

from grange.model import (Agent, DigestEntry, Record, ValidationError, decode_json, encode_json,
                          parse_digest, parse_farm_push, parse_gossip, parse_record, same_agents)


def record_json(**overrides):
    rec = {"proto": 1, "group": "default", "grange": "alice", "incarnation": 1763300000000,
           "counter": 77, "rev": 3, "farm_age_s": 20.5, "farm_stale": False, "leaving": False,
           "agents": [{"owner": "alice", "name": "proj-12-pr-7 docs", "state": "idle"}]}
    rec.update(overrides)
    return rec


class JsonTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(decode_json(encode_json({"a": [1, None, "é"]})), {"a": [1, None, "é"]})

    def test_invalid_json_is_400(self):
        for raw in (b"{", b"\xff", b"NaN", b'{"a": Infinity}', b"[1,]"):
            with self.assertRaises(ValidationError) as cm:
                decode_json(raw)
            self.assertEqual(cm.exception.status, 400, raw)


class FarmPushTest(unittest.TestCase):
    def test_valid(self):
        agents = parse_farm_push({"agents": [{"owner": "djp3", "name": "n", "state": "busy"}]})
        self.assertEqual(agents, (Agent("djp3", "n", "busy"),))

    def test_empty_list_is_valid(self):
        self.assertEqual(parse_farm_push({"agents": []}), ())

    def test_unseen_state_is_accepted(self):
        self.assertEqual(parse_farm_push({"agents": [{"owner": "o", "name": "n", "state": "pondering"}]})[0].state,
                         "pondering")

    def test_extra_fields_dropped(self):
        agents = parse_farm_push({"agents": [{"owner": "o", "name": "n", "state": "s",
                                              "transcript": "secret", "path": "/Users/x"}], "extra": 1})
        self.assertEqual(agents[0].to_json(), {"owner": "o", "name": "n", "state": "s"})

    def test_same_names_both_kept(self):
        agents = parse_farm_push({"agents": [{"owner": "o", "name": "n", "state": "busy"},
                                             {"owner": "o", "name": "n", "state": "busy"}]})
        self.assertEqual(len(agents), 2)

    def test_wrong_types_are_400(self):
        for body in ([], {"agents": {}}, {"agents": [1]}, {"agents": [{"owner": 1, "name": "n", "state": "s"}]},
                     {"agents": [{"name": "n", "state": "s"}]}, {}):
            with self.assertRaises(ValidationError) as cm:
                parse_farm_push(body)
            self.assertEqual(cm.exception.status, 400, body)

    def test_limits_are_413(self):
        long = "x" * 257
        for body in ({"agents": [{"owner": "o", "name": long, "state": "s"}]},
                     {"agents": [{"owner": "o", "name": "n", "state": "s"}] * 201}):
            with self.assertRaises(ValidationError) as cm:
                parse_farm_push(body)
            self.assertEqual(cm.exception.status, 413)

    def test_limit_is_bytes_not_characters(self):
        ok = "é" * 128                                     # 256 bytes
        parse_farm_push({"agents": [{"owner": "o", "name": ok, "state": "s"}]})
        with self.assertRaises(ValidationError):
            parse_farm_push({"agents": [{"owner": "o", "name": ok + "x", "state": "s"}]})

    def test_exactly_at_limits_is_fine(self):
        parse_farm_push({"agents": [{"owner": "o" * 256, "name": "n", "state": "s"}] * 200})

    def test_lone_surrogate_is_400(self):
        """JSON can spell a lone surrogate, which is not text and has no UTF-8
        form. It is refused like any other malformed value (R9.10)."""
        for field in ("owner", "name", "state"):
            raw = ('{"agents":[{"owner":"o","name":"n","state":"s"}]}'
                   .replace(f'"{field}":"{field[0]}"', f'"{field}":"x\\ud800"')).encode()
            with self.assertRaises(ValidationError) as cm:
                parse_farm_push(decode_json(raw))
            self.assertEqual(cm.exception.status, 400, field)
            self.assertIn("not valid Unicode", cm.exception.message)

    def test_surrogate_pair_is_one_character(self):
        agents = parse_farm_push(decode_json(b'{"agents":[{"owner":"o","name":"\\ud83d\\ude00","state":"s"}]}'))
        self.assertEqual(agents[0].name, "\U0001F600")

    def test_same_agents_ignores_order_not_multiplicity(self):
        a, b = Agent("o", "a", "busy"), Agent("o", "b", "idle")
        self.assertTrue(same_agents((a, b), (b, a)))
        self.assertFalse(same_agents((a, a, b), (a, b, b)))
        self.assertFalse(same_agents((a,), (a, a)))


class RecordTest(unittest.TestCase):
    def test_round_trip(self):
        rec = parse_record(record_json())
        self.assertEqual(rec.content_version(), (1763300000000, 3))
        self.assertEqual(rec.liveness_version(), (1763300000000, 77))
        self.assertEqual(parse_record(rec.to_json()), rec)

    def test_null_farm_age(self):
        self.assertIsNone(parse_record(record_json(farm_age_s=None)).farm_age_s)

    def test_bad_fields_are_400(self):
        for change in ({"counter": 0}, {"rev": 0}, {"counter": True}, {"incarnation": -1},
                       {"incarnation": 1 << 63}, {"farm_age_s": "7"}, {"farm_age_s": -1},
                       {"leaving": 0}, {"grange": ""}, {"agents": None}):
            with self.assertRaises(ValidationError) as cm:
                parse_record(record_json(**change))
            self.assertEqual(cm.exception.status, 400, change)

    def test_missing_field_is_400(self):
        body = record_json()
        del body["rev"]
        with self.assertRaises(ValidationError):
            parse_record(body)

    def test_unknown_fields_dropped(self):
        rec = parse_record(record_json(hostname="laptop.local"))
        self.assertNotIn("hostname", rec.to_json())


class DigestAndMessageTest(unittest.TestCase):
    def test_digest(self):
        d = parse_digest({"djp3": [1763323200123, 418, 12, 7.2], "alice": [1, 2, 3, None]})
        self.assertEqual(d["djp3"], DigestEntry(1763323200123, 418, 12, 7.2))
        self.assertEqual(d["alice"].to_json(), [1, 2, 3, None])

    def test_digest_shape_is_400(self):
        for value in ({"a": [1, 2, 3]}, {"a": "x"}, [], {"a": [1, 0, 1, None]}, {"": [1, 1, 1, None]}):
            with self.assertRaises(ValidationError) as cm:
                parse_digest(value)
            self.assertEqual(cm.exception.status, 400, value)

    def test_lone_surrogate_in_a_record_or_digest_is_400(self):
        for value, parse in ((record_json(grange="a\udc80"), parse_record),
                             (record_json(group="g\udc80"), parse_record),
                             ({"a\udc80": [1, 1, 1, None]}, parse_digest)):
            with self.assertRaises(ValidationError) as cm:
                parse(value)
            self.assertEqual(cm.exception.status, 400, value)

    def test_digest_cap_is_413(self):
        with self.assertRaises(ValidationError) as cm:
            parse_digest({f"g{i}": [1, 1, 1, None] for i in range(1001)})
        self.assertEqual(cm.exception.status, 413)

    def test_push_and_repair_shapes(self):
        push = parse_gossip({"proto": 1, "group": "default", "from": "djp3", "records": [record_json()]})
        self.assertFalse(push.is_repair)
        repair = parse_gossip({"proto": 1, "group": "default", "from": "djp3", "digest": {}, "records": []})
        self.assertTrue(repair.is_repair)
        self.assertEqual(parse_gossip(repair.to_json()).to_json(), repair.to_json())

    def test_body_cap_is_413(self):
        with self.assertRaises(ValidationError) as cm:
            parse_gossip({"proto": 1, "group": "default", "from": "x", "records": [record_json()] * 201})
        self.assertEqual(cm.exception.status, 413)


if __name__ == "__main__":
    unittest.main()

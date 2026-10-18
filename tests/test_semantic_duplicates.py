"""Offline safety regressions; model judgements are simulated, not quality evals."""
import json
import time
import unittest

from text_processing import AlertTranslation, remove_confirmed_cross_source_duplicates


class SemanticDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.history = [{"id": "p1", "source": "primary", "at": time.time(),
                         "text": "A drone is heading toward Brovary"}]
        self.source = "Дрон зараз летить на Бровари"

    def translation(self, updates):
        payload = {"decision": "PUBLISH", "operational_updates": [
            {"source": source, "translation": output,
             "novelty": {"status": status, "duplicate_of": refs}}
            for source, output, status, refs in updates
        ]}
        return AlertTranslation("\n".join(x[1] for x in updates), json.dumps(payload), list(self.history))

    def check(self, output, source=None, status="DUPLICATE", refs=None):
        source = source or self.source
        result = self.translation([(source, output, status, ["p1"] if refs is None else refs)])
        return remove_confirmed_cross_source_duplicates(result, source, "backup", self.history)

    def test_equivalent_fact_both_source_orders(self):
        for previous, current in (("primary", "backup"), ("backup", "primary")):
            self.history[0]["source"] = previous
            result = self.translation([(self.source, "A UAV is moving toward Brovary", "DUPLICATE", ["p1"])])
            output, removed = remove_confirmed_cross_source_duplicates(result, self.source, current, self.history)
            self.assertEqual(output, "")
            self.assertEqual(removed[0]["duplicate_of"], ["p1"])

    def test_malicious_or_incorrect_duplicate_decision_cannot_remove_changed_facts(self):
        for output in ("2 drones are heading toward Brovary", "A drone is heading toward Kyiv",
                       "Two drones are heading toward Brovary", "A drone is heading toward Brovary via Kozyn",
                       "A drone is flying low toward Brovary",
                       "A jet-powered UAV is heading toward Brovary", "A missile is heading toward Brovary",
                       "A drone was shot down over Brovary", "A drone turned away from Brovary",
                       "A drone may be heading toward Brovary", "No drones are heading toward Brovary",
                       "Another drone is heading toward Brovary", "New launches toward Brovary"):
            with self.subTest(output=output):
                self.assertEqual(self.check(output)[0], output)

    def test_short_ambiguous_and_unknown_references_pass(self):
        for source in ("Бровари", "Збили", "Ще один дрон на Бровари", "Знову дрон на Бровари"):
            self.assertTrue(self.check("A drone is heading toward Brovary", source)[0])
        for status, refs in (("UNCERTAIN", ["p1"]), ("NEW", []), ("DUPLICATE", ["missing"]),
                             ("DUPLICATE", []), ("DUPLICATE", ["p1", "p1"])):
            self.assertTrue(self.check("A drone is heading toward Brovary", status=status, refs=refs)[0])

    def test_mixed_update_keeps_new_route(self):
        new_source = "Повернув у напрямку Києва"
        result = self.translation([(self.source, "A UAV is heading toward Brovary", "DUPLICATE", ["p1"]),
                                   (new_source, "Turned toward Kyiv", "NEW", [])])
        output, removed = remove_confirmed_cross_source_duplicates(
            result, self.source + ". " + new_source, "backup", self.history)
        self.assertEqual(output, "Turned toward Kyiv")
        self.assertEqual(len(removed), 1)

    def test_stale_history_concurrent_arrival_and_same_source_pass(self):
        result = self.translation([(self.source, "A UAV is heading toward Brovary", "DUPLICATE", ["p1"])])
        for history, source in (([], "backup"), (self.history, "primary"),
                                (self.history + [dict(self.history[0], id="p2")], "backup")):
            self.assertEqual(remove_confirmed_cross_source_duplicates(result, self.source, source, history)[0], result)
        self.history[0]["at"] -= 121
        self.assertTrue(self.check("A drone is heading toward Brovary")[0])

    def test_invalid_evidence_and_plain_translation_fail_open(self):
        result = AlertTranslation("A UAV toward Brovary", "invalid", self.history)
        self.assertEqual(remove_confirmed_cross_source_duplicates(result, self.source, "backup", self.history),
                         (str(result), []))
        self.assertEqual(remove_confirmed_cross_source_duplicates("SkyMall", "СкайМол", "backup", self.history),
                         ("SkyMall", []))

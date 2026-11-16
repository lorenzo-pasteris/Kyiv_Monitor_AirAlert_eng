"""Regressions from public Nov 14–16 posts; model JSON is constructed, never billed."""
import asyncio
import json
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_alert_reliability as reliability
from test_routing import FakeTelegramMessage, monitor
from text_processing import (
    alert_numeric_facts, clean_alert_source_text, is_commentary_alert_message,
    is_non_operational_alert_message, is_priority_alert_message, parse_alert_gate_output,
    protected_place_tokens, restore_alert_place_tokens, strip_mixed_alert_commentary,
    translate_known_terse_fragment,
)

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/alerts_2026_11_14_16.json').read_text())
REAL = {item['id']: item for item in FIXTURE['messages']}


def validated(source, output, excerpt=None):
    raw = json.dumps({'decision': 'PUBLISH', 'operational_updates': [
        {'source': excerpt or source, 'translation': output}]})
    return parse_alert_gate_output(restore_alert_place_tokens(raw, source), source, require_excerpts=True)


class RealMessageTests(unittest.TestCase):
    def test_incoming_ballistic_reports_are_not_potential_threats(self):
        for ident in (23447, 23459):
            self.assertEqual(translate_known_terse_fragment(REAL[ident]['text']),
                             'Ballistic missiles heading toward Kyiv!')
        source = REAL[23446]['text']
        self.assertEqual(validated(source, 'Ballistic threat from Bryansk and Kursk')[0], 'PUBLISH')
        self.assertIsNone(translate_known_terse_fragment(source))

    def test_real_launch_shelter_landmark_and_outcome_fragments(self):
        expected = {
            23449: 'Numerous launches.', 23448: 'Take cover immediately!',
            23454: 'Hydroelectric power station, bridges!', 23435: 'Falling.',
            23421: 'It appears to have fallen in the forest.',
            23315: 'No threats currently reported in Kyiv.',
            23286: 'No longer observed.', 23463: 'Currently clear.',
        }
        for ident, output in expected.items():
            with self.subTest(id=ident):
                source = REAL[ident]['text']
                self.assertEqual(translate_known_terse_fragment(source), output)
                for category in ('ENGAGEMENT', 'NON_OPERATIONAL_NEWS', 'DISTANT_WITHOUT_KYIV_TRAJECTORY'):
                    raw = json.dumps(dict(decision='DROP', block_category=category, evidence=source, translation=output))
                    self.assertEqual(parse_alert_gate_output(raw, source, require_excerpts=True)[0], 'PUBLISH')
        for source in ('Багато виходів', 'Масові пуски', 'Повторні виходи', 'Нові пуски'):
            self.assertTrue(is_priority_alert_message(source))
            self.assertIsNotNone(translate_known_terse_fragment(source))
        for source in ('Виходи з метро закриті', 'Скільки виходів?', 'Багато виходів, підпишіться!'):
            self.assertIsNone(translate_known_terse_fragment(source))

    def test_all_real_numeric_failures_accept_faithful_translations(self):
        outputs = {
            23308: 'Two remain, Hostomel/Boyarka',
            23310: 'The other one is heading north in the region, Dymer',
            23314: 'The last one is heading toward Lebedivka',
            23333: 'Another jet-powered UAV toward CHP-6, Troieshchyna',
            23338: 'This one is heading toward Hostomel',
            23353: 'Another Banderol toward the reservoir',
            23430: 'Two more in the north of the region, the first toward Vyshhorod',
            23431: 'This one was also intercepted',
            23432: 'The last one is near Dymer',
        }
        for ident, output in outputs.items():
            with self.subTest(id=ident):
                self.assertEqual(validated(REAL[ident]['text'], output)[0], 'PUBLISH')
        self.assertEqual(alert_numeric_facts('Another one'), ['1'])
        self.assertEqual(alert_numeric_facts('Двоє, троє, четверо'), ['2', '3', '4'])
        self.assertEqual(validated('Черговий дрон повз Княжичі на ДВРЗ',
                                   'Another drone past Kniazhychi toward DVRZ')[0], 'PUBLISH')
        for source, output in (
            (REAL[23308]['text'], 'Three remain, Hostomel/Boyarka'),
            ('2 ракети на Київ', '3 missiles toward Kyiv'),
            ('Ще один реактив на ТЕЦ-6, Троєщину', '2 jet-powered UAVs toward CHP-6, Troieshchyna'),
            ('Дрон на висоті 100м', 'Drone at 200m altitude'),
            ('Не збили', 'Shot down'),
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                validated(source, output)

    def test_place_errors_and_known_source_typo(self):
        for ident, good in (
            (23403, 'Next, Dymerka'), (23342, 'Solomianka next'),
            (23287, 'Another one toward Petrivtsi, Vyshhorod'),
            (23319, 'Jet-powered UAV toward Petrivtsi/Vyshhorod'),
        ):
            with self.subTest(id=ident):
                self.assertEqual(validated(REAL[ident]['text'], good)[0], 'PUBLISH')
                if REAL[ident]['published']:
                    with self.assertRaises(ValueError):
                        validated(REAL[ident]['text'], REAL[ident]['published']['text'])
        self.assertEqual(set(protected_place_tokens('Димерка, Димер').values()), {'Dymerka', 'Dymer'})
        self.assertEqual(set(protected_place_tokens('Вишглрод').values()), {'Vyshhorod'})
        with self.assertRaises(ValueError):
            validated('Далі Димерка', 'Dymerka via Dymer')

    def test_false_all_clear_is_blocked_even_in_a_larger_message(self):
        for ident in (23286, 23463):
            with self.subTest(id=ident), self.assertRaisesRegex(ValueError, 'All clear invented'):
                validated(REAL[ident]['text'], REAL[ident]['published']['text'])
        with self.assertRaisesRegex(ValueError, 'All clear invented'):
            validated('Все, чисто по швидкісних, уважно до відбою',
                      'All clear for high-speed targets, stay alert until the all clear')
        self.assertEqual(validated('Відбій по балістиці', 'Ballistic threat all clear')[0], 'PUBLISH')

    def test_missed_interception_cannot_disappear_by_selecting_a_smaller_excerpt(self):
        source = REAL[23417]['text']
        output = 'Moving toward Vyshhorod at 100m altitude'
        with self.assertRaisesRegex(ValueError, 'Operational fact omitted'):
            validated(source, output, 'виліз в бік Вишгорода на висоті 100м!')
        with self.assertRaises(ValueError):
            validated(source, 'Apparently moving toward Vyshhorod at 100m altitude')
        self.assertEqual(validated(source, 'Apparently missed; it reappeared toward Vyshhorod at 100m altitude')[0], 'PUBLISH')
        with self.assertRaises(ValueError):
            validated('Схоже збили', 'Shot down', 'збили')

    def test_comments_are_removed_without_losing_operational_observation(self):
        for ident in (23444, 23376):
            source = REAL[ident]['text']
            self.assertTrue(is_commentary_alert_message(source))
            raw = json.dumps({'decision': 'PUBLISH', 'operational_updates': [
                {'source': source, 'translation': 'Commentary'}]})
            self.assertEqual(parse_alert_gate_output(raw, source, require_excerpts=True)[0], 'DROP')
        clean = strip_mixed_alert_commentary(REAL[23377]['text'])
        self.assertEqual(clean, 'У русні продовжує працювати ППО')
        self.assertEqual(validated(clean, 'Russian air defence continues to operate')[0], 'PUBLISH')
        for ident in (23468, 23471):
            self.assertTrue(is_non_operational_alert_message(clean_alert_source_text(REAL[ident]['text'])))


class RealDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reliability.AlertDeliveryTests.asyncSetUp(self)
        self.set(monitor, 'alert_output_entity', None)

    set = reliability.AlertDeliveryTests.set
    msg = reliability.AlertDeliveryTests.msg
    process = reliability.AlertDeliveryTests.process
    record = reliability.AlertDeliveryTests.record
    make_due = reliability.AlertDeliveryTests.make_due

    async def unknown(self):
        self.set(monitor, 'production_client', SimpleNamespace(get_messages=AsyncMock()))
        monitor.state_store.persist_operational_state('last_alert_output_id', '499')
        monitor.safe_send.return_value = None
        self.set(monitor, 'translate_message', reliability.REAL_TRANSLATE)
        await self.process(self.msg(1, REAL[23435]['text']))
        self.make_due(send_started_at=time.time() - 25, next_reconcile=0)
        return self.record()

    def history(self, delivered=False):
        rows = [FakeTelegramMessage(499, 'Earlier alert', self.now - timedelta(seconds=40))]
        if delivered:
            rows.insert(0, FakeTelegramMessage(500, '🟡 Falling.', self.now - timedelta(seconds=15)))
        monitor.production_client.get_messages.return_value = rows

    async def test_real_terse_messages_go_through_delivery_without_paid_model_call(self):
        self.set(monitor, 'translate_message', reliability.REAL_TRANSLATE)
        self.set(monitor, 'http_client', SimpleNamespace(post=AsyncMock(side_effect=AssertionError('No API call'))))
        for ident in sorted((23447, 23448, 23449, 23454, 23435, 23421, 23315, 23286, 23463, 23459)):
            self.assertTrue(await self.process(self.msg(ident, REAL[ident]['text'])))
            self.assertEqual(self.record(ident)['status'], 'published')
        self.assertEqual(monitor.safe_send.await_count, 10)
        monitor.http_client.post.assert_not_awaited()

    async def test_real_numeric_case_uses_full_translation_parser_and_delivery(self):
        self.set(monitor, 'translate_message', reliability.REAL_TRANSLATE)
        self.set(monitor.httpx, 'Timeout', lambda *args, **kwargs: None)
        source = REAL[23308]['text']
        raw = reliability.publish(source, 'Two remain, Hostomel/Boyarka')
        self.set(monitor, 'http_client', SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {'stop_reason': 'end_turn', 'content': [{'text': raw}]}))))
        await self.process(self.msg(23308, source))
        self.assertEqual(self.record(23308)['status'], 'published')
        monitor.http_client.post.assert_awaited_once()

    async def test_history_confirms_unknown_send_after_restart_without_duplicate(self):
        await self.unknown()
        self.history(delivered=True)
        monitor.state_store.initialize_stats_db()
        await monitor.reconcile_unknown_alerts()
        self.assertEqual(self.record()['status'], 'published')
        self.assertEqual(self.record()['output_id'], 500)
        monitor.safe_send.assert_awaited_once()

    async def test_two_complete_absence_checks_allow_only_one_fresh_resend(self):
        await self.unknown()
        self.history()
        monitor.safe_send.return_value = {'message_id': 501}
        await monitor.reconcile_unknown_alerts()
        self.assertIn('absent_since', self.record())
        monitor.safe_send.assert_awaited_once()
        self.make_due(next_reconcile=0, absent_since=time.time() - 11)
        await monitor.reconcile_unknown_alerts()
        self.assertEqual(self.record()['status'], 'published')
        self.assertEqual(self.record()['output_id'], 501)
        self.assertEqual(monitor.safe_send.await_count, 2)
        await monitor.reconcile_unknown_alerts()
        self.assertEqual(monitor.safe_send.await_count, 2)

    async def test_recovered_message_uses_current_alert_colour(self):
        await self.unknown()
        self.history()
        self.make_due(next_reconcile=0, absent_since=time.time() - 11)
        monitor.alert_level = 'RED'
        monitor.safe_send.return_value = {'message_id': 501}
        await monitor.reconcile_unknown_alerts()
        self.assertEqual(monitor.safe_send.call_args.args[0], '🔴 Falling.')
        self.assertEqual(self.record()['attempt_text'], '🔴 Falling.')

    async def test_existing_session_resolves_output_and_rejects_wrong_channel(self):
        entity = SimpleNamespace(id=123)
        client = SimpleNamespace(get_entity=AsyncMock(return_value=entity),
                                 get_messages=AsyncMock(return_value=[self.msg(499)]))
        self.set(monitor, 'production_client', client)
        self.set(monitor.utils, 'get_peer_id', lambda value: int(monitor.ALERT_OUTPUT_CHAT_ID))
        await monitor.prepare_alert_reconciliation()
        self.assertIs(monitor.alert_output_entity, entity)
        self.assertEqual(monitor.state_store.load_operational_state('last_alert_output_id'), '499')
        self.set(monitor.utils, 'get_peer_id', lambda value: 999)
        await monitor.prepare_alert_reconciliation()
        self.assertIsNone(monitor.alert_output_entity)
        self.assertEqual(client.get_messages.await_count, 1)
        client.get_entity.side_effect = TimeoutError
        await monitor.prepare_alert_reconciliation()
        self.assertIsNone(monitor.alert_output_entity)

    async def test_empty_partial_or_failed_history_is_never_absence(self):
        await self.unknown()
        for history in ([], [FakeTelegramMessage(501, 'A newer update', self.now)]):
            self.make_due(next_reconcile=0, absent_since=time.time() - 11)
            monitor.production_client.get_messages.return_value = history
            await monitor.reconcile_unknown_alerts()
            self.assertNotIn('absent_since', self.record())
        monitor.production_client.get_messages.side_effect = TimeoutError
        self.make_due(next_reconcile=0)
        await monitor.reconcile_unknown_alerts()
        monitor.safe_send.assert_awaited_once()

    async def test_pending_send_lock_does_not_block_poller(self):
        await self.unknown()
        lock = monitor.alert_message_locks.setdefault((self.source, 1), asyncio.Lock())
        async with lock:
            await asyncio.wait_for(monitor.reconcile_unknown_alerts(), timeout=0.1)
        monitor.production_client.get_messages.assert_not_awaited()

    async def test_all_clear_expiry_or_episode_change_prevents_resend(self):
        await self.unknown()
        self.history()
        self.make_due(next_reconcile=0, absent_since=time.time() - 11)
        monitor.alert_active = False
        await monitor.reconcile_unknown_alerts()
        monitor.alert_active = True
        self.make_due(deadline=time.time() - 1)
        await monitor.reconcile_unknown_alerts()
        self.assertEqual(self.record()['status'], 'unresolved_delivery')
        self.make_due(status='delivery_unknown', next_reconcile=0, deadline=time.time() + 60, episode='old')
        await monitor.reconcile_unknown_alerts()
        monitor.safe_send.assert_awaited_once()

    async def test_all_clear_while_reading_history_prevents_resend(self):
        await self.unknown()
        self.make_due(next_reconcile=0, absent_since=time.time() - 11)
        async def changing_state(*args, **kwargs):
            monitor.alert_active = False
            return [FakeTelegramMessage(499, 'Earlier alert', self.now - timedelta(seconds=40))]
        monitor.production_client.get_messages.side_effect = changing_state
        await monitor.reconcile_unknown_alerts()
        monitor.safe_send.assert_awaited_once()

    async def test_resend_failure_is_bounded_and_older_identical_post_is_not_confirmation(self):
        await self.unknown()
        self.history()
        monitor.production_client.get_messages.return_value[0].text = '🟡 Falling.'
        self.make_due(next_reconcile=0, absent_since=time.time() - 11)
        await monitor.reconcile_unknown_alerts()
        self.assertEqual(self.record()['status'], 'delivery_unknown')
        self.assertEqual(monitor.safe_send.await_count, 2)
        for _ in range(5):
            self.make_due(next_reconcile=0, absent_since=time.time() - 11)
            await monitor.reconcile_unknown_alerts()
        self.assertEqual(monitor.safe_send.await_count, 2)
        self.assertEqual(self.record()['status'], 'unresolved_delivery')

    async def test_delivery_logs_exception_type_but_not_secret_exception_text(self):
        self.set(monitor, 'http_client', SimpleNamespace(post=AsyncMock(side_effect=TimeoutError('SECRET'))))
        self.set(monitor.httpx, 'Timeout', lambda *args, **kwargs: None)
        with patch('builtins.print') as logged:
            await monitor.telegram_request('sendMessage', {'text': 'test'})
        self.assertIn('TimeoutError', str(logged.call_args_list))
        self.assertNotIn('SECRET', str(logged.call_args_list))

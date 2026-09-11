import unittest
from unittest.mock import Mock, patch

from ...engines.base import Base
from ...lib.exception import TranslationCanceled
from ...lib.novel import NovelTranslator, ContextManager


class AbortableEngine(Base):
    name = 'Abortable'
    stream = True
    endpoint = 'https://example.test'

    def get_result(self, response):
        return response


class TestAbort(unittest.TestCase):
    def test_abort_closes_the_response_being_read(self):
        AbortableEngine.set_config({"api_keys": ["k"]})
        engine = AbortableEngine()
        response = Mock()
        with patch('calibre_plugins.novel_translator.engines.base.request',
                   return_value=response):
            engine.translate('x')
        engine.abort()
        response.close.assert_called_once_with()

    def test_abort_with_nothing_in_flight_is_harmless(self):
        AbortableEngine.set_config({"api_keys": ["k"]})
        AbortableEngine().abort()


class TestCancelDuringARequest(unittest.TestCase):
    """A cancel raised while the model is answering stops the run at
    once, with nothing of the cut-short reply kept and no pause waited
    out."""

    def _translator(self, engine):
        cache = Mock()
        cache.get_info.return_value = None
        translator = NovelTranslator(
            engine, [], ContextManager(cache).load(), cache, config={})
        translator.set_logging(Mock())
        return translator

    def test_a_reply_cut_short_by_a_cancel_is_not_a_reply(self):
        engine = Mock()
        engine.request_attempt = 3
        canceled = []
        engine.translate.side_effect = lambda text: (
            canceled.append(True) or '[1]\nHalf a')
        translator = self._translator(engine)
        translator.set_cancel_request(lambda: bool(canceled))
        with self.assertRaises(TranslationCanceled):
            translator._translate_with_retry('system', 'user')
        self.assertEqual(1, engine.translate.call_count)

    def test_an_error_after_a_cancel_is_the_cancel(self):
        engine = Mock()
        engine.request_attempt = 3
        canceled = []
        engine.translate.side_effect = lambda text: (
            canceled.append(True) or (_ for _ in ()).throw(
                Exception('connection closed')))
        translator = self._translator(engine)
        translator.set_cancel_request(lambda: bool(canceled))
        with patch('calibre_plugins.novel_translator.lib.novel.time.sleep'
                   ) as sleep:
            with self.assertRaises(TranslationCanceled):
                translator._translate_with_retry('system', 'user')
        sleep.assert_not_called()

    def test_the_pause_between_attempts_notices_a_cancel(self):
        engine = Mock()
        engine.request_attempt = 3
        engine.translate.side_effect = Exception('boom')
        translator = self._translator(engine)
        calls = []
        translator.set_cancel_request(lambda: len(calls) > 2)
        with patch('calibre_plugins.novel_translator.lib.novel.time.sleep',
                   side_effect=lambda s: calls.append(s)):
            with self.assertRaises(TranslationCanceled):
                translator._translate_with_retry('system', 'user')
        # Slept in short steps, not the whole five seconds at once.
        self.assertTrue(all(step <= 0.5 for step in calls))

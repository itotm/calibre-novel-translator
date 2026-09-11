import json
import unittest
from unittest.mock import Mock, patch

from ...lib.exception import (
    HTTPRequestError, UnexpectedResult, TranslationFailed)
from ...lib.novel import (
    NovelTranslator, ContextManager, describe_error, retry_after,
    is_rate_limited, is_routing_dead_end)


RATE_LIMIT_BODY = json.dumps({'error': {
    'message': 'Provider returned error', 'code': 429,
    'metadata': {
        'raw': 'deepseek/deepseek-v4-flash-0731 is temporarily '
               'rate-limited upstream. Please retry shortly.',
        'provider_name': 'Makora', 'provider_error_code': 'capacity',
        'retry_after_seconds': 1}}})

DEAD_END_BODY = json.dumps({'error': {
    'message': 'No endpoints found that can handle the requested '
               'parameters. To learn more about provider routing, visit: '
               'https://openrouter.ai/docs/guides/routing',
    'code': 404}})


def wrapped(http):
    """What Base.translate raises around an HTTP error."""
    error = UnexpectedResult('Can not parse returned response. Raw data: '
                             '\n\nTraceback (most recent call last):\n'
                             '  File "x.py", line 1\n' + str(http))
    error.cause = http
    return error


class TestDescribeError(unittest.TestCase):
    def test_a_provider_error_is_one_line(self):
        http = HTTPRequestError(429, 'Too Many Requests', RATE_LIMIT_BODY)
        text = describe_error(wrapped(http))
        self.assertEqual(
            'HTTP 429: Provider returned error: deepseek/deepseek-v4-flash'
            '-0731 is temporarily rate-limited upstream. Please retry '
            'shortly.', text)

    def test_a_traceback_is_reduced_to_its_message(self):
        error = Exception(
            'Can not parse returned response. Raw data: \n\n'
            'Traceback (most recent call last):\n'
            '  File "x.py", line 1, in f\n'
            '    ~~~^^^\n'
            'ValueError: not json')
        self.assertEqual('ValueError: not json', describe_error(error))

    def test_retry_after_from_header_or_body(self):
        self.assertEqual(7.0, retry_after(wrapped(
            HTTPRequestError(429, 'x', RATE_LIMIT_BODY, retry_after=7))))
        self.assertEqual(1.0, retry_after(wrapped(
            HTTPRequestError(429, 'x', RATE_LIMIT_BODY))))
        self.assertIsNone(retry_after(Exception('boom')))

    def test_classification(self):
        self.assertTrue(is_rate_limited(wrapped(
            HTTPRequestError(429, 'Too Many Requests', RATE_LIMIT_BODY))))
        self.assertTrue(is_rate_limited(Exception('rate limit exceeded')))
        self.assertFalse(is_rate_limited(Exception('boom')))
        self.assertTrue(is_routing_dead_end(wrapped(
            HTTPRequestError(404, 'Not Found', DEAD_END_BODY))))
        self.assertFalse(is_routing_dead_end(wrapped(
            HTTPRequestError(404, 'Not Found', '{"error": {"message": '
                             '"model not found"}}'))))


class TestRetryLoop(unittest.TestCase):
    def _translator(self, engine, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        translator = NovelTranslator(
            engine, [], ContextManager(cache).load(), cache,
            config=config or {})
        translator.set_logging(Mock())
        return translator

    def _engine(self, replies):
        engine = Mock()
        engine.request_attempt = 3
        engine.translate.side_effect = replies
        del engine.relax_parameters
        return engine

    def test_a_rate_limit_is_waited_out_not_spent(self):
        http = HTTPRequestError(429, 'Too Many Requests', RATE_LIMIT_BODY)
        engine = self._engine(
            [wrapped(http), wrapped(http), wrapped(http), wrapped(http),
             'done'])
        translator = self._translator(engine)
        waits = []
        with patch.object(translator, '_wait', side_effect=waits.append):
            self.assertEqual(
                'done', translator._translate_with_retry('s', 'u'))
        # Five requests for three attempts: the limits did not count.
        self.assertEqual(5, engine.translate.call_count)
        # Waited at least what the provider asked, growing each time.
        self.assertEqual(4, len(waits))
        self.assertTrue(all(w >= 1 for w in waits))
        self.assertTrue(waits[-1] > waits[0])
        logged = ' '.join(
            c.args[0] for c in translator.log.call_args_list if c.args)
        self.assertIn('rate limited', logged)
        self.assertNotIn('Traceback', logged)

    def test_the_patience_has_a_limit(self):
        http = HTTPRequestError(429, 'Too Many Requests', RATE_LIMIT_BODY)
        engine = self._engine([wrapped(http)] * 10)
        translator = self._translator(
            engine, {'novel_rate_limit_max_wait': 5})
        with patch.object(translator, '_wait'):
            with self.assertRaises(TranslationFailed) as raised:
                translator._translate_with_retry('s', 'u')
        # Three attempts plus the few waits that fit in five seconds.
        self.assertLess(engine.translate.call_count, 8)
        self.assertIn('HTTP 429', str(raised.exception))
        self.assertNotIn('Traceback', str(raised.exception))

    def test_zero_patience_treats_it_as_an_error(self):
        http = HTTPRequestError(429, 'Too Many Requests', RATE_LIMIT_BODY)
        engine = self._engine([wrapped(http)] * 10)
        translator = self._translator(
            engine, {'novel_rate_limit_max_wait': 0})
        with patch.object(translator, '_wait'):
            with self.assertRaises(TranslationFailed):
                translator._translate_with_retry('s', 'u')
        self.assertEqual(3, engine.translate.call_count)

    def test_a_routing_dead_end_relaxes_the_parameters_once(self):
        http = HTTPRequestError(404, 'Not Found', DEAD_END_BODY)
        engine = self._engine([wrapped(http), 'done'])
        engine.relax_parameters = Mock(return_value=True)
        translator = self._translator(engine)
        with patch.object(translator, '_wait'):
            self.assertEqual(
                'done', translator._translate_with_retry('s', 'u'))
        engine.relax_parameters.assert_called_once_with()
        self.assertEqual(2, engine.translate.call_count)

    def test_a_dead_end_with_nothing_to_relax_is_a_failure(self):
        http = HTTPRequestError(404, 'Not Found', DEAD_END_BODY)
        engine = self._engine([wrapped(http)] * 5)
        engine.relax_parameters = Mock(return_value=False)
        translator = self._translator(engine)
        with patch.object(translator, '_wait'):
            with self.assertRaises(TranslationFailed):
                translator._translate_with_retry('s', 'u')
        self.assertEqual(3, engine.translate.call_count)

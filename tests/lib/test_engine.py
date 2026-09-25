import io
import re
import json
from http.client import IncompleteRead
import unittest
from pathlib import Path
from types import GeneratorType
from unittest.mock import patch, Mock

from mechanize import HTTPError  # type: ignore
from mechanize._response import (  # type: ignore
    closeable_response as mechanize_response)

from ...lib.cache import Paragraph
from ...engines.base import Base
from ...lib.exception import UnexpectedResult
from ...engines.genai import GenAI
from ...engines.openai import ChatgptTranslate
from ...engines.openrouter import OpenRouterTranslate
from ...engines.anthropic import ClaudeTranslate


module_name = 'calibre_plugins.novel_translator.engines'


class MockEngine(Base):
    endpoint = 'https://example.com/api'

    def get_headers(self):
        return {
            'Authorization': 'Bearer a', 'Content-Type': 'application/json'}

    def get_body(self, text):
        return json.dumps({'text': text})


class TestBase(unittest.TestCase):
    def setUp(self):
        self.translator = MockEngine()

    def test_class(self):
        self.assertIsNone(Base.name)
        self.assertIsNone(Base.alias)

        self.assertEqual({}, Base.lang_codes)
        self.assertEqual({}, Base.config)
        self.assertIsNone(Base.endpoint)
        self.assertEqual('POST', Base.method)
        self.assertFalse(Base.stream)

        self.assertTrue(Base.need_api_key)
        self.assertEqual('API Keys', Base.api_key_hint)
        self.assertEqual(r'^[^\s]+$', Base.api_key_pattern)
        self.assertEqual(['401'], Base.api_key_errors)
        self.assertEqual('\n\n', Base.separator)
        self.assertEqual(
            ('{{{{id_{}}}}}', r'({{\s*)+id\s*_\s*{}\s*(\s*}})+'),
            Base.placeholder)
        self.assertIsNone(Base.using_tip)

        self.assertEqual(0, Base.concurrency_limit)
        self.assertEqual(0.0, Base.request_interval)
        self.assertEqual(3, Base.request_attempt)
        self.assertEqual(10.0, Base.request_timeout)
        self.assertEqual(10, Base.max_error_count)

    @patch.dict(Base.config, {
        'api_keys': ['a', 'b', 'c'],
        'concurrency_limit': 5,
        'request_interval': 10,
        'request_attempt': 3,
        'request_timeout': 10,
        'max_error_count': 20})
    def test_create_translator(self):
        translator = Base()

        self.assertIsNone(translator.proxy_type)
        self.assertIsNone(translator.proxy_host)
        self.assertIsNone(translator.proxy_port)

        self.assertEqual(['b', 'c'], translator.api_keys)
        self.assertEqual([], translator.bad_api_keys)
        self.assertEqual('a', translator.api_key)

        self.assertEqual(5, translator.concurrency_limit)
        self.assertEqual(10, translator.request_interval)
        self.assertEqual(3, translator.request_attempt)
        self.assertEqual(10, translator.request_timeout)
        self.assertEqual(20, translator.max_error_count)

    def test_placeholder(self):
        marks = [
            '{{id_1}}', '{id_1} }', '{{id_1}', '{ { id_1 }}', '{ { id _ 1 }']
        for mark in marks:
            with self.subTest(mark=mark):
                self.assertIsNotNone(re.search(
                    Base.placeholder[1].format(1), 'xxx %s xxx' % mark))

    def test_get_api_key_no_need_api_key(self):
        self.translator.need_api_key = False
        self.assertIsNone(self.translator.get_api_key())

    def test_get_api_key_with_empty_keys(self):
        self.translator.need_api_key = True
        self.translator.api_keys = []
        self.assertIsNone(self.translator.get_api_key())

    def test_get_api_key(self):
        self.translator.need_api_key = True
        self.translator.api_keys = ['a', 'b']
        self.assertEqual('a', self.translator.get_api_key())

    def test_swap_api_key_in_bad_keys(self):
        self.translator.api_key = 'a'
        self.translator.bad_api_keys = []
        self.assertFalse(self.translator.swap_api_key())

    def test_swap_api_key_with_none_new_api_key(self):
        self.translator.api_key = 'a'
        self.translator.bad_api_keys = []

        with patch.object(self.translator, 'get_api_key') as mock_get_api_key:
            mock_get_api_key.return_value = None
            self.assertFalse(self.translator.swap_api_key())

        self.assertEqual(['a'], self.translator.bad_api_keys)
        self.assertEqual(None, self.translator.api_key)

    def test_swap_api_key(self):
        self.translator.api_key = 'a'
        self.translator.bad_api_keys = []

        with patch.object(self.translator, 'get_api_key') as mock_get_api_key:
            mock_get_api_key.return_value = 'b'
            self.assertTrue(self.translator.swap_api_key())

        self.assertEqual(['a'], self.translator.bad_api_keys)
        self.assertEqual('b', self.translator.api_key)

    def test_need_swap_api_key_no_need_api_key(self):
        self.translator.need_api_key = False
        self.assertFalse(self.translator.need_swap_api_key('test error'))

    def test_need_swap_api_key_with_empty_keys(self):
        self.translator.need_api_key = True
        self.translator.api_keys = []
        self.assertFalse(self.translator.need_swap_api_key('test error'))

    def test_need_swap_api_key_without_key_errors(self):
        self.translator.need_api_key = True
        self.translator.api_keys = ['a']
        self.translator.api_key_errors = []
        self.assertFalse(self.translator.need_swap_api_key('test error'))

    def test_need_swap_api_key(self):
        self.translator.need_api_key = True
        self.translator.api_keys = ['a']
        self.translator.api_key_errors = ['error']
        self.assertTrue(self.translator.need_swap_api_key('test error'))

    @patch(module_name + '.base.request')
    def test_translate(self, mock_request):
        self.translator.stream = False
        mock_request.return_value = '{"text": "你好世界"}'

        self.assertEqual(
            '{"text": "你好世界"}', self.translator.translate('Hello World'))

        mock_request.assert_called_once_with(
            url='https://example.com/api', data='{"text": "Hello World"}',
            headers={
                'Authorization': 'Bearer a', 'Content-Type': 'application/json'
            }, method='POST', timeout=10.0, proxy_uri=None,
            raw_object=False, keepalive=False)

    @patch(module_name + '.base.request')
    def test_translate_with_stream(self, mock_request):
        self.translator.stream = True
        mock_response = Mock(mechanize_response)
        mock_request.return_value = mock_response

        self.assertIs(mock_response, self.translator.translate('Hello World'))

        mock_request.assert_called_once_with(
            url='https://example.com/api', data='{"text": "Hello World"}',
            headers={
                'Authorization': 'Bearer a', 'Content-Type': 'application/json'
            }, method='POST', timeout=10.0, proxy_uri=None,
            raw_object=True, keepalive=False)

    @patch(module_name + '.base.request')
    def test_translate_with_http_error(self, mock_request):
        mock_request.side_effect = Exception(
            'HTTP Error 409: Too many requests\n\n{"error": "any error"}')

        with self.assertRaises(Exception) as cm:
            self.translator.translate('Hello World')
        self.assertRegex(
            str(cm.exception), 'HTTP Error 409: Too many requests')
        self.assertRegex(str(cm.exception), '{"error": "any error"}')

    @patch(module_name + '.base.request')
    def test_translate_with_http_stream_parse_error(self, mock_request):
        self.translator.stream = True
        mock_response = Mock(mechanize_response)
        mock_request.return_value = mock_response

        with patch.object(self.translator, 'get_result') as mock_get_result:
            with self.assertRaises(Exception) as cm:
                mock_get_result.side_effect = Exception('test parse error')
                self.translator.translate('Hello World')
            self.assertRegex(str(cm.exception), 'test parse error')

    @patch(module_name + '.base.request')
    def test_translate_with_http_parse_error(self, mock_request):
        self.translator.stream = False
        mock_request.return_value = 'any unexpected result'

        with patch.object(self.translator, 'get_result') as mock_get_result:
            with self.assertRaises(Exception) as cm:
                mock_get_result.side_effect = Exception('test parse error')
                self.translator.translate('Hello World')
            self.assertRegex(
                str(cm.exception), 'test parse error\n\nany unexpected result')

    @patch(module_name + '.base.Base.need_swap_api_key')
    @patch(module_name + '.base.request')
    def test_translate_no_need_swap_api_keys(
            self, mock_request, mock_need_swap_api_key):
        mock_request.side_effect = Exception
        mock_need_swap_api_key.return_value = False

        self.assertRaises(
            UnexpectedResult, self.translator.translate, 'Hello World')
        self.assertEqual(1, mock_request.call_count)

    @patch(module_name + '.base.Base.swap_api_key')
    @patch(module_name + '.base.Base.need_swap_api_key')
    @patch(module_name + '.base.request')
    def test_translate_swap_api_keys_when_unavailable(
            self, mock_request, mock_need_swap_api_key, mock_swap_api_key):
        mock_request.side_effect = Exception
        mock_need_swap_api_key.return_value = True
        mock_swap_api_key.return_value = False

        self.assertRaises(
            UnexpectedResult, self.translator.translate, 'Hello World')
        self.assertEqual(1, mock_request.call_count)

    @patch(module_name + '.base.Base.swap_api_key')
    @patch(module_name + '.base.Base.need_swap_api_key')
    @patch(module_name + '.base.request')
    def test_translate_swap_api_keys_with_http_error(
            self, mock_request, mock_need_swap_api_key, mock_swap_api_key):
        self.translator.stream = True
        mock_response = Mock(mechanize_response)
        mock_request.side_effect = [HTTPError, HTTPError, mock_response]
        mock_need_swap_api_key.return_value = True
        mock_swap_api_key.return_value = True

        self.assertIs(mock_response, self.translator.translate('Hello World'))
        self.assertEqual(3, mock_request.call_count)

    @patch(module_name + '.base.Base.swap_api_key')
    @patch(module_name + '.base.Base.need_swap_api_key')
    @patch(module_name + '.base.request')
    def test_translate_swap_api_keys_with_http_error_without_result(
            self, mock_request, mock_need_swap_api_key, mock_swap_api_key):
        mock_request.side_effect = HTTPError(
            'https://example.com/api', 409, 'Too many requests', {},
            io.BytesIO(b'{"error": "any error"}'))
        mock_need_swap_api_key.side_effect = [True, True, False]

        self.assertRaises(
            UnexpectedResult, self.translator.translate, 'Hello World')

        self.assertEqual(3, mock_request.call_count)
        calls = mock_need_swap_api_key.mock_calls
        self.assertRegex(calls[0].args[0], 'Too many requests')
        self.assertRegex(calls[1].args[0], 'Too many requests')
        self.assertRegex(calls[2].args[0], 'Too many requests')

    @patch(module_name + '.base.Base.swap_api_key')
    @patch(module_name + '.base.Base.need_swap_api_key')
    @patch(module_name + '.base.Base.get_result')
    @patch(module_name + '.base.request')
    def test_translate_swap_api_keys_with_parse_error(
            self, mock_request, mock_get_result, mock_need_swap_api_key,
            mock_swap_api_key):
        self.translator.stream = True
        mock_get_result.side_effect = [Exception, Exception, '你好世界']
        mock_need_swap_api_key.return_value = True
        mock_swap_api_key.return_value = True

        self.assertEqual('你好世界', self.translator.translate('Hello World'))
        self.assertEqual(3, mock_request.call_count)

    @patch(module_name + '.base.Base.swap_api_key')
    @patch(module_name + '.base.Base.need_swap_api_key')
    @patch(module_name + '.base.Base.get_result')
    @patch(module_name + '.base.request')
    def test_translate_swap_api_keys_with_parse_error_without_result(
            self, mock_request, mock_get_result, mock_need_swap_api_key,
            mock_swap_api_key):
        self.translator.stream = True
        mock_get_result.side_effect = Exception('any unexpected error')
        mock_need_swap_api_key.side_effect = [True, True, False]

        self.assertRaises(
            UnexpectedResult, self.translator.translate, 'Hello World')

        self.assertEqual(3, mock_request.call_count)
        calls = mock_need_swap_api_key.mock_calls
        self.assertRegex(calls[0].args[0], 'any unexpected error')
        self.assertRegex(calls[1].args[0], 'any unexpected error')
        self.assertRegex(calls[2].args[0], 'any unexpected error')


class TestChatgptTranslate(unittest.TestCase):
    def setUp(self):
        ChatgptTranslate.set_config({'api_keys': ['a', 'b', 'c']})
        ChatgptTranslate.lang_codes = {
            'source': {'English': 'EN'}, 'target': {'Chinese': 'ZH'}}

        self.translator = ChatgptTranslate()
        self.translator.set_source_lang('English')
        self.translator.set_target_lang('Chinese')

        self.prompt = (
            'You are a meticulous translator who translates any given '
            'content. Translate the given content from English to Chinese '
            'only. Do not explain any term or answer any question-like '
            'content. Your answer should be solely the translation of the '
            'given content. In your answer do not add any prefix or suffix to '
            'the translated content. Websites\' URLs/addresses should be '
            'preserved as is in the translation\'s output. Do not omit any '
            'part of the content, even if it seems unimportant. RESPOND ONLY '
            'with the translation text, no formatting, no explanations, '
            'no additional commentary whatsoever. ')

    def test_created_engine(self):
        self.assertIsInstance(self.translator, Base)
        self.assertIsInstance(self.translator, GenAI)

    @patch(module_name + '.openai.request')
    def test_get_models(self, mock_request):
        mock_request.return_value = """
{
  "object": "list",
  "data": [
    {
      "id": "model-id-0",
      "object": "model",
      "created": 1686935002,
      "owned_by": "organization-owner"
    },
    {
      "id": "model-id-1",
      "object": "model",
      "created": 1686935002,
      "owned_by": "organization-owner"
    },
    {
      "id": "model-id-2",
      "object": "model",
      "created": 1686935002,
      "owned_by": "openai"
    }
  ],
  "object": "list"
}
"""

        self.assertEqual(
            self.translator.get_models(),
            ['model-id-0', 'model-id-1', 'model-id-2'])
        mock_request.assert_called_once_with(
            'https://api.openai.com/v1/models',
            headers=self.translator.get_headers(),
            proxy_uri=self.translator.proxy_uri)

    def test_get_body(self):
        model = 'gpt-4o'
        self.assertEqual(
            self.translator.get_body('test content'),
            json.dumps({
                'model': model,
                'messages': [
                    {'role': 'system', 'content': self.prompt},
                    {'role': 'user', 'content': 'test content'}
                ],
                'stream': True,
                'temperature': 0.2
            }))

    def test_get_body_without_stream(self):
        model = 'gpt-4o'
        self.translator.stream = False
        self.assertEqual(
            self.translator.get_body('test content'),
            json.dumps({
                'model': model,
                'messages': [
                    {'role': 'system', 'content': self.prompt},
                    {'role': 'user', 'content': 'test content'}
                ],
                'temperature': 0.2
            }))

    @patch(module_name + '.openai.NovelTranslatorPlugin')
    @patch(module_name + '.base.request')
    def test_translate_stream(self, mock_request, mock_et):
        model = 'gpt-4o'
        data = json.dumps({
            'model': model,
            'messages': [
                {'role': 'system', 'content': self.prompt},
                {'role': 'user', 'content': 'Hello World!'}
            ],
            'stream': True,
            'temperature': 0.2,
        })
        mock_et.__version__ = '1.0.0'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer a',
            'User-Agent': 'Novel-Translator/1.0.0'}
        template = b'data: {"choices":[{"delta":{"content":"%b"}}]}'
        mock_response = Mock()
        mock_response.readline.side_effect = [
            template % i.encode() for i in '你好世界！'] \
            + ['data: [DONE]'.encode()]
        mock_request.return_value = mock_response
        url = 'https://api.openai.com/v1/chat/completions'
        result = self.translator.translate('Hello World!')

        mock_request.assert_called_with(
            url=url, data=data, headers=headers, method='POST', timeout=60.0,
            proxy_uri=None, raw_object=True, keepalive=False)
        self.assertIsInstance(result, GeneratorType)
        self.assertEqual('你好世界！', ''.join(result))

    @patch(module_name + '.base.request')
    def test_translate_normal(self, mock_request):
        mock_request.return_value = \
            '{"choices": [{"message": {"content": "你好世界！"}}]}'
        self.translator.stream = False
        result = self.translator.translate('Hello World!')

        self.assertEqual('你好世界！', result)


class TestChatgptStreamParsing(unittest.TestCase):
    """A stream that ends badly must not hang or pass for a full reply."""

    def setUp(self):
        ChatgptTranslate.set_config({'api_keys': ['a']})
        self.translator = ChatgptTranslate()

    def _stream(self, lines):
        response = Mock()
        response.readline.side_effect = list(lines)
        return self.translator._parse_stream(response)

    def test_stream_ends_without_the_done_marker(self):
        # A provider that just closes the body: readline keeps handing
        # back b'', which used to spin forever.
        chunks = self._stream([
            b'data: {"choices":[{"delta":{"content":"ciao"}}]}',
            b'', b'', b''])
        self.assertEqual('ciao', ''.join(chunks))

    def test_an_error_sent_inside_the_stream_is_raised(self):
        # OpenRouter reports an upstream failure as a regular event; it
        # used to be dropped, leaving a half-written reply behind.
        chunks = self._stream([
            b'data: {"choices":[{"delta":{"content":"ciao"}}]}',
            b'data: {"error":{"message":"upstream timed out"}}',
            b'data: [DONE]'])
        with self.assertRaises(Exception) as caught:
            ''.join(chunks)
        self.assertIn('upstream timed out', str(caught.exception))

    def test_the_finish_reason_and_the_provider_are_recorded(self):
        chunks = self._stream([
            b'data: {"id":"gen-1","provider":"DeepInfra","choices":'
            b'[{"delta":{"content":"ciao"},"finish_reason":null}]}',
            b'data: {"id":"gen-1","provider":"DeepInfra","choices":'
            b'[{"delta":{"content":""},"finish_reason":"length"}]}',
            b'data: [DONE]'])
        self.assertEqual('ciao', ''.join(chunks))
        self.assertEqual('length', self.translator.last_finish_reason)
        self.assertEqual('DeepInfra', self.translator.last_provider)
        self.assertEqual('gen-1', self.translator.last_generation_id)

    def test_the_service_tier_that_served_is_recorded(self):
        chunks = self._stream([
            b'data: {"id":"gen-1","service_tier":"flex","choices":'
            b'[{"delta":{"content":"ciao"},"finish_reason":"stop"}]}',
            b'data: [DONE]'])
        self.assertEqual('ciao', ''.join(chunks))
        self.assertEqual('flex', self.translator.last_service_tier)

    def test_the_usage_at_the_end_of_the_stream_is_recorded(self):
        chunks = self._stream([
            b'data: {"id":"gen-1","choices":[{"delta":{"content":"ciao"},'
            b'"finish_reason":"stop"}]}',
            b'data: {"id":"gen-1","choices":[],"usage":{"prompt_tokens":'
            b'12,"completion_tokens":3,"cost":0.0004}}',
            b'data: [DONE]'])
        self.assertEqual('ciao', ''.join(chunks))
        self.assertEqual(
            {'prompt_tokens': 12, 'completion_tokens': 3, 'cost': 0.0004},
            self.translator.last_usage)

    def test_a_new_stream_forgets_the_last_reply(self):
        self.translator.last_finish_reason = 'length'
        self.translator.last_provider = 'DeepInfra'
        self.translator.last_generation_id = 'gen-1'
        chunks = self._stream([
            b'data: {"choices":[{"delta":{"content":"ciao"}}]}',
            b'data: [DONE]'])
        self.assertEqual('ciao', ''.join(chunks))
        self.assertIsNone(self.translator.last_finish_reason)
        self.assertIsNone(self.translator.last_provider)
        self.assertIsNone(self.translator.last_generation_id)

    def test_data_lines_without_a_space_are_read(self):
        chunks = self._stream([
            b'data:{"choices":[{"delta":{"content":"ciao"}}]}',
            b'data: [DONE]'])
        self.assertEqual('ciao', ''.join(chunks))

    def test_the_payload_may_contain_the_separator(self):
        payload = json.dumps(
            {'choices': [{'delta': {'content': 'the data: point'}}]})
        chunks = self._stream(
            [('data: %s' % payload).encode(), b'data: [DONE]'])
        self.assertEqual('the data: point', ''.join(chunks))


class TestOpenRouterTranslate(unittest.TestCase):
    def setUp(self):
        OpenRouterTranslate.set_config({'api_keys': ['sk-or-v1-a']})
        OpenRouterTranslate.lang_codes = {
            'source': {'English': 'EN'}, 'target': {'Italian': 'IT'}}

        self.translator = OpenRouterTranslate()
        self.translator.set_source_lang('English')
        self.translator.set_target_lang('Italian')

    def reconfigure(self, **preferences):
        """Rebuild the engine with the given engine preferences."""
        preferences.setdefault('api_keys', ['sk-or-v1-a'])
        OpenRouterTranslate.set_config(preferences)
        translator = OpenRouterTranslate()
        translator.set_source_lang('English')
        translator.set_target_lang('Italian')
        return translator

    def test_created_engine(self):
        self.assertIsInstance(self.translator, GenAI)
        self.assertIsInstance(self.translator, ChatgptTranslate)

    @patch(module_name + '.openrouter.request')
    def test_get_models(self, mock_request):
        mock_request.return_value = json.dumps({'data': [
            {'id': 'openai/gpt-5.1'},
            {'id': 'deepseek/deepseek-v4-flash'}]})

        self.assertEqual(
            ['deepseek/deepseek-v4-flash', 'openai/gpt-5.1'],
            self.translator.get_models())
        mock_request.assert_called_once_with(
            'https://openrouter.ai/api/v1/models',
            headers=self.translator.get_headers(),
            proxy_uri=self.translator.proxy_uri)

    @patch(module_name + '.openrouter.request')
    def test_get_models_records_the_limits(self, mock_request):
        mock_request.return_value = json.dumps({'data': [
            {
                'id': 'openai/gpt-5.1',
                'context_length': 400000,
                'top_provider': {
                    'context_length': 400000,
                    'max_completion_tokens': 128000,
                },
                'supported_parameters': ['response_format', 'seed'],
            },
            {
                # No top_provider figures at all: the model is listed,
                # and what is not stated stays unstated.
                'id': 'someone/mystery-model',
                'context_length': 8192,
            },
        ]})

        self.assertEqual(
            ['openai/gpt-5.1', 'someone/mystery-model'],
            self.translator.get_models())
        self.assertEqual(
            {'context_length': 400000, 'max_output_tokens': 128000,
             'structured_output': True,
             'supported_parameters': ['response_format', 'seed']},
            OpenRouterTranslate.get_model_limits('openai/gpt-5.1'))
        self.assertEqual(
            {'context_length': 8192, 'max_output_tokens': None,
             'structured_output': False, 'supported_parameters': []},
            OpenRouterTranslate.get_model_limits('someone/mystery-model'))
        self.assertEqual(
            {}, OpenRouterTranslate.get_model_limits('nobody/nothing'))

    def test_parameters_the_model_rejects_are_not_sent(self):
        """A fifth of the OpenRouter catalogue takes no temperature."""
        translator = self.reconfigure(
            model='vendor/no-knobs',
            model_supported_parameters=['max_tokens', 'response_format'],
            seed=42, top_k=20)

        body = json.loads(translator.get_body('test content'))

        self.assertNotIn('temperature', body)
        self.assertNotIn('seed', body)
        self.assertNotIn('top_k', body)
        self.assertNotIn('reasoning', body)
        # What the request is made of is never touched.
        self.assertEqual('vendor/no-knobs', body['model'])
        self.assertIn('messages', body)

    def test_parameters_the_model_accepts_are_sent(self):
        translator = self.reconfigure(
            model='vendor/every-knob',
            model_supported_parameters=[
                'temperature', 'seed', 'top_k', 'reasoning'],
            seed=42, top_k=20)

        body = json.loads(translator.get_body('test content'))

        self.assertEqual(0.2, body['temperature'])
        self.assertEqual(42, body['seed'])
        self.assertEqual(20, body['top_k'])
        self.assertEqual({'enabled': False}, body['reasoning'])

    def test_nothing_is_filtered_when_the_model_is_unknown(self):
        translator = self.reconfigure(model='vendor/unlisted', seed=42)

        body = json.loads(translator.get_body('test content'))

        self.assertEqual(0.2, body['temperature'])
        self.assertEqual(42, body['seed'])

    def test_the_service_tier_is_left_out_by_default(self):
        body = json.loads(self.translator.get_body('test content'))
        self.assertNotIn('service_tier', body)

    def test_a_service_tier_is_sent_and_flex_waits_longer(self):
        translator = self.reconfigure(service_tier='flex')
        body = json.loads(translator.get_body('test content'))
        self.assertEqual('flex', body['service_tier'])
        self.assertGreaterEqual(translator.request_timeout, 900)
        structured = json.loads(translator.get_body_for_structured(
            'test content', {'type': 'object'}))
        self.assertEqual('flex', structured['service_tier'])

    def test_priority_keeps_the_timeout(self):
        translator = self.reconfigure(
            service_tier='priority', request_timeout=120)
        body = json.loads(translator.get_body('test content'))
        self.assertEqual('priority', body['service_tier'])
        self.assertEqual(120, translator.request_timeout)

    def test_an_unknown_service_tier_is_the_default(self):
        translator = self.reconfigure()
        translator.set_service_tier('turbo')
        self.assertEqual('default', translator.service_tier)
        body = json.loads(translator.get_body('test content'))
        self.assertNotIn('service_tier', body)

    def test_extra_body_forces_a_filtered_parameter(self):
        translator = self.reconfigure(
            model='vendor/no-knobs',
            model_supported_parameters=['max_tokens'],
            extra_body='{"temperature": 0.9}')

        body = json.loads(translator.get_body('test content'))

        self.assertEqual(0.9, body['temperature'])

    def test_structured_output_follows_the_model(self):
        self.assertIsNone(self.reconfigure(
            model='vendor/no-json',
            model_supported_parameters=['temperature'],
        ).structured_output_mode)
        self.assertEqual('schema', self.reconfigure(
            model='vendor/json',
            model_supported_parameters=['structured_outputs'],
        ).structured_output_mode)
        # An unknown model keeps what the gateway itself advertises.
        self.assertEqual('schema', self.reconfigure(
            model='vendor/unlisted').structured_output_mode)

    def test_model_max_output_tokens_is_a_preference(self):
        translator = self.reconfigure(model_max_output_tokens=64000)
        self.assertEqual(64000, translator.model_max_output_tokens)
        self.assertEqual(0, self.reconfigure().model_max_output_tokens)

    def test_get_headers(self):
        translator = self.reconfigure(
            app_referer='https://example.com', app_title='My Books',
            extra_headers='{"X-Session-Id": "calibre-translation"}')
        headers = translator.get_headers()

        self.assertEqual('https://example.com', headers['HTTP-Referer'])
        self.assertEqual('My Books', headers['X-Title'])
        self.assertEqual('calibre-translation', headers['X-Session-Id'])
        self.assertEqual('Bearer sk-or-v1-a', headers['Authorization'])

    def test_get_body_default(self):
        body = json.loads(self.translator.get_body('test content'))

        self.assertEqual('deepseek/deepseek-v4-flash', body['model'])
        self.assertEqual(0.2, body['temperature'])
        # Reasoning is off by default and routing asks for the cheapest
        # endpoint that honours every parameter we send.
        self.assertEqual({'enabled': False}, body['reasoning'])
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual(
            {'sort': 'price', 'require_parameters': True},
            body['provider'])
        # Neutral values are omitted so the provider keeps its own default.
        for key in ('top_k', 'min_p', 'top_a', 'seed', 'max_tokens',
                    'frequency_penalty', 'presence_penalty',
                    'repetition_penalty'):
            self.assertNotIn(key, body)

    def test_get_body_with_preferences(self):
        translator = self.reconfigure(
            model='deepseek/deepseek-v4-flash-0731', temperature=0.1,
            top_k=40, min_p=0.05, top_a=0.1, seed=42, max_tokens=8192,
            frequency_penalty=0.2, presence_penalty=0.1,
            repetition_penalty=1.05, reasoning_effort='low',
            reasoning_exclude=False, provider_only='baidu/fp8, deepinfra',
            provider_ignore='novita', provider_quantizations='fp8',
            provider_sort='throughput', provider_allow_fallbacks=False,
            provider_require_parameters=True,
            provider_data_collection='deny', provider_zdr=True,
            extra_body='{"cache_control": {"type": "ephemeral"}}')
        body = json.loads(translator.get_body('test content'))

        self.assertEqual('deepseek/deepseek-v4-flash-0731', body['model'])
        self.assertEqual(0.1, body['temperature'])
        self.assertEqual(40, body['top_k'])
        self.assertEqual(0.05, body['min_p'])
        self.assertEqual(0.1, body['top_a'])
        self.assertEqual(42, body['seed'])
        self.assertEqual(8192, body['max_tokens'])
        self.assertEqual(0.2, body['frequency_penalty'])
        self.assertEqual(0.1, body['presence_penalty'])
        self.assertEqual(1.05, body['repetition_penalty'])
        self.assertEqual({'effort': 'low'}, body['reasoning'])
        self.assertEqual({
            'only': ['baidu/fp8', 'deepinfra'],
            'ignore': ['novita'],
            'quantizations': ['fp8'],
            'sort': 'throughput',
            'allow_fallbacks': False,
            'require_parameters': True,
            'data_collection': 'deny',
            'zdr': True}, body['provider'])
        self.assertEqual({'type': 'ephemeral'}, body['cache_control'])

    def test_get_body_reasoning_omitted(self):
        body = json.loads(
            self.reconfigure(reasoning_effort='default').get_body('t'))
        self.assertNotIn('reasoning', body)

    def test_get_body_reasoning_disabled(self):
        # 'none' is not an effort level of the unified API: it has to be
        # sent as the documented off switch, and `exclude` would be
        # meaningless next to it.
        body = json.loads(
            self.reconfigure(reasoning_effort='none').get_body('t'))
        self.assertEqual({'enabled': False}, body['reasoning'])

    def test_get_body_reasoning_disabled_ignores_the_budget(self):
        body = json.loads(self.reconfigure(
            reasoning_effort='none', reasoning_max_tokens=2048).get_body('t'))
        self.assertEqual({'enabled': False}, body['reasoning'])

    def test_get_body_reasoning_budget_supersedes_effort(self):
        body = json.loads(self.reconfigure(
            reasoning_effort='high', reasoning_max_tokens=2048).get_body('t'))
        self.assertEqual(
            {'max_tokens': 2048, 'exclude': True}, body['reasoning'])

    def test_get_body_for_structured_keeps_reasoning(self):
        schema = {'type': 'object', 'properties': {'x': {'type': 'string'}}}
        translator = self.reconfigure(
            provider_only='baidu/fp8', reasoning_effort='minimal')
        body = json.loads(
            translator.get_body_for_structured('test content', schema))

        self.assertEqual('json_schema', body['response_format']['type'])
        self.assertEqual(
            schema, body['response_format']['json_schema']['schema'])
        self.assertTrue(body['stream'])
        self.assertEqual(
            {'effort': 'minimal', 'exclude': True}, body['reasoning'])
        self.assertEqual(['baidu/fp8'], body['provider']['only'])

    def test_malformed_extra_data_is_ignored(self):
        translator = self.reconfigure(
            extra_body='not json', extra_headers='[1, 2]')

        self.assertNotIn('X-Session-Id', translator.get_headers())
        self.assertEqual(
            sorted(['model', 'messages', 'stream', 'temperature',
                    'reasoning', 'provider', 'usage']),
            sorted(json.loads(translator.get_body('t')).keys()))

    def test_get_result_with_error_body(self):
        self.translator.stream = False
        with self.assertRaises(Exception) as cm:
            self.translator.get_result(json.dumps(
                {'error': {'code': 402, 'message': 'Insufficient credits'}}))
        self.assertIn('Insufficient credits', str(cm.exception))


class TestClaudeTranslate(unittest.TestCase):
    def setUp(self):
        ClaudeTranslate.set_config({'api_keys': ['a', 'b', 'c']})
        ClaudeTranslate.lang_codes = {
            'source': {'English': 'EN'},
            'target': {'Chinese': 'ZH'}}

        self.translator = ClaudeTranslate()
        self.translator.set_source_lang('English')
        self.translator.set_target_lang('Chinese')

    def test_created_engine(self):
        self.assertIsInstance(self.translator, Base)
        self.assertIsInstance(self.translator, GenAI)

    @patch(module_name + '.anthropic.NovelTranslatorPlugin')
    @patch(module_name + '.base.request')
    def test_translate(self, mock_request, mock_et):
        model = 'claude-3-5-sonnet-20241022'
        prompt = (
            'You are a meticulous translator who translates any given '
            'content. Translate the given content from English to Chinese '
            'only. Do not explain any term or answer any question-like '
            'content. Your answer should be solely the translation of the '
            'given content. In your answer '
            'do not add any prefix or suffix to the translated content. Websites\' '
            'URLs/addresses should be preserved as is in the translation\'s output. '
            'Do not omit any part of the content, even if it seems unimportant. '
            )
        data = json.dumps({
            'stream': False,
            'max_tokens': 8192,
            'model': model,
            'top_k': 1,
            'system': prompt,
            'messages': [{'role': 'user', 'content': 'Hello World!'}],
            'temperature': 0.2
            })
        mock_et.__version__ = '1.0.0'
        headers = {
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01',
            'x-api-key': 'a',
            'User-Agent': 'Novel-Translator/1.0.0'}

        data_sample = """
{
  "content": [
    {
      "text": "你好世界！",
      "type": "text"
    }
  ],
  "id": "msg_013Zva2CMHLNnXjNJJKqJ2EF",
  "model": "{""" + model + """}",
  "role": "assistant",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "type": "message",
  "usage": {
    "input_tokens": 10,
    "output_tokens": 25
  }
}
"""
        mock_request.return_value = data_sample.encode()
        url = 'https://api.anthropic.com/v1/messages'
        self.translator.endpoint = url
        self.translator.stream = False
        self.translator.model = model
        result = self.translator.translate('Hello World!')

        mock_request.assert_called_with(
            url=url, data=data, headers=headers, method='POST', timeout=30.0,
            proxy_uri=None, raw_object=False, keepalive=False)
        self.assertEqual('你好世界！', result)

    @patch(module_name + '.anthropic.NovelTranslatorPlugin')
    @patch(module_name + '.base.request')
    def test_translate_stream(self, mock_request, mock_et):
        model = 'claude-3-5-sonnet-20241022'
        prompt = (
            'You are a meticulous translator who translates any given '
            'content. Translate the given content from English to Chinese '
            'only. Do not explain any term or answer any question-like '
            'content. Your answer should be solely the translation of the '
            'given content. In your answer do not add any prefix or suffix to '
            'the translated content. Websites\' URLs/addresses should be '
            'preserved as is in the translation\'s output. Do not omit any '
            'part of the content, even if it seems unimportant. ')
        data = json.dumps({
            'stream': True,
            'max_tokens': 8192,
            'model': model,
            'top_k': 1,
            'system': prompt,
            'messages': [{'role': 'user', 'content': 'Hello World!'}],
            'temperature': 0.2
            })
        mock_et.__version__ = '1.0.0'
        headers = {
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01',
            'x-api-key': 'a',
            'User-Agent': 'Novel-Translator/1.0.0'}

        data_sample = """
event: message_start
data: {"type":"message_start","message":{}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{}}

event: ping
data: {"type": "ping"}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"text":"你"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"text":"好"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"text":"世"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"text":"界"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"text":"！"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{}}

event: message_stop
data: {"type":"message_stop"}
"""
        mock_response = Mock()
        # With the line endings kept, as readline() hands them over: a
        # blank line of the stream is b'\n', and b'' means the body is
        # over.
        mock_response.readline.side_effect = \
            data_sample.encode().splitlines(keepends=True)
        mock_request.return_value = mock_response
        url = 'https://api.anthropic.com/v1/messages'
        self.translator.endpoint = url
        self.translator.model = model
        result = self.translator.translate('Hello World!')

        mock_request.assert_called_with(
            url=url, data=data, headers=headers, method='POST', timeout=30.0,
            proxy_uri=None, raw_object=True, keepalive=False)
        self.assertIsInstance(result, GeneratorType)
        self.assertEqual('你好世界！', ''.join(result))


class TestClaudeReplyLimit(unittest.TestCase):
    def setUp(self):
        ClaudeTranslate.set_config({'api_keys': ['a']})
        self.translator = ClaudeTranslate()

    def test_default_follows_the_model(self):
        self.assertEqual(
            8192, ClaudeTranslate.default_reply_limit('claude-3-5-haiku'))
        self.assertEqual(
            64000, ClaudeTranslate.default_reply_limit('claude-3-7-sonnet'))
        self.assertEqual(
            32000, ClaudeTranslate.default_reply_limit('claude-sonnet-4-5'))
        self.assertEqual(32000, ClaudeTranslate.default_reply_limit(None))

    def test_setting_overrides_the_default(self):
        ClaudeTranslate.set_config({'api_keys': ['a'], 'max_tokens': 2048})
        translator = ClaudeTranslate()
        translator.model = 'claude-sonnet-4-5'
        translator.set_source_lang('English')
        translator.set_target_lang('Italian')
        self.assertEqual(2048, translator.reply_limit())
        self.assertEqual(2048, translator.model_max_output_tokens)
        self.assertEqual(
            2048, json.loads(translator.get_body('x'))['max_tokens'])

    def test_pipeline_can_lower_it_for_one_call(self):
        # What lib/novel.py does around a summary call.
        self.translator.model = 'claude-sonnet-4-5'
        self.translator.set_source_lang('English')
        self.translator.set_target_lang('Italian')
        self.translator.max_tokens = 4000
        self.assertEqual(
            4000, json.loads(self.translator.get_body('x'))['max_tokens'])
        self.translator.max_tokens = 0
        self.assertEqual(
            32000, json.loads(self.translator.get_body('x'))['max_tokens'])


class TestClaudeStreamParsing(unittest.TestCase):
    def setUp(self):
        ClaudeTranslate.set_config({'api_keys': ['a']})
        self.translator = ClaudeTranslate()

    def _stream(self, lines):
        response = Mock()
        response.readline.side_effect = list(lines)
        return self.translator._parse_stream(response)

    def test_stream_ends_without_message_stop(self):
        # A provider that just closes the body: readline hands back b''
        # from then on, which used to be read forever.
        chunks = self._stream([
            b'data: {"type":"content_block_delta","delta":{"text":"ciao"}}',
            b'', b'', b''])
        self.assertEqual('ciao', ''.join(chunks))

    def test_incomplete_read_ends_the_stream(self):
        chunks = self._stream([
            b'data: {"type":"content_block_delta","delta":{"text":"ciao"}}',
            IncompleteRead(b'')])
        self.assertEqual('ciao', ''.join(chunks))

    def test_payload_containing_the_prefix_survives(self):
        chunks = self._stream([
            b'data: {"type":"content_block_delta",'
            b'"delta":{"text":"the data: point"}}',
            b'data: {"type":"message_stop"}'])
        self.assertEqual('the data: point', ''.join(chunks))

    def test_prefix_without_a_space(self):
        chunks = self._stream([
            b'data:{"type":"content_block_delta","delta":{"text":"x"}}',
            b'data:{"type":"message_stop"}'])
        self.assertEqual('x', ''.join(chunks))


class TestOpenAIProviders(unittest.TestCase):
    """One engine, many servers: the preset fills in what differs."""

    def engine(self, **preferences):
        preferences.setdefault('api_keys', ['k'])
        ChatgptTranslate.set_config(preferences)
        translator = ChatgptTranslate()
        translator.set_source_lang('English')
        translator.set_target_lang('Italian')
        return translator

    def test_openai_is_the_default(self):
        translator = self.engine()
        self.assertEqual('openai', translator.provider)
        self.assertEqual(
            'https://api.openai.com/v1/chat/completions', translator.endpoint)
        self.assertEqual('gpt-4o', translator.model)
        self.assertEqual(
            'Bearer k', translator.get_headers()['Authorization'])

    def test_preset_fills_endpoint_model_and_temperature(self):
        translator = self.engine(provider='deepseek')
        self.assertEqual(
            'https://api.deepseek.com/v1/chat/completions',
            translator.endpoint)
        self.assertEqual('deepseek-chat', translator.model)
        self.assertEqual(1.3, translator.temperature)
        self.assertEqual(
            'https://api.deepseek.com/v1/models',
            translator.get_model_endpoint())

    def test_overrides_win_over_the_preset(self):
        translator = self.engine(
            provider='groq', endpoint='https://proxy.local/v1/chat/completions',
            model='mixtral', temperature=0.2)
        self.assertEqual(
            'https://proxy.local/v1/chat/completions', translator.endpoint)
        self.assertEqual('mixtral', translator.model)
        self.assertEqual(0.2, translator.temperature)

    def test_unknown_provider_falls_back_to_openai(self):
        translator = self.engine(provider='nonsense')
        self.assertEqual(
            'https://api.openai.com/v1/chat/completions', translator.endpoint)

    def test_local_server_takes_no_key(self):
        translator = self.engine(provider='ollama', api_keys=[])
        self.assertFalse(ChatgptTranslate.needs_api_key({'provider': 'ollama'}))
        self.assertIsNone(translator.api_key)
        self.assertNotIn('Authorization', translator.get_headers())
        self.assertEqual('http://localhost:11434/v1/chat/completions',
                         translator.endpoint)

    def test_azure_names_the_deployment_in_the_url(self):
        translator = self.engine(
            provider='azure',
            endpoint='https://r.openai.azure.com/openai/deployments/d/'
                     'chat/completions?api-version=2024-10-21')
        headers = translator.get_headers()
        self.assertEqual('k', headers['api-key'])
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('model', json.loads(translator.get_body('x')))
        self.assertNotIn(
            'model', json.loads(translator.get_body_for_structured('x')))
        # Nothing to list: the listing is skipped, not requested.
        with patch(module_name + '.openai.request') as mock_request:
            self.assertEqual([], translator.get_models())
            mock_request.assert_not_called()

    def test_key_hint_follows_the_preset(self):
        self.assertEqual('gsk_...', ChatgptTranslate.key_hint(
            {'provider': 'groq'}))
        self.assertEqual(
            ChatgptTranslate.api_key_hint,
            ChatgptTranslate.key_hint({'provider': 'mistral'}))

    def test_openrouter_carries_no_presets(self):
        OpenRouterTranslate.set_config({
            'api_keys': ['k'], 'provider': 'azure',
            # Left behind by an older configuration: not a setting any
            # more, so not read.
            'endpoint': 'https://elsewhere.example/v1/chat/completions'})
        translator = OpenRouterTranslate()
        self.assertIsNone(OpenRouterTranslate.preset_for())
        self.assertEqual(
            'https://openrouter.ai/api/v1/chat/completions',
            translator.endpoint)
        self.assertTrue(OpenRouterTranslate.needs_api_key())
        self.assertEqual(
            'Bearer k', translator.get_headers()['Authorization'])


class TestOpenRouterRelax(unittest.TestCase):
    def test_relaxing_drops_require_parameters_once(self):
        OpenRouterTranslate.set_config({'api_keys': ['k']})
        translator = OpenRouterTranslate()
        translator.set_source_lang('English')
        translator.set_target_lang('Italian')
        self.assertTrue(translator.relax_parameters())
        self.assertFalse(translator.relax_parameters())
        body = json.loads(translator.get_body('x'))
        self.assertNotIn('require_parameters', body.get('provider', {}))


class TestModelEndpoint(unittest.TestCase):
    """The listing URL keeps the path prefix of the chat endpoint."""

    def _endpoint(self, chat):
        ChatgptTranslate.set_config({'api_keys': ['a'], 'endpoint': chat})
        return ChatgptTranslate().get_model_endpoint()

    def test_openai(self):
        self.assertEqual(
            'https://api.openai.com/v1/models',
            self._endpoint('https://api.openai.com/v1/chat/completions'))

    def test_gateway_with_a_path_prefix(self):
        self.assertEqual(
            'https://api.groq.com/openai/v1/models',
            self._endpoint('https://api.groq.com/openai/v1/chat/completions'))
        self.assertEqual(
            'http://box/ollama/v1/models',
            self._endpoint('http://box/ollama/v1/chat/completions/'))

    def test_endpoint_without_the_standard_suffix(self):
        self.assertEqual(
            'https://example.org/v1/models',
            self._endpoint('https://example.org/custom'))


class TestOpenRouterHeaders(unittest.TestCase):
    def test_non_latin1_characters_are_replaced(self):
        OpenRouterTranslate.set_config({
            'api_keys': ['a'], 'app_title': '翻訳 Novel',
            'extra_headers': '{"X-Note": "caf\u00e9 \u65e5"}'})
        headers = OpenRouterTranslate().get_headers()
        for value in headers.values():
            value.encode('latin-1')  # must not raise
        self.assertEqual('?? Novel', headers['X-Title'])
        self.assertEqual('caf\u00e9 ?', headers['X-Note'])


class TestKeyErrorMatching(unittest.TestCase):
    @patch(module_name + '.base.request')
    def test_a_traceback_line_number_is_not_an_auth_error(
            self, mock_request):
        # '401' used to be looked for in the traceback as well, where a
        # "line 401" of any frame passed for an authentication failure.
        ChatgptTranslate.set_config({'api_keys': ['a', 'b']})
        translator = ChatgptTranslate()
        translator.stream = False
        translator.set_source_lang('English')
        translator.set_target_lang('Italian')
        mock_request.side_effect = Exception('connection reset')
        with patch(module_name + '.base.traceback_error',
                   return_value='File "x.py", line 401, in translate'):
            with self.assertRaises(Exception):
                translator.translate('x')
        # One request, no key swapped: the spare key is still there.
        self.assertEqual(1, mock_request.call_count)
        self.assertEqual(['b'], translator.api_keys)


class TestOpenRouterProviderExclusion(unittest.TestCase):
    LISTING = json.dumps({'data': {'endpoints': [
        {'provider_name': 'OpenInference', 'tag': 'open-inference/fp8'},
        {'provider_name': 'DeepInfra', 'tag': 'deepinfra/fp8'}]}})

    def setUp(self):
        OpenRouterTranslate._provider_slugs.clear()
        OpenRouterTranslate.set_config(
            {'api_keys': ['sk-or-v1-a'], 'model': 'vendor/model'})
        OpenRouterTranslate.lang_codes = {
            'source': {'English': 'EN'}, 'target': {'Italian': 'IT'}}
        self.translator = OpenRouterTranslate()
        self.translator.set_source_lang('English')
        self.translator.set_target_lang('Italian')

    @patch(module_name + '.openrouter.request')
    def test_the_display_name_is_resolved_to_the_slug(self, mock_request):
        mock_request.return_value = json.dumps({'data': {'endpoints': [
            {'provider_name': 'OpenInference', 'tag': 'open-inference/fp8'},
            {'provider_name': 'DeepInfra', 'tag': 'deepinfra/fp8'}]}})
        self.assertTrue(self.translator.exclude_provider('OpenInference'))
        self.assertEqual('open-inference', self.translator.provider_ignore)
        body = json.loads(self.translator.get_body('x'))
        self.assertEqual(['open-inference'], body['provider']['ignore'])
        # Already excluded: nothing to add.
        self.assertFalse(self.translator.exclude_provider('OpenInference'))
        self.assertIn(
            '/models/vendor/model/endpoints', mock_request.call_args[0][0])

    @patch(module_name + '.openrouter.request')
    def test_the_name_itself_when_the_listing_fails(self, mock_request):
        mock_request.side_effect = Exception('offline')
        self.translator.provider_ignore = 'novita'
        self.assertTrue(self.translator.exclude_provider('Some Host'))
        self.assertEqual('novita, some-host', self.translator.provider_ignore)

    def test_the_setting_on_disk_is_not_touched(self):
        with patch(module_name + '.openrouter.request',
                   side_effect=Exception('offline')):
            self.translator.exclude_provider('Flaky')
        self.assertEqual('', OpenRouterTranslate.config.get(
            'provider_ignore', ''))

    @patch(module_name + '.openrouter.request')
    def test_the_only_provider_allowed_is_kept(self, mock_request):
        """Ignoring the one provider the request is pinned to left
        OpenRouter nothing to route to, and every request of the rest
        of the run failed with "All providers have been ignored"."""
        mock_request.return_value = self.LISTING
        self.translator.provider_only = 'deepinfra/fp8'
        with self.assertRaises(ValueError) as caught:
            self.translator.exclude_provider('DeepInfra')
        self.assertIn('provider_only', str(caught.exception))
        self.assertEqual('deepinfra/fp8', self.translator.provider_only)
        self.assertEqual('', self.translator.provider_ignore)
        body = json.loads(self.translator.get_body('x'))
        self.assertEqual(['deepinfra/fp8'], body['provider']['only'])
        self.assertNotIn('ignore', body['provider'])

    @patch(module_name + '.openrouter.request')
    def test_a_pinned_provider_among_others_is_taken_off_the_list(
            self, mock_request):
        mock_request.return_value = self.LISTING
        self.translator.provider_only = 'deepinfra/fp8, open-inference'
        self.assertTrue(self.translator.exclude_provider('DeepInfra'))
        self.assertEqual('open-inference', self.translator.provider_only)
        self.assertEqual('deepinfra', self.translator.provider_ignore)
        body = json.loads(self.translator.get_body('x'))
        self.assertEqual(['open-inference'], body['provider']['only'])
        self.assertEqual(['deepinfra'], body['provider']['ignore'])

    @patch(module_name + '.openrouter.request')
    def test_the_slug_is_looked_up_once_per_model(self, mock_request):
        """The copies of the engine reading a chapter's chunks together
        each exclude the provider, under the lock that holds the other
        chunks up: one listing serves them all."""
        import copy
        mock_request.return_value = self.LISTING
        copies = [copy.copy(self.translator) for _ in range(3)]
        for engine in [self.translator] + copies:
            self.assertTrue(engine.exclude_provider('DeepInfra'))
            self.assertEqual('deepinfra', engine.provider_ignore)
        self.assertEqual(1, mock_request.call_count)

    def test_usage_accounting_is_asked_for(self):
        body = json.loads(self.translator.get_body('x'))
        self.assertEqual({'include': True}, body['usage'])
        self.translator.usage_accounting = False
        body = json.loads(self.translator.get_body('x'))
        self.assertNotIn('usage', body)


class TestAbortReachesClones(unittest.TestCase):
    def test_clones_are_aborted_too(self):
        ChatgptTranslate.set_config({'api_keys': ['a']})
        engine = ChatgptTranslate()
        clone = ChatgptTranslate()
        clone.inflight = Mock()
        engine.inflight = Mock()
        engine.clones = [clone]
        engine.abort()
        clone.inflight.close.assert_called_once()
        engine.inflight.close.assert_called_once()

    def test_a_copy_that_shares_the_list_is_closed_once(self):
        """A copy made by ``copy.copy`` once the list of copies existed
        carries the same list, itself included. Asking each copy to
        abort its copies in turn went round that list without end,
        swallowing a RecursionError at every turn, with the window
        frozen behind it."""
        import copy
        ChatgptTranslate.set_config({'api_keys': ['a']})
        engine = ChatgptTranslate()
        engine.clones = []
        clone = copy.copy(engine)
        engine.clones.append(clone)
        clone.inflight = Mock()
        engine.inflight = Mock()
        engine.abort()
        clone.inflight.close.assert_called_once()
        engine.inflight.close.assert_called_once()
        # A copy told to abort on its own closes its own response and
        # does not go through the list it happens to carry.
        clone.inflight.reset_mock()
        engine.inflight.reset_mock()
        clone.abort()
        clone.inflight.close.assert_called_once()
        engine.inflight.close.assert_not_called()


class TestOpenRouterFlex(unittest.TestCase):
    """Whether a model has flex is read off its endpoint listing, which
    tags every endpoint with the tier it serves."""

    def setUp(self):
        OpenRouterTranslate._flex_models.clear()
        self.addCleanup(OpenRouterTranslate._flex_models.clear)
        OpenRouterTranslate.set_config({'api_keys': ['sk-or-v1-a']})
        self.translator = OpenRouterTranslate()

    @patch(module_name + '.openrouter.request')
    def test_a_flex_endpoint_is_found_by_its_tag(self, mock_request):
        mock_request.return_value = json.dumps({'data': {'endpoints': [
            {'provider_name': 'OpenAI', 'tag': 'openai'},
            {'provider_name': 'Google', 'tag': 'google-vertex/global/flex'},
        ]}})
        self.assertTrue(self.translator.model_has_flex('vendor/model'))
        self.assertIn('/models/vendor/model/endpoints',
                      mock_request.call_args[0][0])
        # Asked once per model.
        self.assertTrue(self.translator.model_has_flex('vendor/model'))
        self.assertEqual(1, mock_request.call_count)

    @patch(module_name + '.openrouter.request')
    def test_no_flex_endpoint(self, mock_request):
        mock_request.return_value = json.dumps({'data': {'endpoints': [
            {'provider_name': 'DeepInfra', 'tag': 'deepinfra/fp8'},
            {'provider_name': 'Flexcorp', 'tag': 'flexcorp'}]}})
        self.assertIs(False, self.translator.model_has_flex('vendor/model'))

    @patch(module_name + '.openrouter.request')
    def test_unknown_when_the_listing_fails(self, mock_request):
        mock_request.side_effect = Exception('offline')
        self.assertIsNone(self.translator.model_has_flex('vendor/model'))
        # Not remembered: the next choice of the model asks again.
        self.assertNotIn('vendor/model', OpenRouterTranslate._flex_models)
        self.assertIsNone(self.translator.model_has_flex(''))

    def test_on_by_default_and_saved_with_the_engine(self):
        self.assertTrue(OpenRouterTranslate.flex_when_available)
        self.assertIn('flex_when_available',
                      OpenRouterTranslate.preference_keys)

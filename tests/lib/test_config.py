import unittest
from typing import Any
from unittest.mock import call, Mock

from ...lib.config import Configuration, get_config


class TestFunction(unittest.TestCase):
    def setUp(self):
        self.config = get_config()

    def test_default(self):
        defaults: dict[str, Any] = {
            'toolbar_placed': False,
            'to_library': True,
            'output_path': None,
            'translate_engine': 'OpenRouter',
            'engine_preferences': {},
            'proxy_enabled': False,
            'proxy_type': 'http',
            'proxy_setting': {},
            'cache_enabled': True,
            'cache_path': None,
            'log_translation': True,
            'show_notification': True,
            'translation_position': 'only',
            'column_gap': {
                '_type': 'percentage',
                'percentage': 10,
                'space_count': 6,
            },
            'original_color': None,
            'translation_color': None,
            'priority_rules': [],
            'rule_mode': 'normal',
            'filter_scope': 'text',
            'filter_rules': [],
            'ignore_rules': [],
            'reserve_rules': [],
            'ebook_metadata': {'lang_code': True},
            'openrouter_advanced_parameters': False,
            'novel_chunk_tokens': 16000,
            'novel_max_paragraphs_per_chunk': 75,
            'novel_structured_output': 'auto',
            'novel_overlap_paragraphs': 5,
            'novel_context_tokens': 4000,
            'novel_summary_tokens': 600,
            'novel_glossary_max_entries': 500,
            'novel_glossary_relevant_only': True,
            'novel_glossary_prompt_max_entries': 150,
            'novel_context_max_tokens': 4000,
            'novel_summary_max_chars': 0,
            'novel_summary_input_max_chars': 40000,
            'novel_combined_context_call': True,
            'novel_context_narrative_only': True,
            'novel_skip_context_last_chapter': True,
            'novel_context_reasoning': False,
            'novel_min_chars_for_context': 300,
            'novel_reuse_translated_paragraphs': True,
            'novel_on_missing_paragraphs': 'stop',
            'novel_rate_limit_max_wait': 600,
            'novel_reply_max_tokens': 16384,
            'novel_output_aware_chunking': True,
            'novel_prompt_cache': True,
            'novel_chapter_source': 'toc_level_1',
            'novel_front_matter_min_chars': 100,
            'novel_author_style': 'model',
            'novel_dialogue_convention': 'auto',
            'novel_dialogue_rules': None,
            'novel_translation_prompt': None,
        }

        self.assertEqual(defaults, self.config.preferences.defaults)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.config = Configuration()

    def test_get(self):
        self.config.preferences = {'a': 1, 'b': {'b1': {'b2': 2}}}

        self.assertEqual(None, self.config.get(None))
        self.assertEqual(1, self.config.get(None, 1))
        self.assertEqual(None, self.config.get('a.fake'))
        self.assertEqual(1, self.config.get('a.fake', 1))
        self.assertEqual({'b2': 2}, self.config.get('b.b1'))
        self.assertEqual(2, self.config.get('b.b1.b2'))
        self.assertEqual('only', self.config.get('translation_position'))

    def test_set(self):
        self.config.preferences = {
            'a': 1,
            'b': {
                'b1': 2,
                'b2': True,
                'b3': False,
            }
        }

        self.config.set('a.a1', 1)
        self.assertEqual(1, self.config.preferences['a']['a1'])

        self.config.set('b', 2)
        self.assertEqual(2, self.config.preferences['b'])

        self.config.set('c', 3)
        self.assertEqual(3, self.config.preferences['c'])

        self.config.set('d.d1.d11.d111', 4)
        self.assertEqual(4, self.config.preferences['d']['d1']['d11']['d111'])

        self.assertEqual(self.config.preferences, {
            'a': {
                'a1': 1
            },
            'b': 2,
            'c': 3,
            'd': {
                'd1': {
                    'd11': {
                        'd111': 4
                    }
                }
            }
        })

    def test_update(self):
        self.config.preferences = Mock()
        self.config.update(a=6, b=4)
        self.config.preferences.update.assert_called_with(a=6, b=4)

    def test_delete(self):
        self.config.preferences = {'a': 1, 'b': 2}
        self.assertTrue(self.config.delete('a'))
        self.assertFalse(self.config.delete('c'))
        self.assertEqual({'b': 2}, self.config.preferences)

    def test_commit(self):
        self.config.preferences = Mock()
        self.config.commit()
        self.config.preferences.commit.assert_called_once()

    def test_save(self):
        self.config.preferences = Mock()
        self.config.save(b=2)
        self.assertEqual(
            self.config.preferences.mock_calls,
            [call.update(b=2), call.commit()])

import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ...lib import cache as cache_module
from ...lib.cache import TranslationCache, Paragraph, cache_exists
from ...lib.ebook import Ebook
from ...lib.novel import novel_cache_id, INFO_NOVEL_CHAPTERS
from ...lib import book_translations
from ...lib.book_translations import (
    BookTranslation, configure_translator, create_translation,
    delete_translation, edited_paragraphs, find_translations, forget_edits,
    matching_format, model_limits, save_translation, same_book,
    comparison_problem, compare_labels, alignment_keys)
from ...lib.book_translations import (
    engine_class_for, engine_provider, edited_texts)
from ...engines.openai import ChatgptTranslate
from ...engines.openrouter import OpenRouterTranslate


def make_ebook(book_id=7, files=None):
    ebook = Ebook(
        book_id, 'The Book', files or {
            'epub': '/library/Author/The Book (7)/The Book.epub',
            'mobi': '/library/Author/The Book (7)/The Book.mobi'},
        'epub', 'English', ['An Author'])
    ebook.set_target_lang('Italian')
    return ebook


class TempCache(unittest.TestCase):
    """Caches written into a directory of their own, not the user's."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        cache_path = os.path.join(self.directory, 'cache')
        patches = [
            patch.object(TranslationCache, 'dir_path', self.directory),
            patch.object(TranslationCache, 'cache_path', cache_path),
            patch.object(TranslationCache, 'temp_path',
                         os.path.join(self.directory, 'temp')),
            patch.object(book_translations, 'get_cache',
                         lambda cache_id: TranslationCache(cache_id, True)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(shutil.rmtree, self.directory, True)

    def cache(self, cache_id):
        cache = TranslationCache(cache_id, True)
        self.addCleanup(cache.close)
        return cache


class TestMatchingFormat(unittest.TestCase):
    def test_the_same_book_in_the_same_library(self):
        ebook = make_ebook()
        info = {'book_id': '7', 'library_id': 'lib', 'input_format': 'mobi',
                'input_path': '/moved/elsewhere.mobi'}
        self.assertEqual('mobi', matching_format(info, 'x', ebook, 'lib'))

    def test_the_same_book_id_in_another_library_is_another_book(self):
        ebook = make_ebook()
        info = {'book_id': '7', 'library_id': 'other',
                'input_path': '/somewhere/else.epub'}
        self.assertIsNone(matching_format(info, 'x', ebook, 'lib'))

    def test_by_the_path_of_a_file(self):
        ebook = make_ebook()
        info = {'input_path': ebook.files['epub']}
        self.assertEqual('epub', matching_format(info, 'x', ebook, 'lib'))

    def test_a_cache_written_before_the_book_was_recorded(self):
        ebook = make_ebook()
        cache_id = novel_cache_id(
            ebook.files['epub'], 'OpenRouter', 'Italian', '')
        info = {'engine_name': 'OpenRouter', 'target_lang': 'Italian',
                INFO_NOVEL_CHAPTERS: '[]'}
        self.assertEqual('epub', matching_format(info, cache_id, ebook))
        self.assertIsNone(matching_format(info, 'another id', ebook))

    def test_a_cache_of_another_kind_is_nobody_s(self):
        ebook = make_ebook()
        cache_id = novel_cache_id(
            ebook.files['epub'], 'OpenRouter', 'Italian', '')
        info = {'engine_name': 'OpenRouter', 'target_lang': 'Italian'}
        self.assertIsNone(matching_format(info, cache_id, ebook))


class TestTranslations(TempCache):
    def test_two_translations_of_a_book_are_two_caches(self):
        ebook = make_ebook()
        first = create_translation(
            ebook, 'OpenRouter', 'deepseek/deepseek-v4-flash', 'default',
            None, 'lib')
        second = create_translation(
            ebook, 'OpenRouter', 'openai/gpt-6-luna-pro', 'flex',
            {'max_output_tokens': 128000, 'supported_parameters': ['seed']},
            'lib')
        self.assertNotEqual(first, second)
        found = {item.cache_id: item for item in find_translations(
            ebook, 'lib')}
        self.assertEqual({first, second}, set(found))
        self.assertEqual('openai/gpt-6-luna-pro', found[second].model)
        self.assertEqual('flex', found[second].service_tier)
        self.assertEqual('Italian', found[second].target_lang)
        self.assertEqual('epub', found[second].input_format)

    def test_another_book_is_not_listed(self):
        create_translation(make_ebook(), 'OpenRouter', 'm', None, None, 'lib')
        other = make_ebook(8, {'epub': '/library/Other/Other.epub'})
        self.assertEqual([], find_translations(other, 'lib'))

    def test_a_legacy_cache_is_listed_without_a_model(self):
        ebook = make_ebook()
        cache_id = novel_cache_id(
            ebook.files['epub'], 'OpenRouter', 'Italian', '')
        cache = self.cache(cache_id)
        cache.set_info('engine_name', 'OpenRouter')
        cache.set_info('target_lang', 'Italian')
        cache.set_info(INFO_NOVEL_CHAPTERS, json.dumps([{'index': 1}] * 3))
        cache.set_info('novel_progress', '2')
        found = find_translations(ebook)
        self.assertEqual([cache_id], [item.cache_id for item in found])
        self.assertEqual('', found[0].model)
        self.assertEqual('2/3', found[0].progress_label())

    def test_delete(self):
        ebook = make_ebook()
        cache_id = create_translation(ebook, 'OpenRouter', 'm', None, None)
        delete_translation(cache_id)
        self.assertEqual([], find_translations(ebook))

    def test_texts_leave_out_the_ignored_and_the_html(self):
        cache = self.cache('texts')
        cache.add(2, 'md5-2', '<p>b</p>', 'b')
        cache.add(1, 'md5-1', '<p>a</p>', 'a')
        cache.add(3, 'md5-3', '<p></p>', '', ignored=True)
        cache.update(2, translation='B')
        texts = cache.texts()
        self.assertEqual([1, 2], [p.id for p in texts])
        self.assertEqual('B', texts[1].translation)
        self.assertIsNone(texts[0].raw)

    def test_a_kept_translation_is_used_with_the_cache_turned_off(self):
        kept = self.cache('kept')
        kept.set_info('model', 'm')
        with patch.object(cache_module, 'get_config',
                          lambda: {'cache_enabled': False}):
            cache = cache_module.get_cache('kept')
            try:
                self.assertTrue(cache.is_persistence())
                self.assertEqual('m', cache.get_info('model'))
            finally:
                cache.close()
            fresh = cache_module.get_cache('new one')
            self.addCleanup(fresh.close)
            self.assertFalse(fresh.is_persistence())
        self.assertTrue(cache_exists('kept'))
        self.assertFalse(cache_exists('never'))

    def test_a_file_that_is_not_a_database_is_refused(self):
        os.makedirs(TranslationCache.cache_path, exist_ok=True)
        with open(os.path.join(
                TranslationCache.cache_path, 'junk.db'), 'wb') as junk:
            junk.write(b'not sqlite at all ' * 100)
        with self.assertRaises(Exception):
            TranslationCache('junk')
        # And the listing goes past it.
        self.assertEqual([], find_translations(make_ebook()))

    def test_a_rerun_forgets_only_the_corrections_it_replaced(self):
        cache = self.cache('rerun')
        for pid in (1, 2):
            cache.add(pid, 'md5-%d' % pid, '<p/>', 'x%d' % pid)
        save_translation(cache, 1, 'mine 1')
        save_translation(cache, 2, 'mine 2')
        before = edited_texts(cache)
        self.assertEqual({1: 'mine 1', 2: 'mine 2'}, before)
        # The run replaced paragraph 1 and stopped before paragraph 2.
        cache.update(1, translation='model 1')
        forget_edits(cache, before)
        self.assertEqual({2}, edited_paragraphs(cache))
        cache.update(2, translation='model 2')
        forget_edits(cache, before)
        self.assertEqual(set(), edited_paragraphs(cache))

    def test_corrections_are_remembered_and_forgotten(self):
        cache = self.cache('edits')
        cache.add(1, 'md5-1', '<p>a</p>', 'a')
        cache.add(2, 'md5-2', '<p>b</p>', 'b')
        cache.update(1, translation='A', engine_name='OpenRouter')
        save_translation(cache, 1, 'Ah')
        self.assertEqual('Ah', cache.paragraph(1).translation)
        # Still stamped with the engine, so a resume keeps it.
        self.assertEqual('OpenRouter', cache.paragraph(1).engine_name)
        self.assertEqual({1}, edited_paragraphs(cache))
        self.assertEqual(1, BookTranslation('edits', cache.all_info()).edited)
        forget_edits(cache)
        self.assertEqual(set(), edited_paragraphs(cache))


class TestComparison(unittest.TestCase):
    def test_same_book_by_id_path_or_title(self):
        stamped = {'library_id': 'L', 'book_id': '7', 'input_path': '/a',
                   'title': 'T'}
        self.assertTrue(same_book(stamped, dict(stamped, input_path='/b')))
        self.assertFalse(same_book(stamped, dict(stamped, book_id='8')))
        # A cache never opened since 1.3 records only the title.
        self.assertTrue(same_book(stamped, {'title': 'T'}))
        self.assertFalse(same_book(stamped, {'title': 'Other'}))
        self.assertTrue(same_book({'input_path': '/a'},
                                  {'input_path': '/a', 'title': 'X'}))

    def test_what_can_be_compared(self):
        book = {'library_id': 'L', 'book_id': '7', 'input_format': 'epub'}
        self.assertIsNotNone(comparison_problem([book]))
        self.assertIsNone(comparison_problem([book, dict(book)]))
        self.assertIn('not of the same book', comparison_problem(
            [book, dict(book, book_id='8')]))
        # Written before 1.3: which file it was made from is unknown.
        self.assertIn('1.3', comparison_problem(
            [book, {'library_id': 'L', 'book_id': '7'}]))
        # Another file of the book is another list of paragraphs.
        self.assertIn('EPUB, MOBI', comparison_problem(
            [book, dict(book, input_format='mobi')]))

    def test_labels_tell_the_translations_apart(self):
        labels = compare_labels([
            {'model': 'a/x', 'target_lang': 'Italian'},
            {'model': 'b/y', 'service_tier': 'flex',
             'target_lang': 'Italian'},
            {'target_lang': 'Italian'}])
        self.assertEqual('a/x', labels[0])
        self.assertEqual('b/y (flex)', labels[1])
        self.assertIn('not recorded', labels[2])
        twins = compare_labels([
            {'model': 'a/x', 'target_lang': 'Italian', 'created': '1'},
            {'model': 'a/x', 'target_lang': 'French', 'created': '2'},
            {'model': 'a/x', 'target_lang': 'French', 'created': '3'}])
        self.assertEqual('a/x \u2192 Italian', twins[0])
        self.assertEqual('a/x \u2192 French [2]', twins[1])

    def test_paragraphs_line_up_past_an_extra_one(self):
        first = [Paragraph(1, 'a', None, 'Hello'),
                 Paragraph(2, 'b', None, '* * *'),
                 Paragraph(3, 'c', None, 'World'),
                 Paragraph(4, 'd', None, '* * *')]
        # The same book extracted with one element more at the start.
        second = [Paragraph(1, 'z', None, 'Cover'),
                  Paragraph(2, 'a2', None, 'Hello'),
                  Paragraph(3, 'b2', None, '* * *'),
                  Paragraph(4, 'c2', None, 'World'),
                  Paragraph(5, 'd2', None, '* * *')]
        keys, other = alignment_keys(first), alignment_keys(second)
        by_key = {other[p.id]: p.id for p in second}
        self.assertEqual({1: 2, 2: 3, 3: 4, 4: 5},
                         {pid: by_key[key] for pid, key in keys.items()})


class TestProvider(unittest.TestCase):
    """A translation made with one provider of the OpenAI-compatible
    engine keeps it when the settings move to another."""

    def setUp(self):
        ChatgptTranslate.set_config({
            'provider': 'groq', 'api_keys': ['gsk-key'],
            'model': 'llama-3.3-70b-versatile', 'temperature': 0.3,
            'providers': {'deepseek': {
                'api_keys': ['sk-deep'], 'model': 'deepseek-chat'}}})
        self.addCleanup(ChatgptTranslate.set_config, {})

    def test_the_provider_of_the_translation(self):
        engine_class = engine_class_for(
            ChatgptTranslate, {'provider': 'deepseek'})
        self.assertIsNot(ChatgptTranslate, engine_class)
        self.assertEqual('deepseek', engine_provider(engine_class))
        translator = engine_class()
        self.assertEqual('sk-deep', translator.api_key)
        self.assertIn('deepseek', translator.endpoint)
        self.assertEqual('deepseek-chat', translator.model)
        # The class the settings use is left alone.
        self.assertEqual('groq', ChatgptTranslate.config['provider'])

    def test_the_same_provider_or_none(self):
        self.assertIs(ChatgptTranslate, engine_class_for(
            ChatgptTranslate, {'provider': 'groq'}))
        self.assertIs(ChatgptTranslate, engine_class_for(
            ChatgptTranslate, {}))
        self.assertIs(OpenRouterTranslate, engine_class_for(
            OpenRouterTranslate, {'provider': 'deepseek'}))
        self.assertIsNone(engine_provider(OpenRouterTranslate))

    def test_a_legacy_translation_runs_on_the_settings_tier(self):
        legacy = BookTranslation('x', {'engine_name': 'OpenRouter'})
        self.assertIn('settings', legacy.tier_label())
        recorded = BookTranslation('y', {'model': 'm', 'service_tier': ''})
        self.assertEqual('Default', recorded.tier_label())


class TestConfigureTranslator(unittest.TestCase):
    def setUp(self):
        OpenRouterTranslate.set_config({
            'api_keys': ['k'], 'model': 'deepseek/deepseek-v4-flash',
            'model_max_output_tokens': 8192,
            'model_supported_parameters': ['temperature', 'reasoning']})
        self.translator = OpenRouterTranslate()

    def tearDown(self):
        OpenRouterTranslate.set_config({})

    def test_a_translation_without_a_model_runs_on_the_settings(self):
        configure_translator(self.translator, {})
        self.assertEqual('deepseek/deepseek-v4-flash', self.translator.model)
        self.assertEqual(8192, self.translator.model_max_output_tokens)

    def test_the_model_of_the_translation_with_its_limits(self):
        configure_translator(self.translator, {
            'model': 'openai/gpt-6-luna-pro',
            'model_limits': json.dumps({
                'max_output_tokens': 128000,
                'supported_parameters': ['seed', 'response_format']})})
        self.assertEqual('openai/gpt-6-luna-pro', self.translator.model)
        self.assertEqual(128000, self.translator.model_max_output_tokens)
        self.assertEqual(['seed', 'response_format'],
                         self.translator.model_supported_parameters)

    def test_another_model_does_not_inherit_the_settings_figures(self):
        configure_translator(self.translator, {'model': 'someone/else'})
        self.assertEqual(0, self.translator.model_max_output_tokens)
        self.assertEqual([], self.translator.model_supported_parameters)

    def test_the_service_tier_of_the_translation(self):
        configure_translator(self.translator, {'service_tier': 'flex'})
        self.assertEqual('flex', self.translator.service_tier)
        self.assertGreaterEqual(self.translator.request_timeout, 900)

    def test_leaving_flex_gives_the_timeout_back(self):
        OpenRouterTranslate.set_config({
            'api_keys': ['k'], 'service_tier': 'flex',
            'request_timeout': 120})
        translator = OpenRouterTranslate()
        self.assertEqual(900, translator.request_timeout)
        configure_translator(translator, {'service_tier': 'default'})
        self.assertEqual(120, translator.request_timeout)

    def test_limits_of_the_configured_model_come_from_the_settings(self):
        with patch.object(OpenRouterTranslate, 'model_details', {}):
            self.assertEqual(
                {'max_output_tokens': 8192,
                 'supported_parameters': ['temperature', 'reasoning']},
                model_limits(
                    OpenRouterTranslate, 'deepseek/deepseek-v4-flash',
                    'deepseek/deepseek-v4-flash'))
            self.assertEqual({}, model_limits(
                OpenRouterTranslate, 'someone/else',
                'deepseek/deepseek-v4-flash'))

    def test_limits_from_the_listing(self):
        details = {'openai/gpt-6-luna-pro': {
            'max_output_tokens': 128000, 'supported_parameters': ['seed']}}
        with patch.object(OpenRouterTranslate, 'model_details', details):
            self.assertEqual(
                {'max_output_tokens': 128000,
                 'supported_parameters': ['seed']},
                model_limits(OpenRouterTranslate, 'openai/gpt-6-luna-pro'))

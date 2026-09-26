import json
import unittest
from unittest.mock import Mock, patch, call

from ...lib.cache import Paragraph
from ...lib.exception import TranslationCanceled, TranslationFailed
from ...lib.novel import (
    Chapter, ChapterBuilder, TokenBudget, ContextManager, NovelTranslator,
    DEFAULT_NOVEL_TRANSLATION_PROMPT, DEFAULT_NOVEL_GLOSSARY_PROMPT,
    DEFAULT_NOVEL_CONTEXT_PROMPT, _extract_json_string,
    tag_paragraphs, parse_tagged_response, _extract_json_object,
    _extract_entities_fallback, suspicious_translations, _ranges,
    realign_shifted, title_matches, probe_engine, PROBE_PARAGRAPHS,
    INFO_NOVEL_USAGE, INFO_NOVEL_REPORT,
    novel_cache_id, _href_to_page_id,
    detect_dialogue_style, detect_book_dialogue_style,
    dialogue_convention_style, dialogue_instruction, collapse_blank_lines,
    DIALOGUE_CONVENTIONS,
    NO_AUTHOR_INFORMATION, book_excerpt,
    INFO_NOVEL_SUMMARIES, INFO_NOVEL_GLOSSARY, INFO_NOVEL_PROGRESS,
    INFO_NOVEL_STYLE, INFO_NOVEL_STYLE_NOTE)


module_name = 'calibre_plugins.novel_translator.lib.novel'


def make_paragraph(pid, text, page='p1', ignored=False):
    """Build a Paragraph suitable for the pipeline tests."""
    return Paragraph(
        pid, 'md5-%s' % pid, text, text, ignored=ignored, page=page)


class MockTocNode:
    """Mimic calibre.ebooks.oeb.base.TOC nodes just enough for the tests."""

    def __init__(self, title, href, children=None):
        self.title = title
        self.href = href
        self.nodes = list(children or [])


class MockManifestItem:
    def __init__(self, item_id, href):
        self.id = item_id
        self.href = href


# ---------------------------------------------------------------------------
# _href_to_page_id
# ---------------------------------------------------------------------------


class TestHrefToPageId(unittest.TestCase):
    def setUp(self):
        self.items = [
            MockManifestItem('a', 'OEBPS/ch1.xhtml'),
            MockManifestItem('b', 'OEBPS/ch2.xhtml'),
            MockManifestItem('c', 'OEBPS/ch3.xhtml'),
        ]

    def test_exact_match(self):
        self.assertEqual('a', _href_to_page_id(
            'OEBPS/ch1.xhtml', self.items))

    def test_fragment_stripped(self):
        self.assertEqual('b', _href_to_page_id(
            'OEBPS/ch2.xhtml#section-3', self.items))

    def test_basename_fallback(self):
        self.assertEqual('c', _href_to_page_id('ch3.xhtml', self.items))

    def test_empty_href(self):
        self.assertIsNone(_href_to_page_id('', self.items))
        self.assertIsNone(_href_to_page_id(None, self.items))

    def test_unknown(self):
        self.assertIsNone(_href_to_page_id('unknown.xhtml', self.items))


# ---------------------------------------------------------------------------
# ChapterBuilder
# ---------------------------------------------------------------------------


class TestChapterBuilder(unittest.TestCase):
    def setUp(self):
        # Three xhtml pages, spine order = a, b, c.
        self.pages = ['a', 'b', 'c']
        self.items = [
            MockManifestItem('a', 'OEBPS/ch1.xhtml'),
            MockManifestItem('b', 'OEBPS/ch2.xhtml'),
            MockManifestItem('c', 'OEBPS/ch3.xhtml'),
        ]
        self.paragraphs = [
            make_paragraph(0, 'Alpha paragraph one with enough text to pass the front-matter filter.',
                           page='a'),
            make_paragraph(1, 'Alpha paragraph two with enough text to pass the front-matter filter.',
                           page='a'),
            make_paragraph(2, 'Beta paragraph one with enough text to pass the front-matter filter.',
                           page='b'),
            make_paragraph(3, 'Beta paragraph two with enough text to pass the front-matter filter.',
                           page='b'),
            make_paragraph(
                4,
                'Gamma paragraph one: this single paragraph is intentionally long '
                'so that the front-matter filter (default 100 chars) does not '
                'exclude the page from chapter content.',
                page='c'),
            # Aux metadata paragraphs, should be filtered out.
            make_paragraph(5, 'Book title', page='content.opf'),
            make_paragraph(6, 'TOC title', page='toc.ncx'),
        ]

    def test_toc_level_1(self):
        toc = [
            MockTocNode('Chapter One', 'OEBPS/ch1.xhtml'),
            MockTocNode('Chapter Two', 'OEBPS/ch2.xhtml'),
            MockTocNode('Chapter Three', 'OEBPS/ch3.xhtml'),
        ]
        chapters = ChapterBuilder(
            self.pages, toc, self.items, self.paragraphs).build()
        self.assertEqual(3, len(chapters))
        self.assertEqual([1, 2, 3], [c.index for c in chapters])
        self.assertEqual(
            ['Chapter One', 'Chapter Two', 'Chapter Three'],
            [c.title for c in chapters])
        self.assertEqual(2, len(chapters[0].paragraphs))
        self.assertEqual(2, len(chapters[1].paragraphs))
        self.assertEqual(1, len(chapters[2].paragraphs))
        # Aux paragraphs must not leak into chapters.
        for c in chapters:
            for p in c.paragraphs:
                self.assertNotIn(p.page, ChapterBuilder.AUX_PAGES)

    def test_toc_level_1_only_ignores_nested(self):
        # Only the top-level nodes should define chapter boundaries; a
        # nested subnode inside "Chapter One" must not split the chapter.
        toc = [
            MockTocNode('Chapter One', 'OEBPS/ch1.xhtml', [
                MockTocNode('Section 1.1', 'OEBPS/ch2.xhtml')]),
            MockTocNode('Chapter Two', 'OEBPS/ch3.xhtml'),
        ]
        chapters = ChapterBuilder(
            self.pages, toc, self.items, self.paragraphs).build()
        self.assertEqual(2, len(chapters))
        # Chapter One should now include pages a and b.
        self.assertEqual(['a', 'b'], chapters[0].page_ids)
        self.assertEqual(['c'], chapters[1].page_ids)

    def test_fallback_to_files_when_toc_empty(self):
        chapters = ChapterBuilder(
            self.pages, [], self.items, self.paragraphs).build()
        self.assertEqual(3, len(chapters))
        # Titles derive from first paragraph text (truncated to 80 chars).
        self.assertTrue(chapters[0].title.startswith('Alpha paragraph one'),
                        chapters[0].title)
        self.assertTrue(chapters[1].title.startswith('Beta paragraph one'),
                        chapters[1].title)
        self.assertTrue(chapters[2].title.startswith('Gamma paragraph one'),
                        chapters[2].title)

    def test_fallback_when_toc_has_single_node(self):
        toc = [MockTocNode('Only', 'OEBPS/ch1.xhtml')]
        chapters = ChapterBuilder(
            self.pages, toc, self.items, self.paragraphs).build()
        # Falls back to xhtml file mode -> 3 chapters, not 1.
        self.assertEqual(3, len(chapters))

    def test_page_before_first_boundary_goes_to_chapter_1(self):
        # TOC starts at ch2 but page 'a' exists in the spine.
        toc = [
            MockTocNode('Chapter Two', 'OEBPS/ch2.xhtml'),
            MockTocNode('Chapter Three', 'OEBPS/ch3.xhtml'),
        ]
        chapters = ChapterBuilder(
            self.pages, toc, self.items, self.paragraphs).build()
        self.assertEqual(2, len(chapters))
        self.assertIn('a', chapters[0].page_ids)
        self.assertIn('b', chapters[0].page_ids)
        self.assertIn('c', chapters[1].page_ids)

    def test_toc_with_bad_href_is_skipped(self):
        toc = [
            MockTocNode('Chapter One', 'OEBPS/ch1.xhtml'),
            MockTocNode('Broken', 'does_not_exist.xhtml'),
            MockTocNode('Chapter Two', 'OEBPS/ch2.xhtml'),
        ]
        chapters = ChapterBuilder(
            self.pages, toc, self.items, self.paragraphs).build()
        # Broken href yields 2 usable boundaries, not 3.
        self.assertEqual(2, len(chapters))

    def test_aux_paragraphs_collected_separately(self):
        builder = ChapterBuilder(
            self.pages, [], self.items, self.paragraphs)
        self.assertEqual(2, len(builder.aux_paragraphs))
        pages = {p.page for p in builder.aux_paragraphs}
        self.assertEqual({'content.opf', 'toc.ncx'}, pages)

    def test_ignored_paragraphs_kept_but_not_counted(self):
        paragraphs = list(self.paragraphs)
        paragraphs[0].ignored = True  # First 'a' paragraph is ignored.
        chapters = ChapterBuilder(
            self.pages, [], self.items, paragraphs,
            front_matter_min_chars=0).build()
        # It still travels through the pipeline so DOM re-assembly can run.
        self.assertEqual(2, len(chapters[0].paragraphs))
        # But translatable count reflects the ignored flag.
        self.assertEqual(1, len(chapters[0].translatable_paragraphs()))

    def test_empty_spine(self):
        chapters = ChapterBuilder([], [], [], []).build()
        self.assertEqual([], chapters)

    def test_char_count(self):
        chapter = Chapter(1, 't', ['a'], [
            make_paragraph(0, 'hello'),
            make_paragraph(1, 'world!', ignored=True),
            make_paragraph(2, ''),
        ])
        # Only non-ignored paragraphs contribute.
        self.assertEqual(len('hello') + 0, chapter.char_count)

    # -- toc_level_2 ----------------------------------------------------------

    def _pages_and_items_for_anthology(self):
        """Return spine/items for a minimal 2-book anthology.

        Structure mirrors a real anthology EPUB:
          spine: cover_b1, ch1_b1, ch2_b1, cover_b2, ch1_b2
          TOC level-1: Book1 -> cover_b1, Book2 -> cover_b2
          TOC level-2: Ch1 -> ch1_b1, Ch2 -> ch2_b1, Ch1b2 -> ch1_b2
        """
        pages = ['cover_b1', 'ch1_b1', 'ch2_b1', 'cover_b2', 'ch1_b2']
        items = [
            MockManifestItem('cover_b1', 'book1/cover.xhtml'),
            MockManifestItem('ch1_b1', 'book1/chapter1.xhtml'),
            MockManifestItem('ch2_b1', 'book1/chapter2.xhtml'),
            MockManifestItem('cover_b2', 'book2/cover.xhtml'),
            MockManifestItem('ch1_b2', 'book2/chapter1.xhtml'),
        ]
        return pages, items

    def test_toc_level2_splits_anthology_into_narrative_chapters(self):
        pages, items = self._pages_and_items_for_anthology()
        # Level-1 TOC nodes each have level-2 children.
        toc = [
            MockTocNode('Book One', 'book1/cover.xhtml', [
                MockTocNode('Chapter One', 'book1/chapter1.xhtml'),
                MockTocNode('Chapter Two', 'book1/chapter2.xhtml'),
            ]),
            MockTocNode('Book Two', 'book2/cover.xhtml', [
                MockTocNode('Chapter One', 'book2/chapter1.xhtml'),
            ]),
        ]
        paragraphs = [
            make_paragraph(0, 'x' * 200, page='ch1_b1'),
            make_paragraph(1, 'x' * 200, page='ch2_b1'),
            make_paragraph(2, 'x' * 200, page='ch1_b2'),
            # Cover pages: short text (< 100 chars)
            make_paragraph(3, 'Book One', page='cover_b1'),
            make_paragraph(4, 'Book Two', page='cover_b2'),
        ]
        builder = ChapterBuilder(
            pages, toc, items, paragraphs, source='toc_level_2',
            front_matter_min_chars=0)  # disable front-matter filter here
        chapters = builder.build()
        # 3 level-2 chapters: Ch1/B1, Ch2/B1, Ch1/B2
        self.assertEqual(3, len(chapters))
        self.assertEqual('Chapter One', chapters[0].title)
        self.assertEqual('Chapter Two', chapters[1].title)
        self.assertEqual('Chapter One', chapters[2].title)

    def test_toc_level2_fallback_to_level1_when_no_children(self):
        """If level-2 has fewer than 2 entries, fall back to level-1."""
        pages, items = self._pages_and_items_for_anthology()
        toc = [
            MockTocNode('Book One', 'book1/cover.xhtml'),  # no children
            MockTocNode('Book Two', 'book2/cover.xhtml'),  # no children
        ]
        paragraphs = [make_paragraph(i, 'text', page=p)
                      for i, p in enumerate(pages)]
        builder = ChapterBuilder(
            pages, toc, items, paragraphs, source='toc_level_2',
            front_matter_min_chars=0)
        chapters = builder.build()
        # Falls back to level-1 -> 2 chapters
        self.assertEqual(2, len(chapters))
        self.assertEqual('Book One', chapters[0].title)

    # -- front-matter filter --------------------------------------------------

    def test_front_matter_filter_excludes_short_pages(self):
        """Pages with very little text are excluded from chapter content."""
        pages = ['cover', 'ch1', 'ch2']
        items = [
            MockManifestItem('cover', 'cover.xhtml'),
            MockManifestItem('ch1', 'chapter1.xhtml'),
            MockManifestItem('ch2', 'chapter2.xhtml'),
        ]
        toc = [
            MockTocNode('Ch1', 'chapter1.xhtml'),
            MockTocNode('Ch2', 'chapter2.xhtml'),
        ]
        paragraphs = [
            # Cover page: 8 chars only (below threshold 100)
            make_paragraph(0, 'THE BOOK', page='cover'),
            # Narrative pages: plenty of text
            make_paragraph(1, 'x' * 200, page='ch1'),
            make_paragraph(2, 'x' * 150, page='ch2'),
        ]
        builder = ChapterBuilder(
            pages, toc, items, paragraphs, source='toc_level_1',
            front_matter_min_chars=100)
        chapters = builder.build()
        self.assertEqual(2, len(chapters))
        # The cover page paragraph must not appear in any chapter.
        for ch in chapters:
            for p in ch.paragraphs:
                self.assertNotEqual('cover', p.page,
                    'Cover page paragraph leaked into chapter content')
        # ...but it is not lost either: it is handed back with the
        # metadata and the TOC entries, to be translated apart from the
        # narrative. A title page left in the source language is a hole.
        self.assertEqual(
            ['THE BOOK'],
            [p.original for p in builder.auxiliary_paragraphs()])

    def test_front_matter_filter_zero_disables(self):
        """Setting front_matter_min_chars=0 disables the filter entirely."""
        pages = ['cover', 'ch1']
        items = [
            MockManifestItem('cover', 'cover.xhtml'),
            MockManifestItem('ch1', 'chapter1.xhtml'),
        ]
        toc = [MockTocNode('Ch1', 'chapter1.xhtml'),
               MockTocNode('Cover', 'cover.xhtml')]
        paragraphs = [
            make_paragraph(0, 'THE BOOK', page='cover'),  # short
            make_paragraph(1, 'x' * 200, page='ch1'),
        ]
        builder = ChapterBuilder(
            pages, toc, items, paragraphs, source='toc_level_1',
            front_matter_min_chars=0)
        chapters = builder.build()
        # With filter disabled, cover paragraph must appear in chapter 2
        all_pages = {p.page for ch in chapters for p in ch.paragraphs}
        self.assertIn('cover', all_pages)

    def test_front_matter_long_pages_not_excluded(self):
        """Pages with enough text are kept even with the filter enabled."""
        pages = ['ch1', 'ch2']
        items = [
            MockManifestItem('ch1', 'chapter1.xhtml'),
            MockManifestItem('ch2', 'chapter2.xhtml'),
        ]
        toc = [MockTocNode('Ch1', 'chapter1.xhtml'),
               MockTocNode('Ch2', 'chapter2.xhtml')]
        paragraphs = [
            make_paragraph(0, 'x' * 200, page='ch1'),  # 200 chars > 100
            make_paragraph(1, 'x' * 150, page='ch2'),
        ]
        builder = ChapterBuilder(
            pages, toc, items, paragraphs, source='toc_level_1',
            front_matter_min_chars=100)
        chapters = builder.build()
        all_pages = {p.page for ch in chapters for p in ch.paragraphs}
        self.assertIn('ch1', all_pages)
        self.assertIn('ch2', all_pages)


# ---------------------------------------------------------------------------
# TokenBudget
# ---------------------------------------------------------------------------


class TestTokenBudget(unittest.TestCase):
    def test_estimate_latin(self):
        budget = TokenBudget()
        # ~4 chars/token on latin text.
        self.assertGreater(budget.estimate('hello world!'), 0)
        self.assertLess(budget.estimate('hello world!'), 10)

    def test_estimate_cjk(self):
        budget = TokenBudget()
        # 4 CJK characters -> ~2 tokens with ratio_cjk=2.
        cjk_text = '你好世界'
        self.assertGreaterEqual(budget.estimate(cjk_text), 2)

    def test_estimate_empty(self):
        self.assertEqual(0, TokenBudget().estimate(''))
        self.assertEqual(0, TokenBudget().estimate(None))

    def test_budget_minimum(self):
        # Very small budgets are clamped to at least 100.
        budget = TokenBudget(budget=10)
        self.assertEqual(100, budget.budget)

    def test_chunk_single_fit(self):
        paragraphs = [
            make_paragraph(0, 'short one'),
            make_paragraph(1, 'short two'),
            make_paragraph(2, 'short three'),
        ]
        budget = TokenBudget(budget=8000)
        chunks = budget.chunk(paragraphs, reserved=0)
        self.assertEqual(1, len(chunks))
        self.assertEqual(3, len(chunks[0]))

    def test_chunk_splits_when_needed(self):
        # A tiny budget will force multiple chunks.
        paragraphs = [
            make_paragraph(0, 'x' * 400),
            make_paragraph(1, 'y' * 400),
            make_paragraph(2, 'z' * 400),
        ]
        # 400 chars / 4 ~= 100 tokens per paragraph.
        budget = TokenBudget(budget=200)
        chunks = budget.chunk(paragraphs, reserved=0)
        self.assertGreater(len(chunks), 1)
        # Total paragraphs preserved.
        total = sum(len(c) for c in chunks)
        self.assertEqual(3, total)

    def test_chunk_never_splits_paragraph(self):
        paragraphs = [make_paragraph(0, 'x' * 100000)]  # ~25k tokens
        budget = TokenBudget(budget=200)
        chunks = budget.chunk(paragraphs, reserved=0)
        self.assertEqual(1, len(chunks))
        self.assertEqual(1, len(chunks[0]))

    def test_chunk_reserves_headroom(self):
        paragraphs = [make_paragraph(i, 'x' * 400) for i in range(10)]
        # 8000 budget, reserved 7500 -> only 500 tokens available.
        # Each paragraph ~100 tokens -> at most ~5 per chunk.
        budget = TokenBudget(budget=8000)
        chunks = budget.chunk(paragraphs, reserved=7500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 5)

    def test_chunk_carries_ignored_paragraphs(self):
        paragraphs = [
            make_paragraph(0, 'text 1'),
            make_paragraph(1, '', ignored=True),
            make_paragraph(2, 'text 2'),
        ]
        chunks = TokenBudget(budget=8000).chunk(paragraphs, reserved=0)
        self.assertEqual(1, len(chunks))
        # All paragraphs (including ignored) are still present.
        self.assertEqual(3, len(chunks[0]))

    # -- dual-cap chunking ------------------------------------------------

    def test_chunk_capped_by_max_paragraphs(self):
        # 100 very short paragraphs, small token weight but many tags.
        # Cap tokens is generous (8000) so the paragraph cap should fire.
        paragraphs = [make_paragraph(i, 'short') for i in range(100)]
        budget = TokenBudget(budget=8000, max_paragraphs=30)
        chunks = budget.chunk(paragraphs, reserved=0)
        # 100 / 30 = ceil(3.33) => 4 chunks.
        self.assertEqual(4, len(chunks))
        for c in chunks:
            # Each chunk must not exceed the paragraph cap.
            self.assertLessEqual(len(c), 30)

    def test_chunk_capped_by_tokens(self):
        # Few very large paragraphs: token cap should fire first.
        paragraphs = [make_paragraph(i, 'x' * 3000) for i in range(5)]
        # Very small token budget forces splitting on tokens.
        budget = TokenBudget(budget=200, max_paragraphs=60)
        chunks = budget.chunk(paragraphs, reserved=0)
        # Oversized paragraphs become singleton chunks -> 5 chunks.
        self.assertGreater(len(chunks), 1)
        # No chunk should hold more than one paragraph in this case.
        for c in chunks:
            self.assertEqual(1, len(c))

    def test_chunk_max_paragraphs_zero_disables_cap(self):
        # Many short paragraphs, generous token budget, cap=0 -> single chunk.
        paragraphs = [make_paragraph(i, 'short') for i in range(200)]
        budget = TokenBudget(budget=100000, max_paragraphs=0)
        chunks = budget.chunk(paragraphs, reserved=0)
        self.assertEqual(1, len(chunks))
        self.assertEqual(200, len(chunks[0]))

    def test_chunk_default_max_paragraphs(self):
        # Sized on what a model can write, not on what it can read: the
        # reply is about as long as the chunk, and output limits are far
        # below context windows. Small and local models need it lowered.
        budget = TokenBudget(budget=8000)
        self.assertEqual(100, budget.max_paragraphs)

    def test_chunk_with_stats_reports_reason(self):
        # 70 short paragraphs, generous token budget, cap=60.
        # First chunk closed by paragraphs, second by end of stream.
        paragraphs = [make_paragraph(i, 'short') for i in range(70)]
        budget = TokenBudget(budget=100000, max_paragraphs=60)
        stats = budget.chunk_with_stats(paragraphs, reserved=0)
        self.assertEqual(2, len(stats))
        # (chunk, tokens, reason)
        self.assertEqual(TokenBudget.REASON_PARAGRAPHS, stats[0][2])
        self.assertEqual(TokenBudget.REASON_END, stats[-1][2])
        # First chunk has exactly 60 paragraphs (the cap).
        self.assertEqual(60, len(stats[0][0]))

    def test_chunk_with_stats_token_reason(self):
        # Trigger token-based closure with small budget.
        paragraphs = [make_paragraph(i, 'x' * 400) for i in range(10)]
        # 400 chars / 4 ~= 100 tokens per paragraph.
        budget = TokenBudget(budget=250, max_paragraphs=60)
        stats = budget.chunk_with_stats(paragraphs, reserved=0)
        # At least the first split should be token-driven.
        reasons = [s[2] for s in stats[:-1]]
        self.assertIn(TokenBudget.REASON_TOKENS, reasons)

    def test_chunk_ignored_do_not_count_toward_paragraph_cap(self):
        # 5 translatable + 55 ignored + 5 translatable = 10 translatable
        # but 65 total. With max_paragraphs=60 the cap should NOT fire
        # because ignored paragraphs do not consume the budget.
        paragraphs = []
        for i in range(5):
            paragraphs.append(make_paragraph(i, 'text'))
        for i in range(5, 60):
            paragraphs.append(make_paragraph(i, '', ignored=True))
        for i in range(60, 65):
            paragraphs.append(make_paragraph(i, 'text'))
        budget = TokenBudget(budget=100000, max_paragraphs=60)
        chunks = budget.chunk(paragraphs, reserved=0)
        # All 65 paragraphs (including ignored) fit in a single chunk.
        self.assertEqual(1, len(chunks))
        self.assertEqual(65, len(chunks[0]))


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------


class TestContextManager(unittest.TestCase):
    def setUp(self):
        self.cache = Mock()
        self.cache.get_info.return_value = None

    def test_load_empty(self):
        ctx = ContextManager(self.cache).load()
        self.assertEqual([], ctx.get_summaries())
        self.assertEqual({}, ctx.get_glossary())
        self.assertEqual(0, ctx.get_progress())

    def test_load_existing(self):
        summaries = [{'chapter': 1, 'title': 'a', 'summary': 'ok'}]
        glossary = {'Frodo': {
            'translation': 'Frodo', 'type': 'character', 'notes': 'hobbit'}}
        self.cache.get_info.side_effect = lambda key: {
            INFO_NOVEL_SUMMARIES: json.dumps(summaries),
            INFO_NOVEL_GLOSSARY: json.dumps(glossary),
            INFO_NOVEL_PROGRESS: '1',
        }.get(key)
        ctx = ContextManager(self.cache).load()
        self.assertEqual(summaries, ctx.get_summaries())
        self.assertEqual(glossary, ctx.get_glossary())
        self.assertEqual(1, ctx.get_progress())

    def test_load_handles_corrupted_json(self):
        self.cache.get_info.side_effect = lambda key: {
            INFO_NOVEL_SUMMARIES: 'not json',
            INFO_NOVEL_GLOSSARY: '[not a dict]',
            INFO_NOVEL_PROGRESS: 'garbage',
        }.get(key)
        ctx = ContextManager(self.cache).load()
        self.assertEqual([], ctx.get_summaries())
        self.assertEqual({}, ctx.get_glossary())
        self.assertEqual(0, ctx.get_progress())

    def test_append_chapter(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 'The Ring', 'Frodo gets the ring.', [
            {'source': 'Frodo', 'translation': 'Frodo',
             'type': 'character', 'notes': 'hobbit'},
            {'source': 'The Shire', 'translation': 'La Contea',
             'type': 'place'},
        ])
        summaries = ctx.get_summaries()
        self.assertEqual(1, len(summaries))
        self.assertEqual('Frodo gets the ring.', summaries[0]['summary'])
        glossary = ctx.get_glossary()
        self.assertIn('Frodo', glossary)
        self.assertEqual('La Contea', glossary['The Shire']['translation'])
        self.assertEqual(1, ctx.get_progress())
        # Persistence: three set_info calls (summaries, glossary, progress).
        keys = [c.args[0] for c in self.cache.set_info.call_args_list]
        self.assertIn(INFO_NOVEL_SUMMARIES, keys)
        self.assertIn(INFO_NOVEL_GLOSSARY, keys)
        self.assertIn(INFO_NOVEL_PROGRESS, keys)

    def test_append_chapter_again_replaces_its_summary(self):
        # A chapter translated a second time ("Re-run all") must not
        # leave two entries that every later prompt would then carry.
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 'One', 'First take.', [])
        ctx.append_chapter(2, 'Two', 'Second chapter.', [])
        ctx.append_chapter(1, 'One', 'Second take.', [])
        self.assertEqual(
            [(1, 'Second take.'), (2, 'Second chapter.')],
            [(s['chapter'], s['summary']) for s in ctx.get_summaries()])

    def test_reset_progress_keeps_what_was_learned(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 'One', 'Summary.', [
            {'source': 'Aslan', 'translation': 'Aslan'}])
        ctx.set_style('Dry and ironic.')
        ctx.reset_progress()
        self.assertEqual(0, ctx.get_progress())
        self.assertEqual(1, len(ctx.get_summaries()))
        self.assertIn('Aslan', ctx.get_glossary())
        self.assertEqual('Dry and ironic.', ctx.get_style())

    def test_append_chapter_ignores_malformed_entities(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 't', 's', [
            {'source': 'Ok', 'translation': 'Ok'},
            {'source': '', 'translation': 'nope'},      # empty source
            {'source': 'X', 'translation': ''},         # empty translation
            'not a dict',
            {'source': 'Y'},                             # missing translation
        ])
        self.assertEqual(['Ok'], list(ctx.get_glossary().keys()))

    def test_glossary_cap_fifo(self):
        ctx = ContextManager(self.cache, glossary_max_entries=3).load()
        ctx.append_chapter(1, 't', 's', [
            {'source': 'A', 'translation': 'A'},
            {'source': 'B', 'translation': 'B'},
            {'source': 'C', 'translation': 'C'},
        ])
        self.assertEqual(3, len(ctx.get_glossary()))
        ctx.append_chapter(2, 't', 's', [
            {'source': 'D', 'translation': 'D'},
        ])
        keys = list(ctx.get_glossary().keys())
        self.assertEqual(3, len(keys))
        self.assertNotIn('A', keys)  # Oldest dropped.
        self.assertIn('D', keys)

    def test_an_entry_written_by_the_user_is_never_changed(self):
        ctx = ContextManager(self.cache).load()
        ctx.replace_glossary({
            'Sir Kay': {'translation': 'Ser Caio', 'user': True}})
        ctx.append_chapter(1, 't', 's', [
            {'source': 'Sir Kay', 'translation': 'Sir Kay',
             'type': 'character'},
            {'source': 'Merlin', 'translation': 'Merlino'},
        ])
        self.assertEqual(
            {'translation': 'Ser Caio', 'user': True},
            ctx.get_glossary()['Sir Kay'])
        self.assertEqual('Merlino', ctx.get_glossary()['Merlin'][
            'translation'])

    def test_the_cap_never_drops_an_entry_written_by_the_user(self):
        ctx = ContextManager(self.cache, glossary_max_entries=2).load()
        ctx.replace_glossary({'A': {'translation': 'A', 'user': True}})
        ctx.append_chapter(1, 't', 's', [
            {'source': 'B', 'translation': 'B'},
            {'source': 'C', 'translation': 'C'},
        ])
        self.assertEqual(['A', 'C'], list(ctx.get_glossary()))

    def test_progress_never_decreases(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(3, 't', 's')
        self.assertEqual(3, ctx.get_progress())
        ctx.append_chapter(2, 't', 's')  # Retro-append should not lower.
        self.assertEqual(3, ctx.get_progress())

    def test_context_text_basic(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 'Prologue', 'Something happens.', [
            {'source': 'Bilbo', 'translation': 'Bilbo',
             'type': 'character'},
        ])
        text = ctx.context_text(budget_tokens=2000)
        self.assertIn('Prologue', text)
        self.assertIn('Something happens.', text)
        self.assertIn('Bilbo', text)

    def test_context_text_truncation(self):
        ctx = ContextManager(self.cache).load()
        # Fill with many long summaries; small budget forces trimming.
        for i in range(10):
            ctx.append_chapter(
                i + 1, 'T%d' % i, 'x' * 500,
                [{'source': 'K%d' % i, 'translation': 'V%d' % i}])
        # Budget of 100 tokens ~= 400 chars.
        text = ctx.context_text(budget_tokens=100)
        # It should never be empty and always contain the header labels.
        self.assertTrue(text)

    def test_glossary_for_keeps_only_the_names_in_the_text(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 't', 's', [
            {'source': 'Fidelma', 'translation': 'Fidelma'},
            {'source': 'Canterbury', 'translation': 'Canterbury'},
            {'source': 'Wighard', 'translation': 'Wighard'},
        ])
        # Possessives and case differences still find their entry.
        selected = ctx.glossary_for(
            "fidelma's cell was next to Wighard's.")
        self.assertEqual({'Fidelma', 'Wighard'}, set(selected))

    def test_glossary_for_matches_a_shortened_name(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 't', 's', [
            {'source': 'Bishop Gelasius', 'translation': 'Vescovo Gelasio'},
            {'source': 'Abbess Wulfrun', 'translation': 'Badessa Wulfrun'},
        ])
        # The prose rarely repeats a title; the entry is still needed.
        selected = ctx.glossary_for('Gelasius raised his hand.')
        self.assertEqual({'Bishop Gelasius'}, set(selected))

    def test_glossary_for_without_text_returns_everything(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 't', 's', [
            {'source': 'A', 'translation': 'A'},
            {'source': 'B', 'translation': 'B'},
        ])
        self.assertEqual({'A', 'B'}, set(ctx.glossary_for(None)))

    def test_glossary_for_limit_keeps_the_most_recent(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 't', 's', [
            {'source': 'A', 'translation': 'A'},
            {'source': 'B', 'translation': 'B'},
            {'source': 'C', 'translation': 'C'},
        ])
        self.assertEqual(['B', 'C'], list(ctx.glossary_for(None, limit=2)))

    def test_context_text_carries_only_the_relevant_glossary(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 'Prologue', 'Something happens.', [
            {'source': 'Bilbo', 'translation': 'Bilbo'},
            {'source': 'Smaug', 'translation': 'Smaug'},
        ])
        text = ctx.context_text(
            budget_tokens=2000, relevant_to='Bilbo walked home.')
        self.assertIn('Bilbo', text)
        self.assertNotIn('Smaug', text)

    def test_replace_glossary(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(1, 't', 's', [
            {'source': 'A', 'translation': 'A'},
        ])
        ctx.replace_glossary({
            'B': {'translation': 'B', 'type': 'character'},
            'Bad': {'translation': ''},  # dropped: missing translation
            'Weird': 'not a dict',        # dropped: wrong type
        })
        self.assertEqual({'B'}, set(ctx.get_glossary().keys()))

    def test_reset(self):
        ctx = ContextManager(self.cache).load()
        ctx.append_chapter(2, 't', 's', [
            {'source': 'A', 'translation': 'A'}])
        ctx.reset()
        self.assertEqual([], ctx.get_summaries())
        self.assertEqual({}, ctx.get_glossary())
        self.assertEqual(0, ctx.get_progress())


# ---------------------------------------------------------------------------
# tag_paragraphs / parse_tagged_response
# ---------------------------------------------------------------------------


class TestTagging(unittest.TestCase):
    def test_tag_paragraphs_basic(self):
        paragraphs = [
            make_paragraph(0, 'First.'),
            make_paragraph(1, 'Second.'),
            make_paragraph(2, 'Third.'),
        ]
        tagged, indices = tag_paragraphs(paragraphs)
        self.assertEqual([1, 2, 3], indices)
        self.assertIn('[1]\nFirst.', tagged)
        self.assertIn('[2]\nSecond.', tagged)
        self.assertIn('[3]\nThird.', tagged)

    def test_tag_paragraphs_skips_ignored(self):
        paragraphs = [
            make_paragraph(0, 'A'),
            make_paragraph(1, 'B', ignored=True),
            make_paragraph(2, 'C'),
        ]
        tagged, indices = tag_paragraphs(paragraphs)
        # Indices count the *paragraphs list* position, not translated count.
        self.assertEqual([1, 3], indices)
        self.assertIn('[1]\nA', tagged)
        self.assertIn('[3]\nC', tagged)
        self.assertNotIn('[2]', tagged)

    def test_parse_tagged_response(self):
        response = (
            "Sure, here is the translation:\n"
            "[1]\nPrimo.\n\n"
            "[2]\nSecondo.\n\n"
            "[3]\nTerzo.\n\nEnd of translation.")
        parsed = parse_tagged_response(response, [1, 2, 3])
        self.assertEqual({1: 'Primo.', 2: 'Secondo.', 3: 'Terzo.'}, parsed)

    def test_parse_tagged_response_multiline(self):
        response = (
            "[1]\nLine one.\nLine two.\n\n"
            "[2]\nAlone.")
        parsed = parse_tagged_response(response, [1, 2])
        self.assertEqual(2, len(parsed))
        self.assertIn('Line one.', parsed[1])
        self.assertIn('Line two.', parsed[1])
        self.assertEqual('Alone.', parsed[2])

    def test_parse_tagged_response_missing(self):
        response = "[1]\nOnly one."
        parsed = parse_tagged_response(response, [1, 2, 3])
        self.assertEqual({1: 'Only one.'}, parsed)

    def test_parse_tagged_response_ignores_hallucinated_tags(self):
        response = "[1]\nReal.\n\n[5]\nHallucinated."
        parsed = parse_tagged_response(response, [1, 2])
        # Marker 5 was not expected; ignored.
        self.assertEqual({1: 'Real.'}, parsed)

    def test_parse_tagged_response_empty(self):
        self.assertEqual({}, parse_tagged_response('', [1]))
        self.assertEqual({}, parse_tagged_response(None, [1]))

    def test_parse_tagged_response_extra_whitespace(self):
        response = (
            "\n\n  [1]  \nPrimo paragrafo.\n\n\n"
            "  [2]\nSecondo.\n")
        parsed = parse_tagged_response(response, [1, 2])
        self.assertEqual('Primo paragrafo.', parsed[1])
        self.assertEqual('Secondo.', parsed[2])

    def test_parse_tagged_response_duplicate_marker_last_wins(self):
        response = "[1]\nFirst try.\n\n[1]\nBetter version."
        parsed = parse_tagged_response(response, [1])
        self.assertEqual('Better version.', parsed[1])


# ---------------------------------------------------------------------------
# _extract_json_object
# ---------------------------------------------------------------------------


class TestExtractJson(unittest.TestCase):
    def test_clean_json(self):
        self.assertEqual(
            {'a': 1}, _extract_json_object('{"a": 1}'))

    def test_with_prose_prefix(self):
        self.assertEqual(
            {'entities': []},
            _extract_json_object('Here is the JSON:\n{"entities": []}'))

    def test_with_code_fence(self):
        self.assertEqual(
            {'x': 'y'},
            _extract_json_object('```json\n{"x": "y"}\n```'))

    def test_ignores_braces_in_strings(self):
        self.assertEqual(
            {'text': 'this { is not } a brace'},
            _extract_json_object('{"text": "this { is not } a brace"}'))

    def test_returns_none_when_invalid(self):
        self.assertIsNone(_extract_json_object('no json here'))
        self.assertIsNone(_extract_json_object(''))
        self.assertIsNone(_extract_json_object(None))
        self.assertIsNone(_extract_json_object('{"broken":'))


class TestExtractEntitiesFallback(unittest.TestCase):
    def test_arrow_syntax(self):
        text = (
            'Here are some entities:\n'
            '"Aslan" -> "Aslan" (character): the lion king\n'
            '"Narnia" -> "Narnia" (place)\n'
            'End of list.')
        entities = _extract_entities_fallback(text)
        sources = [e['source'] for e in entities]
        self.assertIn('Aslan', sources)
        self.assertIn('Narnia', sources)

    def test_double_arrow_syntax(self):
        text = 'Aslan => Aslan (character) - the lion'
        entities = _extract_entities_fallback(text)
        self.assertTrue(
            any(e['source'] == 'Aslan' for e in entities))

    def test_unicode_arrow(self):
        text = '"Frodo" → "Frodo" (character): the hobbit'
        entities = _extract_entities_fallback(text)
        self.assertTrue(
            any(e['source'] == 'Frodo' for e in entities))

    def test_skips_prose_lines(self):
        text = (
            'This sentence has many words but is not an entity mapping.\n'
            'Here are the entities I extracted from the chapter:')
        entities = _extract_entities_fallback(text)
        # No entity mapping in prose (no ``->`` separator).
        self.assertEqual([], entities)

    def test_empty_returns_empty(self):
        self.assertEqual([], _extract_entities_fallback(''))
        self.assertEqual([], _extract_entities_fallback(None))

    def test_dedup_by_source(self):
        text = (
            '"Aslan" -> "Aslan" (character): king\n'
            '"Aslan" -> "Aslan" (character): duplicate line')
        entities = _extract_entities_fallback(text)
        self.assertEqual(
            1, sum(1 for e in entities if e['source'] == 'Aslan'))


# ---------------------------------------------------------------------------
# novel_cache_id
# ---------------------------------------------------------------------------


class TestNovelCacheId(unittest.TestCase):
    def test_deterministic(self):
        a = novel_cache_id('/b.epub', 'ChatGPT', 'Italian', '')
        b = novel_cache_id('/b.epub', 'ChatGPT', 'Italian', '')
        self.assertEqual(a, b)

    def test_differs_from_classic(self):
        from ...lib.utils import uid
        classic = uid('/b.epub' + 'ChatGPT' + 'Italian' + '1800' + '')
        novel = novel_cache_id('/b.epub', 'ChatGPT', 'Italian', '')
        self.assertNotEqual(classic, novel)

    def test_encoding_included(self):
        a = novel_cache_id('/b.epub', 'ChatGPT', 'Italian', '')
        b = novel_cache_id('/b.epub', 'ChatGPT', 'Italian', 'gbk')
        self.assertNotEqual(a, b)


# ---------------------------------------------------------------------------
# NovelTranslator
# ---------------------------------------------------------------------------


def _echo_markers(text, suffix=' (IT)'):
    """Test helper: echo back the [N] markers found in ``text``, appending
    ``suffix`` to each paragraph body. Simulates a well-behaved LLM that
    respects the numbered-marker translation format.
    """
    import re
    result = []
    for m in re.finditer(
            r'^\s*\[(\d+)\]\s*\n(.*?)(?=\n\s*\[\d+\]|\Z)',
            text, re.MULTILINE | re.DOTALL):
        result.append('[%s]\n%s%s' % (
            m.group(1), m.group(2).strip(), suffix))
    return '\n\n'.join(result)


def _is_translation_call(prompt):
    """The novel translator uses distinct system prompts for the three
    call kinds:

      * Translation: "You are a professional literary translator..."
      * Summary:     "You are a helpful assistant that produces concise
                     summaries."
      * Glossary:    "You are a helpful assistant. Answer with strict
                     JSON only."

    We detect the translation call by the presence of "translator" in the
    prompt (unique to that path); the two "helpful assistant" prompts are
    used for the two auxiliary calls.
    """
    return 'translator' in (prompt or '').lower()


def _is_glossary_call(prompt):
    return 'json' in (prompt or '').lower()


def _is_summary_call(prompt):
    return ('helpful assistant' in (prompt or '').lower()
            and 'json' not in (prompt or '').lower())


class FakeEngine:
    """Minimal engine stub compatible with NovelTranslator expectations."""

    name = 'FakeEngine'
    request_attempt = 2
    source_lang = 'English'
    target_lang = 'Italian'

    def __init__(self, translate_side_effect=None):
        self.prompt = 'original prompt'
        self.translate_calls = []
        self._translate_side_effect = translate_side_effect

    def override_prompt(self, prompt):
        self._stash = self.prompt
        self.prompt = prompt

    def restore_prompt(self):
        if hasattr(self, '_stash'):
            self.prompt = self._stash

    def get_target_lang(self):
        return self.target_lang

    def translate(self, text):
        self.translate_calls.append({
            'prompt': self.prompt, 'text': text})
        if self._translate_side_effect is not None:
            value = self._translate_side_effect(text, self.prompt)
            if isinstance(value, Exception):
                raise value
            return value
        return text


class TestPromptComposition(unittest.TestCase):
    """A prompt typed in the settings dialog must work as plain prose.

    No placeholder may be mandatory, and a stray brace -- which any
    hand-written prompt is likely to contain -- must not break the run.
    """

    def setUp(self):
        self.cache = Mock()
        self.cache.get_info.return_value = None
        self.ctx = ContextManager(self.cache).load()

    def _translator(self, config=None):
        translator = NovelTranslator(
            FakeEngine(), [], self.ctx, self.cache, config=config or {})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_shipped_prompts_are_literal(self):
        # The glossary prompt used to be filled with str.format(), which
        # forced its JSON example to double every brace. It is filled by
        # literal substitution now, so the braces must reach the model
        # exactly as the model has to reproduce them.
        self.assertNotIn('{{', DEFAULT_NOVEL_GLOSSARY_PROMPT)
        self.assertIn('{"entities": []}', DEFAULT_NOVEL_GLOSSARY_PROMPT)
        # The context block belongs last: everything before it is
        # identical from one chapter to the next, and so stays reusable
        # from the provider's prefix cache.
        self.assertTrue(
            DEFAULT_NOVEL_TRANSLATION_PROMPT.rstrip().endswith('{context}'))

    def test_plain_prose_prompt_still_receives_context(self):
        translator = self._translator(
            {'novel_translation_prompt': 'Translate plainly and drily.'})
        prompt = translator._translation_system_prompt('SUMMARY + GLOSSARY')

        self.assertIn('Translate plainly and drily.', prompt)
        self.assertIn('SUMMARY + GLOSSARY', prompt)
        # The languages are supplied too, since the prompt named neither.
        self.assertIn('English', prompt)
        self.assertIn('Italian', prompt)

    def test_placed_context_is_not_appended_twice(self):
        translator = self._translator({
            'novel_translation_prompt':
                'From <slang> to <tlang>.\n\n{context}\n\nBe brief.'})
        prompt = translator._translation_system_prompt('RUNNING CONTEXT')

        self.assertEqual(1, prompt.count('RUNNING CONTEXT'))
        # The prompt named the languages itself, so no directive is added.
        self.assertTrue(prompt.startswith('From English to Italian.'))
        self.assertTrue(prompt.rstrip().endswith('Be brief.'))

    def test_stray_braces_survive(self):
        translator = self._translator()
        composed = translator._compose_prompt(
            'Summarise. Keep {names} and {{quirks}} intact.',
            {'{text}': ('Chapter text:', 'CHAPTER BODY')},
            required=('{text}',))

        self.assertIn('{names}', composed)
        self.assertIn('{{quirks}}', composed)
        self.assertIn('CHAPTER BODY', composed)

    def test_missing_values_are_appended_with_their_label(self):
        translator = self._translator()
        composed = translator._compose_prompt(
            'List the named entities as JSON.',
            {
                '{existing_keys}': ('Existing entries:', 'Aslan, Narnia'),
                '{source_text}': ('Source:', 'THE SOURCE'),
                '{translated_text}': ('Translation:', 'THE TRANSLATION'),
            },
            required=(
                '{existing_keys}', '{source_text}', '{translated_text}'))

        self.assertIn('Existing entries:\nAslan, Narnia', composed)
        self.assertIn('Source:\nTHE SOURCE', composed)
        self.assertIn('Translation:\nTHE TRANSLATION', composed)

    def test_empty_values_are_not_appended(self):
        translator = self._translator()
        composed = translator._compose_prompt(
            'Summarise.', {'{text}': ('Chapter text:', '')},
            required=('{text}',))

        self.assertEqual('Summarise.', composed)


class TestMarkerShapes(unittest.TestCase):
    def test_marker_and_text_on_one_line(self):
        parsed = parse_tagged_response(
            '[1] «Ciao», disse.\n[2] Poi tacque.\n\n[3]\nTerzo.', [1, 2, 3])
        self.assertEqual(
            {1: '«Ciao», disse.', 2: 'Poi tacque.', 3: 'Terzo.'}, parsed)

    def test_bold_markers(self):
        parsed = parse_tagged_response(
            '**[1]**\nUno.\n\n**[2]** Due.', [1, 2])
        self.assertEqual({1: 'Uno.', 2: 'Due.'}, parsed)

    def test_a_reply_numbered_from_one_is_matched_by_order(self):
        # A retry asks for paragraphs 34 to 36 under those numbers; the
        # model numbers what it was given from 1.
        parsed = parse_tagged_response(
            '[1]\nA.\n\n[2]\nB.\n\n[3]\nC.', [34, 35, 36])
        self.assertEqual({34: 'A.', 35: 'B.', 36: 'C.'}, parsed)

    def test_no_renumbering_when_some_numbers_match(self):
        parsed = parse_tagged_response(
            '[1]\nA.\n\n[35]\nB.', [34, 35, 36])
        self.assertEqual({35: 'B.'}, parsed)


class TestEmptyMarkers(unittest.TestCase):
    def test_marker_without_a_body_is_missing(self):
        # Stored as '', it blanked the paragraph in the output ebook and
        # the alignment retry never asked for it.
        parsed = parse_tagged_response('[1]\n\n[2]\nTesto.', [1, 2])
        self.assertEqual({2: 'Testo.'}, parsed)

    def test_a_later_empty_duplicate_does_not_erase_the_first(self):
        parsed = parse_tagged_response('[1]\nTesto.\n\n[1]\n', [1])
        self.assertEqual({1: 'Testo.'}, parsed)


class TestNovelTranslator(unittest.TestCase):
    def setUp(self):
        self.cache = Mock()
        self.cache.get_info.return_value = None
        self.ctx = ContextManager(self.cache).load()
        # Reset mock so we can inspect only calls made during the test proper.
        self.cache.reset_mock()

        self.paragraphs = [
            make_paragraph(0, 'Alpha 1', page='a'),
            make_paragraph(1, 'Alpha 2', page='a'),
            make_paragraph(2, 'Beta 1', page='b'),
        ]
        self.chapters = [
            Chapter(1, 'Chapter One', ['a'], self.paragraphs[:2]),
            Chapter(2, 'Chapter Two', ['b'], self.paragraphs[2:]),
        ]

    def _make_translator(self, engine, config=None):
        # Disable the short-chapter guard by default so tests can use tiny
        # synthetic paragraphs without their summary/glossary calls being
        # short-circuited by ``novel_min_chars_for_context``.
        base_config = {'novel_min_chars_for_context': 0}
        if config:
            base_config.update(config)
        translator = NovelTranslator(
            engine, self.chapters, self.ctx, self.cache,
            config=base_config)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_run_translates_all_chapters(self):
        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            # Summary path.
            return 'Summary text.'
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(engine)
        count = translator.run()
        self.assertEqual(2, count)
        # Both chapters were persisted.
        self.assertEqual(2, self.ctx.get_progress())
        # All non-ignored paragraphs got translated.
        for p in self.paragraphs:
            self.assertIsNotNone(p.translation)
            self.assertIn('(IT)', p.translation)
            self.assertEqual('FakeEngine', p.engine_name)
            self.assertEqual('Italian', p.target_lang)

    def test_run_resumes_from_progress(self):
        # Simulate a previous run that completed chapter 1.
        self.ctx.progress = 1
        collected = []

        def side_effect(text, prompt):
            collected.append(text[:20])
            if _is_translation_call(prompt):
                return _echo_markers(text)
            return '{"entities": []}'
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(engine)
        count = translator.run()
        self.assertEqual(1, count)
        # Only chapter 2 paragraph should be translated in this run.
        self.assertIsNone(self.paragraphs[0].translation)
        self.assertIsNone(self.paragraphs[1].translation)
        self.assertIsNotNone(self.paragraphs[2].translation)

    def test_paragraphs_already_translated_are_not_sent_again(self):
        """What an interrupted run left in the cache is not paid for twice.

        Progress only advances at the end of a chapter, so resuming
        re-enters a chapter whose first chunks were already stored.
        """
        self.paragraphs[0].translation = 'Alpha 1 (IT)'
        self.paragraphs[0].engine_name = 'FakeEngine'
        self.paragraphs[0].target_lang = 'Italian'
        translation_calls = []
        context_calls = []

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                translation_calls.append(text)
                return _echo_markers(text)
            context_calls.append(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            return 'Summary text.'

        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(
            engine, {'novel_context_timing': 'after'})
        translator.run()

        # The model was asked for the second paragraph only.
        self.assertNotIn('Alpha 1', translation_calls[0])
        self.assertIn('Alpha 2', translation_calls[0])
        # The stored translation was left alone.
        self.assertEqual('Alpha 1 (IT)', self.paragraphs[0].translation)
        # And the summary still read the whole chapter, the paragraph
        # that came from the cache included.
        self.assertIn('Alpha 1 (IT)', context_calls[0])

    def test_reuse_translated_paragraphs_can_be_turned_off(self):
        self.paragraphs[0].translation = 'Alpha 1 (IT)'
        self.paragraphs[0].engine_name = 'FakeEngine'
        self.paragraphs[0].target_lang = 'Italian'
        translation_calls = []

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                translation_calls.append(text)
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            return 'Summary text.'

        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(
            engine, config={'novel_reuse_translated_paragraphs': False})
        translator.run()

        self.assertIn('Alpha 1', translation_calls[0])

    def test_fully_translated_chapter_only_needs_its_context(self):
        """A chapter cancelled between its last chunk and its summary
        asks for the summary alone when it resumes."""
        for paragraph in self.paragraphs[:2]:
            paragraph.translation = '%s (IT)' % paragraph.original
            paragraph.engine_name = 'FakeEngine'
            paragraph.target_lang = 'Italian'
        translation_calls = []
        context_calls = []

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                translation_calls.append(text)
                return _echo_markers(text)
            context_calls.append(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            return 'Summary text.'

        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(engine)
        translator.run()

        # Chapter one asked for nothing but its context; chapter two,
        # which has no translation yet, was translated as usual.
        self.assertEqual(1, len(translation_calls))
        self.assertIn('Beta 1', translation_calls[0])
        self.assertIn('Alpha 1 (IT)', context_calls[0])

    def test_a_chunk_writes_its_own_paragraphs_in_one_batch(self):
        """Each chunk commits once, and only what it produced."""
        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            return 'Summary text.'

        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(
            engine, config={'novel_max_paragraphs_per_chunk': 1})
        translator.run()

        batches = [c.args[0] for c
                   in self.cache.update_paragraphs.call_args_list]
        self.assertEqual(
            [['Alpha 1'], ['Alpha 2'], ['Beta 1']],
            [[p.original for p in batch] for batch in batches])
        self.cache.update_paragraph.assert_not_called()

    def test_chunk_budget_is_capped_by_the_model_reply_limit(self):
        """A chunk is never longer than the model can answer."""
        engine = FakeEngine()
        engine.model_max_output_tokens = 4096
        translator = self._make_translator(
            engine, config={'novel_chunk_tokens': 16000})
        # What fits under a 4096-token reply once the reply's own
        # scaffolding and the growth of a translation are taken out.
        self.assertEqual(
            int((4096 - 1500) / 2.2), translator._effective_chunk_tokens())

        # A model that writes more than the cap in the settings is held
        # to the cap; with both out of the way the budget stands.
        engine.model_max_output_tokens = 128000
        translator = self._make_translator(
            engine, config={'novel_chunk_tokens': 16000})
        self.assertEqual(
            int((16384 - 1500) / 2.2), translator._effective_chunk_tokens())
        translator = self._make_translator(engine, config={
            'novel_chunk_tokens': 16000, 'novel_reply_max_tokens': 0})
        self.assertEqual(16000, translator._effective_chunk_tokens())

        # And it can be turned off.
        engine.model_max_output_tokens = 4096
        translator = self._make_translator(engine, config={
            'novel_chunk_tokens': 16000,
            'novel_output_aware_chunking': False})
        self.assertEqual(16000, translator._effective_chunk_tokens())

    def test_an_impossible_reply_limit_is_not_believed(self):
        engine = FakeEngine()
        engine.model_max_output_tokens = 943718
        translator = self._make_translator(engine)
        self.assertEqual(0, translator.model_output_limit)
        engine.model_max_output_tokens = 65536
        self.assertEqual(65536, translator.model_output_limit)

    def test_unknown_reply_limit_leaves_the_budget_to_the_cap(self):
        engine = FakeEngine()
        translator = self._make_translator(
            engine, config={'novel_chunk_tokens': 16000})
        self.assertEqual(0, translator.model_output_limit)
        self.assertEqual(16384, translator.reply_room_limit)
        self.assertEqual(
            int((16384 - 1500) / 2.2), translator._effective_chunk_tokens())
        translator = self._make_translator(engine, config={
            'novel_chunk_tokens': 16000, 'novel_reply_max_tokens': 0})
        self.assertEqual(0, translator.reply_room_limit)
        self.assertEqual(16000, translator._effective_chunk_tokens())

    def test_cancel_stops_run(self):
        engine = FakeEngine()
        translator = self._make_translator(engine)
        translator.set_cancel_request(lambda: True)
        with self.assertRaises(TranslationCanceled):
            translator.run()

    def _engine_that_never_returns_the_second_paragraph(self):
        # Marker 2 never comes back: not from the alignment retries, and
        # not from the second pass either, which asks for that paragraph
        # alone and gets an empty reply.
        def side_effect(text, prompt):
            if not _is_translation_call(prompt):
                return '{"entities": []}'
            # Two paragraphs asked: the first comes back. One asked, at
            # any number: nothing does.
            both = '[1]' in text and '[2]' in text
            return '[1]\nOnly first.' if both else ''
        return FakeEngine(translate_side_effect=side_effect)

    def test_missing_paragraphs_stop_the_run_by_default(self):
        engine = self._engine_that_never_returns_the_second_paragraph()
        translator = self._make_translator(engine)
        log = Mock()
        translator.set_logging(log)
        with self.assertRaises(TranslationFailed) as raised:
            translator.run()
        self.assertIn('positions 2', str(raised.exception))
        # What the model did translate is in the cache...
        self.assertEqual('Only first.', self.paragraphs[0].translation)
        # ...the chapter is not recorded as done, so a resume asks for
        # the missing paragraph again instead of skipping past it...
        self.assertEqual(0, self.ctx.get_progress())
        # ...and the missing paragraph was tried once more on its own,
        # in a smaller chunk, before giving up.
        self.assertTrue(any(
            'smaller chunks' in (c.args[0] if c.args else '')
            for c in log.call_args_list))
        second_pass = [c for c in engine.translate_calls
                       if _is_translation_call(c['prompt'])
                       and '[2]' not in c['text']]
        self.assertTrue(second_pass)

    def test_missing_paragraphs_can_be_skipped(self):
        engine = self._engine_that_never_returns_the_second_paragraph()
        translator = self._make_translator(
            engine, config={'novel_on_missing_paragraphs': 'continue'})
        log = Mock()
        translator.set_logging(log)
        translator.run()
        self.assertEqual('Only first.', self.paragraphs[0].translation)
        self.assertIsNone(self.paragraphs[1].translation)
        # Progress advanced over the hole, as asked.
        self.assertEqual(2, self.ctx.get_progress())
        self.assertTrue(any(
            'could not be translated' in (c.args[0] if c.args else '')
            for c in log.call_args_list))

    def test_chunks_are_sized_by_the_reply_limit_not_collapsed_by_it(self):
        # A model that writes 4096 tokens gets chunks of about 1180
        # source tokens, what a 4096-token reply can hold. The reserve
        # for the running context is input and must not be taken off
        # the reply limit as well: it used to be, and with the default
        # reserve every chunk came out at the 200-token floor, one
        # paragraph each.
        paragraphs = [
            make_paragraph(i, 'word ' * 30, page='a') for i in range(120)]
        self.chapters = [Chapter(1, 'Long', ['a'], paragraphs)]
        engine = FakeEngine(translate_side_effect=lambda text, prompt:
                            _echo_markers(text)
                            if _is_translation_call(prompt)
                            else '{"entities": []}')
        engine.model_max_output_tokens = 4096
        translator = self._make_translator(engine, config={
            'novel_chunk_tokens': 16000,
            'novel_max_paragraphs_per_chunk': 0})
        translator.run()
        chunk_calls = [c for c in engine.translate_calls
                       if _is_translation_call(c['prompt'])]
        # 120 paragraphs of 37 tokens, 31 to a chunk: four chunks.
        self.assertEqual(4, len(chunk_calls))
        self.assertTrue(all(p.translation for p in paragraphs))

    def test_effective_budget_subtracts_the_reserve_once(self):
        engine = FakeEngine()
        translator = self._make_translator(engine, config={
            'novel_chunk_tokens': 16000, 'novel_reply_max_tokens': 0})
        self.assertEqual(16000 - 4840,
                         translator._effective_chunk_tokens(4840))
        # The reply room is a ceiling on that, not something the
        # reserve is subtracted from again.
        engine.model_max_output_tokens = 4096
        translator = self._make_translator(
            engine, config={'novel_chunk_tokens': 16000})
        self.assertEqual(int((4096 - 1500) / 2.2),
                         translator._effective_chunk_tokens(4840))

    def test_the_two_sizes_agree(self):
        # The room asked for a chunk sized under the reply limit is the
        # reply limit, never more: the numbers come from one pair.
        engine = FakeEngine()
        engine.model_max_output_tokens = 8192
        translator = self._make_translator(engine)
        source = translator.source_for_reply(8192)
        self.assertLessEqual(translator.reply_for_source(source), 8192)

    def test_the_sizing_is_said_once_at_the_start(self):
        engine = FakeEngine(translate_side_effect=lambda text, prompt:
                            _echo_markers(text)
                            if _is_translation_call(prompt)
                            else '{"entities": []}')
        engine.model_max_output_tokens = 8192
        translator = self._make_translator(engine)
        translator.run()
        lines = [c.args[0] for c in translator.log.call_args_list if c.args]
        sizing = [line for line in lines if line.startswith('Sizing:')]
        self.assertEqual(1, len(sizing))
        self.assertIn('8192', sizing[0])
        self.assertIn('50 paragraphs', sizing[0])

    def test_a_chunk_asks_for_room_to_reply(self):
        # Sent without max_tokens, a provider applied its own default
        # of 4096 and a chunk came back cut at a third.
        seen = []

        def side_effect(text, prompt):
            seen.append(engine.max_tokens)
            return _echo_markers(text) if _is_translation_call(prompt) \
                else '{"entities": []}'
        engine = FakeEngine(translate_side_effect=side_effect)
        engine.max_tokens = 0
        translator = self._make_translator(engine)
        translator.run()
        chunk_calls = [m for m in seen if m]
        self.assertTrue(chunk_calls)
        self.assertTrue(all(1500 < m <= 16384 for m in chunk_calls))
        # Put back afterwards.
        self.assertEqual(0, engine.max_tokens)

    def test_reply_room_respects_a_limit_set_by_the_user(self):
        seen = []

        def side_effect(text, prompt):
            seen.append(engine.max_tokens)
            return _echo_markers(text) if _is_translation_call(prompt) \
                else '{"entities": []}'
        engine = FakeEngine(translate_side_effect=side_effect)
        engine.max_tokens = 2048
        translator = self._make_translator(engine)
        translator.run()
        self.assertTrue(all(m == 2048 for m in seen))

    def test_reply_room_can_be_turned_off(self):
        seen = []

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                seen.append(engine.max_tokens)
                return _echo_markers(text)
            return '{"entities": []}'
        engine = FakeEngine(translate_side_effect=side_effect)
        engine.max_tokens = 0
        translator = self._make_translator(
            engine, config={'novel_reply_max_tokens': 0})
        translator.run()
        self.assertTrue(seen)
        self.assertTrue(all(m == 0 for m in seen))

    def test_auxiliary_paragraphs_are_translated_before_the_chapters(self):
        aux = [
            make_paragraph(10, 'The Book Title', page='content.opf'),
            make_paragraph(11, 'Chapter One', page='toc.ncx'),
            make_paragraph(12, 'For my mother', page='dedication'),
            make_paragraph(13, 'Already done', page='dedication'),
        ]
        aux[3].translation = 'Gia fatto'
        aux[3].engine_name = 'FakeEngine'
        aux[3].target_lang = 'Italian'
        calls = []

        def side_effect(text, prompt):
            calls.append(prompt)
            if _is_translation_call(prompt):
                return _echo_markers(text)
            return '{"entities": []}'
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = NovelTranslator(
            engine, self.chapters, self.ctx, self.cache,
            config={'novel_min_chars_for_context': 0}, aux_paragraphs=aux)
        translator.set_logging(Mock())
        translator.run()

        self.assertEqual('The Book Title (IT)', aux[0].translation)
        self.assertEqual('Chapter One (IT)', aux[1].translation)
        self.assertEqual('For my mother (IT)', aux[2].translation)
        # The one the cache already held was not sent again.
        self.assertEqual('Gia fatto', aux[3].translation)
        first = engine.translate_calls[0]['text']
        self.assertIn('The Book Title', first)
        self.assertNotIn('Already done', first)
        # In one request, with no running context, ahead of chapter 1.
        self.assertIn('For my mother', first)
        self.assertNotIn('Alpha 1', first)
        self.assertEqual(2, self.ctx.get_progress())

    def test_summary_and_glossary_persisted(self):
        # The default asks for both in one reply.
        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            return ('Here is JSON: {"summary": "This chapter introduces '
                    'Alpha.", "entities": ['
                    '{"source": "Alpha", "translation": "Alfa", '
                    '"type": "character"}]}')
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(engine)
        translator.run()
        summaries = self.ctx.get_summaries()
        self.assertEqual(2, len(summaries))
        self.assertIn('Alpha', summaries[0]['summary'])
        glossary = self.ctx.get_glossary()
        self.assertIn('Alpha', glossary)
        self.assertEqual('Alfa', glossary['Alpha']['translation'])

    def test_summary_and_glossary_persisted_in_two_calls(self):
        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return ('Here is JSON: {"entities": ['
                        '{"source": "Alpha", "translation": "Alfa", '
                        '"type": "character"}]}')
            # Summary path.
            return 'This chapter introduces Alpha.'
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(
            engine, {'novel_combined_context_call': False})
        translator.run()
        summaries = self.ctx.get_summaries()
        self.assertIn('Alpha', summaries[0]['summary'])
        self.assertEqual('Alfa', self.ctx.get_glossary()['Alpha'][
            'translation'])

    def test_no_translatable_content_skips_chapter(self):
        # Chapter 1 contains only an ignored paragraph.
        self.chapters[0] = Chapter(1, 'Empty', ['a'], [
            make_paragraph(0, '', page='a', ignored=True)])

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            return '{"entities": []}'
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(engine)
        translator.run()
        # Progress still advances past chapter 1.
        self.assertEqual(2, self.ctx.get_progress())

    def test_short_chapters_skip_context_calls(self):
        # With the default guard, tiny chapters ("Alpha 1", "Alpha 2", ...)
        # must be translated but must NOT trigger summary / glossary calls.
        tracker = {'summary': 0, 'glossary': 0}

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                tracker['glossary'] += 1
                return '{"entities": []}'
            # Assume any other call is the summary request.
            tracker['summary'] += 1
            return 'Summary text.'

        engine = FakeEngine(translate_side_effect=side_effect)
        # Explicit threshold well above the tiny sample paragraphs.
        translator = self._make_translator(
            engine, config={'novel_min_chars_for_context': 500})
        translator.run()
        # Both chapters are shorter than 500 chars once translated: no
        # summary / glossary calls should be made.
        self.assertEqual(0, tracker['summary'])
        self.assertEqual(0, tracker['glossary'])
        # But paragraphs must still be translated.
        for p in self.paragraphs:
            self.assertIsNotNone(p.translation)

    def test_glossary_fallback_from_line_based(self):
        # LLM returns a non-JSON list; the fallback parser should
        # recover some entries.
        long_text = ' '.join(['Alpha'] * 200)  # push chapter over threshold
        self.paragraphs = [
            make_paragraph(0, long_text, page='a'),
            make_paragraph(1, long_text, page='b'),
        ]
        self.chapters = [
            Chapter(1, 'Chapter One', ['a'], self.paragraphs[:1]),
            Chapter(2, 'Chapter Two', ['b'], self.paragraphs[1:]),
        ]

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                # Return plain prose, not JSON.
                return (
                    'Here are the entities I found:\n'
                    '"Alpha" -> "Alfa" (character): main figure\n'
                    '"Beta" -> "Beta" (place)\n'
                    'Nothing else notable.')
            return 'Summary text.'

        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(engine)
        translator.run()
        glossary = self.ctx.get_glossary()
        # At least one entity should have been recovered via the fallback.
        self.assertGreater(len(glossary), 0)

    def test_head_short_text_unchanged(self):
        # Short text under the budget is returned verbatim.
        engine = FakeEngine()
        translator = self._make_translator(engine)
        text = 'This is short.'
        self.assertEqual(text, translator._head(text, 100))

    def test_head_long_text_truncated(self):
        engine = FakeEngine()
        translator = self._make_translator(engine)
        text = 'HEAD_START' + ('x' * 5000) + 'TAIL_END'
        clipped = translator._head(text, 500)
        self.assertLess(len(clipped), len(text))
        self.assertEqual(len(clipped), 500)
        self.assertIn('HEAD_START', clipped)
        self.assertNotIn('TAIL_END', clipped)
        self.assertNotIn('middle omitted', clipped)

    def test_head_zero_disables(self):
        # max_chars <= 0 disables truncation.
        engine = FakeEngine()
        translator = self._make_translator(engine)
        text = 'x' * 100000
        self.assertEqual(text, translator._head(text, 0))

    def test_translation_failure_after_retries(self):
        engine = FakeEngine(
            translate_side_effect=lambda t, p: Exception('boom'))
        translator = self._make_translator(engine)
        with patch(f'{module_name}.time'):
            with self.assertRaises(TranslationFailed):
                translator.run()

    # -- overlap chunking -------------------------------------------------

    def test_overlap_default_is_five(self):
        # Default from configuration is 5 sliding paragraphs.
        engine = FakeEngine()
        translator = self._make_translator(engine, config={})
        # _make_translator forces novel_min_chars_for_context=0 but not
        # the overlap; it should fall back to the runtime default.
        self.assertEqual(5, translator.overlap_paragraphs)

    def test_overlap_can_be_disabled(self):
        engine = FakeEngine()
        translator = self._make_translator(
            engine, config={'novel_overlap_paragraphs': 0})
        self.assertEqual(0, translator.overlap_paragraphs)

    def test_overlap_negative_clamped_to_zero(self):
        engine = FakeEngine()
        translator = self._make_translator(
            engine, config={'novel_overlap_paragraphs': -5})
        self.assertEqual(0, translator.overlap_paragraphs)

    def test_overlap_passed_to_next_chunk(self):
        """When overlap > 0, the second chunk must receive as context the
        translations produced by the first chunk."""
        # Build a chapter with enough paragraphs to force 2 chunks
        # (max_paragraphs=3 forces 3+3 = 2 chunks).
        paras = [make_paragraph(i, 'para %d' % i, page='a')
                 for i in range(6)]
        self.chapters = [Chapter(1, 'Ch', ['a'], paras)]
        self.paragraphs = paras

        seen_users = []

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                seen_users.append(text)
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            return 'Summary.'

        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(
            engine, config={
                'novel_overlap_paragraphs': 2,
                'novel_max_paragraphs_per_chunk': 3,
            })
        translator.run()

        # We expect at least 2 translation calls (2 chunks).
        translation_calls = [t for t in seen_users
                             if 'Translate each numbered' in t]
        self.assertGreaterEqual(len(translation_calls), 2)
        # First chunk: no overlap block.
        first = translation_calls[0]
        self.assertNotIn('already translated', first)
        # Second chunk: overlap block present with previous translations.
        second = translation_calls[1]
        self.assertIn('already translated', second)
        # The overlap must contain the translated form of at least one
        # paragraph from the first chunk (which was echoed with '(IT)'),
        # and must come before the paragraphs to translate so the two
        # blocks cannot be confused. The rules themselves are first, so
        # that the part of the request that never changes can be served
        # from the provider's prompt cache.
        self.assertIn('(IT)', second.split('Source paragraphs:')[0])

    def test_overlap_disabled_no_context_block(self):
        """With overlap=0 no chunk should carry a context block, even the
        second one."""
        paras = [make_paragraph(i, 'para %d' % i, page='a')
                 for i in range(6)]
        self.chapters = [Chapter(1, 'Ch', ['a'], paras)]
        self.paragraphs = paras

        seen_users = []

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                seen_users.append(text)
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            return 'Summary.'

        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(
            engine, config={
                'novel_overlap_paragraphs': 0,
                'novel_max_paragraphs_per_chunk': 3,
            })
        translator.run()

        for t in seen_users:
            self.assertNotIn('already translated', t)

    def test_overlap_larger_than_previous_chunk_is_truncated(self):
        """If overlap window is larger than the number of translations
        available from the previous chunk, we simply use what we have."""
        paras = [make_paragraph(i, 'para %d' % i, page='a')
                 for i in range(4)]
        self.chapters = [Chapter(1, 'Ch', ['a'], paras)]
        self.paragraphs = paras

        seen_users = []

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                seen_users.append(text)
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            return 'Summary.'

        engine = FakeEngine(translate_side_effect=side_effect)
        # Ask for 10 paragraphs of overlap but each chunk only has 2.
        translator = self._make_translator(
            engine, config={
                'novel_overlap_paragraphs': 10,
                'novel_max_paragraphs_per_chunk': 2,
            })
        translator.run()

        # The run must complete without error, and the second chunk must
        # still include a context block (with just 2 paragraphs, not 10).
        translation_calls = [t for t in seen_users
                             if 'Translate each numbered' in t]
        self.assertGreaterEqual(len(translation_calls), 2)
        # Every paragraph must have been translated.
        for p in paras:
            self.assertIsNotNone(p.translation)
            self.assertIn('(IT)', p.translation)

    @patch(f'{module_name}.time')
    def test_retry_sleeps_between_attempts(self, mock_time):
        engine = FakeEngine(
            translate_side_effect=lambda t, p: Exception('nope'))
        translator = self._make_translator(engine)
        # request_attempt=2 -> two attempts, one sleep in between.
        with self.assertRaises(TranslationFailed):
            translator._translate_with_retry('sys', 'user', attempts=2)
        mock_time.sleep.assert_called()


# ---------------------------------------------------------------------------
# Structured output (JSON) path
# ---------------------------------------------------------------------------


class StructuredEngine(FakeEngine):
    """FakeEngine that pretends to support structured JSON output.

    Overrides ``translate`` so it *invokes* ``get_body`` (or the swapped
    ``get_body_for_structured``) exactly once per call, giving tests a
    reliable hook to assert which path was chosen.
    """
    structured_output_mode = 'schema'

    def __init__(self, translate_side_effect=None):
        super().__init__(translate_side_effect=translate_side_effect)
        self.body_calls = []
        self.structured_body_calls = []

    def get_body(self, text):
        self.body_calls.append(text)
        return '{"messages": ["marker-path"]}'

    def get_body_for_structured(self, text, schema=None):
        self.structured_body_calls.append({
            'text': text, 'schema': schema})
        return '{"structured": true, "text": "..."}'

    def translate(self, text):
        # Force the routing hook to actually run: build the body via
        # whatever get_body is currently swapped in on the instance,
        # then let the parent class dispatch the response.
        self.get_body(text)
        return super().translate(text)


class UnstructuredEngine(FakeEngine):
    """FakeEngine that does NOT advertise structured output support."""
    structured_output_mode = None


def _echo_markers_as_json(text, suffix=' (IT)'):
    """Test helper: parse the JSON payload embedded in ``text`` (produced
    by ``_build_structured_payload``) and echo it back with translated
    fields, simulating a well-behaved server returning JSON.
    """
    import json as _json
    import re
    # The payload is embedded after "Input:\n" in the user text.
    m = re.search(r'Input:\s*\n(\{.*\})\s*\Z', text, re.DOTALL)
    if not m:
        return '{"paragraphs": []}'
    try:
        obj = _json.loads(m.group(1))
    except ValueError:
        return '{"paragraphs": []}'
    out = {'paragraphs': []}
    for p in obj.get('paragraphs', []):
        out['paragraphs'].append({
            'n': p['n'],
            'translation': (p.get('source') or '').strip() + suffix,
        })
    return _json.dumps(out, ensure_ascii=False)


class TestStructuredOutputCapability(unittest.TestCase):
    """Detection of engine structured-output capability + user setting."""

    def _make_translator(self, engine, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        translator = NovelTranslator(
            engine, [], ctx, cache, config=config or {})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_structured_setting_default_is_auto(self):
        translator = self._make_translator(StructuredEngine())
        self.assertEqual('auto', translator.structured_output_setting)

    def test_structured_setting_normalises_invalid_value(self):
        translator = self._make_translator(
            StructuredEngine(),
            config={'novel_structured_output': 'nonsense'})
        self.assertEqual('auto', translator.structured_output_setting)

    def test_engine_supports_structured_detection_true(self):
        translator = self._make_translator(StructuredEngine())
        self.assertTrue(translator._engine_supports_structured())

    def test_engine_supports_structured_detection_false(self):
        translator = self._make_translator(UnstructuredEngine())
        self.assertFalse(translator._engine_supports_structured())

    def test_structured_active_auto_capable_engine(self):
        translator = self._make_translator(StructuredEngine())
        self.assertTrue(translator._structured_active())

    def test_structured_active_auto_uncapable_engine(self):
        translator = self._make_translator(UnstructuredEngine())
        self.assertFalse(translator._structured_active())

    def test_structured_active_off_overrides_capability(self):
        translator = self._make_translator(
            StructuredEngine(),
            config={'novel_structured_output': 'off'})
        self.assertFalse(translator._structured_active())

    def test_structured_active_force_overrides_missing_capability(self):
        translator = self._make_translator(
            UnstructuredEngine(),
            config={'novel_structured_output': 'force'})
        self.assertTrue(translator._structured_active())

    def test_structured_choice_logged_once(self):
        # The verbose one-shot log should fire exactly once per instance.
        engine = StructuredEngine()
        translator = self._make_translator(engine)
        log = Mock()
        translator.set_logging(log)
        translator._structured_active()
        translator._structured_active()
        translator._structured_active()
        format_logs = [
            c for c in log.call_args_list
            if 'Output format' in (c.args[0] if c.args else '')]
        self.assertEqual(1, len(format_logs))
        self.assertIn('structured JSON', format_logs[0].args[0])

    def test_structured_choice_logs_reason_when_disabled(self):
        translator = self._make_translator(
            StructuredEngine(),
            config={'novel_structured_output': 'off'})
        log = Mock()
        translator.set_logging(log)
        translator._structured_active()
        format_logs = [
            c for c in log.call_args_list
            if 'Output format' in (c.args[0] if c.args else '')]
        self.assertEqual(1, len(format_logs))
        message = format_logs[0].args[0]
        self.assertIn('text markers', message)
        self.assertIn('disabled by user setting', message)


class TestStructuredOutputParser(unittest.TestCase):
    """Robustness of _parse_structured_response."""

    def _make_translator(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        translator = NovelTranslator(
            StructuredEngine(), [], ctx, cache, config={})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_parse_basic(self):
        translator = self._make_translator()
        response = ('{"paragraphs": ['
                    '{"n": 1, "translation": "Primo."},'
                    '{"n": 2, "translation": "Secondo."}]}')
        parsed = translator._parse_structured_response(response, [1, 2])
        self.assertEqual({1: 'Primo.', 2: 'Secondo.'}, parsed)

    def test_parse_ignores_extra_fields(self):
        translator = self._make_translator()
        response = ('{"paragraphs": ['
                    '{"n": 1, "translation": "Primo.", '
                    '"source": "orig", "extra": "junk"}]}')
        parsed = translator._parse_structured_response(response, [1])
        self.assertEqual({1: 'Primo.'}, parsed)

    def test_parse_filters_unexpected_indices(self):
        translator = self._make_translator()
        response = ('{"paragraphs": ['
                    '{"n": 1, "translation": "Real."},'
                    '{"n": 99, "translation": "Hallucinated."}]}')
        parsed = translator._parse_structured_response(response, [1, 2])
        self.assertEqual({1: 'Real.'}, parsed)

    def test_parse_missing_paragraphs(self):
        translator = self._make_translator()
        response = '{"paragraphs": [{"n": 1, "translation": "Only one."}]}'
        parsed = translator._parse_structured_response(response, [1, 2, 3])
        self.assertEqual({1: 'Only one.'}, parsed)

    def test_parse_json_with_prose_prefix(self):
        translator = self._make_translator()
        response = ('Here is your JSON: {"paragraphs": ['
                    '{"n": 1, "translation": "T"}]} thanks!')
        parsed = translator._parse_structured_response(response, [1])
        self.assertEqual({1: 'T'}, parsed)

    def test_parse_json_with_markdown_fence(self):
        translator = self._make_translator()
        response = ('```json\n{"paragraphs": ['
                    '{"n": 1, "translation": "T"}]}\n```')
        parsed = translator._parse_structured_response(response, [1])
        self.assertEqual({1: 'T'}, parsed)

    def test_parse_n_as_string(self):
        # Some models emit "n": "1" instead of "n": 1.
        translator = self._make_translator()
        response = '{"paragraphs": [{"n": "1", "translation": "Uno"}]}'
        parsed = translator._parse_structured_response(response, [1])
        self.assertEqual({1: 'Uno'}, parsed)

    def test_parse_malformed_json_returns_empty(self):
        translator = self._make_translator()
        parsed = translator._parse_structured_response(
            'this is not json', [1])
        self.assertEqual({}, parsed)

    def test_parse_missing_paragraphs_field_returns_empty(self):
        translator = self._make_translator()
        parsed = translator._parse_structured_response(
            '{"other": "field"}', [1])
        self.assertEqual({}, parsed)

    def test_parse_empty_translation_skipped(self):
        translator = self._make_translator()
        response = ('{"paragraphs": ['
                    '{"n": 1, "translation": ""},'
                    '{"n": 2, "translation": "OK"}]}')
        parsed = translator._parse_structured_response(response, [1, 2])
        # Empty translation is dropped; the alignment retry will fill it.
        self.assertEqual({2: 'OK'}, parsed)

    def test_parse_renumbered_reply_by_order(self):
        translator = self._make_translator()
        response = ('{"paragraphs": ['
                    '{"n": 1, "translation": "A"},'
                    '{"n": 2, "translation": "B"}]}')
        parsed = translator._parse_structured_response(response, [40, 41])
        self.assertEqual({40: 'A', 41: 'B'}, parsed)

    def test_parse_preserves_inline_placeholders(self):
        # {id_00001} placeholders must survive the JSON round-trip.
        translator = self._make_translator()
        response = ('{"paragraphs": [{"n": 1, '
                    '"translation": "Testo con {id_00001} preservato."}]}')
        parsed = translator._parse_structured_response(response, [1])
        self.assertIn('{id_00001}', parsed[1])

    def test_parse_salvages_truncated_response(self):
        # A response cut off at the model's output limit never closes its
        # braces: the entries that did complete must still be kept, so
        # only the tail has to be asked for again.
        translator = self._make_translator()
        response = ('{"paragraphs": ['
                    '{"n": 1, "translation": "Primo."},'
                    '{"n": 2, "translation": "Secondo."},'
                    '{"n": 3, "transl')
        parsed = translator._parse_structured_response(response, [1, 2, 3])
        self.assertEqual({1: 'Primo.', 2: 'Secondo.'}, parsed)

    def test_parse_salvage_ignores_echoed_source(self):
        translator = self._make_translator()
        response = ('{"paragraphs": ['
                    '{"n": 1, "source": "First.", "translation": "Primo."},'
                    '{"n": 2, "source": "Second.", "transl')
        parsed = translator._parse_structured_response(response, [1, 2])
        self.assertEqual({1: 'Primo.'}, parsed)

    def test_parse_salvages_entries_under_an_unexpected_key(self):
        translator = self._make_translator()
        response = '{"items": [{"n": 1, "translation": "Primo."}]}'
        parsed = translator._parse_structured_response(response, [1])
        self.assertEqual({1: 'Primo.'}, parsed)

    def test_parse_warns_when_response_is_not_a_clean_object(self):
        translator = self._make_translator()
        translator._parse_structured_response(
            '{"paragraphs": [{"n": 1, "translation": "Primo."},{"n": 2',
            [1, 2])
        messages = [
            call.args[0] for call in translator.log.call_args_list]
        # The message names what was lost, so a reader can tell a model
        # that drops its last entry from one that runs out of output.
        self.assertTrue(
            any('Incomplete JSON reply' in message and 'missing [2]' in message
                for message in messages), messages)


class TestStructuredDispatcher(unittest.TestCase):
    """The dispatcher routes to the structured path when active."""

    def _make_pair(self, engine_cls, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        base_config = {'novel_min_chars_for_context': 0}
        if config:
            base_config.update(config)
        paragraphs = [
            make_paragraph(0, 'First paragraph.', page='a'),
            make_paragraph(1, 'Second paragraph.', page='a'),
        ]
        chapter = Chapter(1, 'C1', ['a'], paragraphs)

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                # Both marker path and structured path arrive here.
                # Return the appropriate format based on the request body.
                if '"structured"' in text or 'JSON' in text.upper() \
                        or 'Input:' in text:
                    return _echo_markers_as_json(text)
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return '{"entities": []}'
            return 'Summary.'

        engine = engine_cls(translate_side_effect=side_effect)
        translator = NovelTranslator(
            engine, [chapter], ctx, cache, config=base_config)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator, engine, paragraphs

    def test_dispatcher_calls_structured_path_when_engine_supports(self):
        translator, engine, paragraphs = self._make_pair(StructuredEngine)
        translator.run()
        # get_body_for_structured must have been called at least once.
        self.assertGreater(len(engine.structured_body_calls), 0)
        # All paragraphs must be translated.
        for p in paragraphs:
            self.assertIsNotNone(p.translation)
            self.assertIn('(IT)', p.translation)

    def test_dispatcher_uses_markers_when_engine_not_capable(self):
        translator, engine, paragraphs = self._make_pair(UnstructuredEngine)
        translator.run()
        # UnstructuredEngine does not override get_body_for_structured;
        # it inherits FakeEngine (which doesn't track structured calls).
        for p in paragraphs:
            self.assertIsNotNone(p.translation)
            self.assertIn('(IT)', p.translation)

    def test_dispatcher_respects_off_setting(self):
        translator, engine, paragraphs = self._make_pair(
            StructuredEngine,
            config={'novel_structured_output': 'off'})
        translator.run()
        # Engine supports structured but user forced it off: no structured
        # calls should have been made.
        self.assertEqual(0, len(engine.structured_body_calls))

    def test_dispatcher_respects_force_setting(self):
        # UnstructuredEngine doesn't declare capability, but 'force'
        # asks the pipeline to try structured anyway. Since
        # UnstructuredEngine's get_body_for_structured falls back to
        # get_body (via GenAI default), the call still succeeds.
        translator, engine, paragraphs = self._make_pair(
            UnstructuredEngine,
            config={'novel_structured_output': 'force'})
        # Sanity: the structured path is chosen.
        self.assertTrue(translator._structured_active())


class TestStructuredEnginePayloads(unittest.TestCase):
    """Verify each engine builds the correct provider-specific body.

    These tests exercise the real engine classes and therefore need the
    ``calibre_plugins.novel_translator`` package to be importable. They
    are skipped in isolated dev environments (where only ``lib/novel.py``
    is loaded standalone) but run normally under ``calibre-debug``.
    """

    @classmethod
    def _can_import_engines(cls):
        try:
            import calibre_plugins.novel_translator.engines.openai  # noqa
            import calibre_plugins.novel_translator.engines.google  # noqa
            return True
        except ImportError:
            return False

    def setUp(self):
        if not self._can_import_engines():
            self.skipTest(
                'engines module not importable in isolated environment')

    def test_openai_response_format_json_schema(self):
        # Import lazily to avoid loading engine chain at module scope.
        from calibre_plugins.novel_translator.engines.openai import (
            ChatgptTranslate)
        engine = ChatgptTranslate.__new__(ChatgptTranslate)
        engine.model = 'gpt-x'
        engine.prompt = 'You translate.'
        engine.stream = True
        engine.samplings = ['temperature']
        engine.sampling = 'temperature'
        engine.temperature = 0.5
        engine.top_p = 1.0
        engine.source_lang = 'English'
        engine.target_lang = 'Italian'
        # Minimal method stubs needed by get_body_for_structured.
        engine.get_prompt = lambda: 'You translate.'

        schema = {'type': 'object', 'properties': {'x': {'type': 'string'}}}
        body_str = engine.get_body_for_structured('hello', schema=schema)
        import json as _json
        body = _json.loads(body_str)
        self.assertEqual('json_schema', body['response_format']['type'])
        self.assertEqual(
            'novel_translation',
            body['response_format']['json_schema']['name'])
        self.assertEqual(
            schema, body['response_format']['json_schema']['schema'])
        self.assertTrue(
            body['response_format']['json_schema'].get('strict'))
        # Streaming stays on so the SSE stream keeps the connection alive
        # during the long prefill phase; the caller reassembles the chunks.
        self.assertTrue(body['stream'])
        # Reasoning is left to the engine preference, not forced off.
        self.assertNotIn('reasoning_effort', body)

    def test_openai_response_format_json_object_when_no_schema(self):
        from calibre_plugins.novel_translator.engines.openai import (
            ChatgptTranslate)
        engine = ChatgptTranslate.__new__(ChatgptTranslate)
        engine.model = 'gpt-x'
        engine.prompt = 'You translate.'
        engine.stream = False
        engine.samplings = ['temperature']
        engine.sampling = 'temperature'
        engine.temperature = 0.5
        engine.top_p = 1.0
        engine.source_lang = 'English'
        engine.target_lang = 'Italian'
        engine.get_prompt = lambda: 'You translate.'

        body_str = engine.get_body_for_structured('hi', schema=None)
        import json as _json
        body = _json.loads(body_str)
        self.assertEqual('json_object', body['response_format']['type'])

    def test_openai_structured_honors_reasoning_effort(self):
        from calibre_plugins.novel_translator.engines.openai import (
            ChatgptTranslate)
        engine = ChatgptTranslate.__new__(ChatgptTranslate)
        engine.model = 'gpt-x'
        engine.prompt = 'You translate.'
        engine.stream = False
        engine.samplings = ['temperature']
        engine.sampling = 'temperature'
        engine.temperature = 0.5
        engine.top_p = 1.0
        engine.source_lang = 'English'
        engine.target_lang = 'Italian'
        engine.get_prompt = lambda: 'You translate.'
        engine.reasoning_effort = 'low'

        import json as _json
        body = _json.loads(engine.get_body_for_structured('hi'))
        self.assertEqual('low', body['reasoning_effort'])
        body = _json.loads(engine.get_body('hi'))
        self.assertEqual('low', body['reasoning_effort'])

    def test_gemini_response_mime_and_schema(self):
        from calibre_plugins.novel_translator.engines.google import (
            GeminiTranslate)
        engine = GeminiTranslate.__new__(GeminiTranslate)
        engine.model = 'gemini-x'
        engine.stream = False
        engine.temperature = 0.5
        engine.top_p = 1.0
        engine.top_k = 40
        engine.source_lang = 'English'
        engine.target_lang = 'Italian'
        # Minimal method stubs used by GeminiTranslate.get_body().
        engine._prompt = lambda text: 'Translate: ' + text

        schema = {'type': 'object', 'properties': {'x': {'type': 'string'}}}
        body_str = engine.get_body_for_structured('hi', schema=schema)
        import json as _json
        body = _json.loads(body_str)
        self.assertEqual(
            'application/json',
            body['generationConfig']['responseMimeType'])
        self.assertEqual(
            schema, body['generationConfig']['responseSchema'])


class ReasoningEngine(FakeEngine):
    """FakeEngine exposing the reasoning knobs of a GenAI engine."""

    reasoning_efforts = ['default', 'none', 'minimal', 'low', 'high']

    def __init__(self, translate_side_effect=None):
        super().__init__(translate_side_effect)
        self.reasoning_effort = 'minimal'
        self.reasoning_max_tokens = 1024
        # (effort, budget) as seen by each request, so a test can tell
        # what was actually sent rather than what was restored after.
        self.seen = []

    def translate(self, text):
        self.seen.append((self.reasoning_effort, self.reasoning_max_tokens))
        return super().translate(text)


class TestContextReasoning(unittest.TestCase):
    """Reasoning is turned down for the summary and glossary calls."""

    def _make_translator(self, engine, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        translator = NovelTranslator(
            engine, [], ctx, cache, config=config or {})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_reasoning_is_off_for_context_calls(self):
        engine = ReasoningEngine()
        translator = self._make_translator(engine)
        translator._translate_context_call('system', 'user', 'summary')
        self.assertEqual([('none', 0)], engine.seen)

    def test_engine_settings_are_restored(self):
        engine = ReasoningEngine()
        translator = self._make_translator(engine)
        translator._translate_context_call('system', 'user', 'summary')
        self.assertEqual('minimal', engine.reasoning_effort)
        self.assertEqual(1024, engine.reasoning_max_tokens)

    def test_engine_settings_are_restored_after_a_failure(self):
        engine = ReasoningEngine(
            translate_side_effect=lambda text, prompt: RuntimeError('boom'))
        translator = self._make_translator(engine)
        with patch('time.sleep'):
            with self.assertRaises(TranslationFailed):
                translator._translate_context_call('system', 'user', 'summary')
        self.assertEqual('minimal', engine.reasoning_effort)
        self.assertEqual(1024, engine.reasoning_max_tokens)

    def test_reasoning_is_kept_when_the_user_allows_it(self):
        engine = ReasoningEngine()
        translator = self._make_translator(
            engine, {'novel_context_reasoning': True})
        translator._translate_context_call('system', 'user', 'summary')
        self.assertEqual([('minimal', 1024)], engine.seen)

    def test_reasoning_is_never_introduced(self):
        # An engine that sends no reasoning field must keep sending none:
        # providers that reject the parameter would fail on it.
        engine = ReasoningEngine()
        engine.reasoning_effort = 'default'
        engine.reasoning_max_tokens = 0
        translator = self._make_translator(engine)
        translator._translate_context_call('system', 'user', 'summary')
        self.assertEqual([('default', 0)], engine.seen)

    def test_engine_without_reasoning_support_is_untouched(self):
        engine = FakeEngine()
        translator = self._make_translator(engine)
        self.assertEqual(
            'user',
            translator._translate_context_call('system', 'user', 'summary'))


class CappedEngine(FakeEngine):
    """FakeEngine exposing an output limit, like the OpenRouter engine."""

    def __init__(self, translate_side_effect=None, max_tokens=0):
        super().__init__(translate_side_effect)
        self.max_tokens = max_tokens
        self.seen_caps = []

    def translate(self, text):
        self.seen_caps.append(self.max_tokens)
        return super().translate(text)


class TestGlossaryStaysSmall(unittest.TestCase):
    """A glossary of hundreds of names must not bloat every request.

    Two failures were observed on a real book once the glossary passed a
    couple of hundred entries: the extraction prompt carried the whole
    list of names to skip, and the model answered by copying that list
    back as new entries until it hit its output limit -- 131072 tokens
    on one call.
    """

    def _make_translator(self, engine, config=None, glossary=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        if glossary:
            ctx.append_chapter(1, 't', 's', glossary)
        cache.reset_mock()
        base = {'novel_min_chars_for_context': 0}
        base.update(config or {})
        translator = NovelTranslator(engine, [], ctx, cache, config=base)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_prompt_lists_only_the_names_of_this_chapter(self):
        engine = FakeEngine(
            translate_side_effect=lambda text, prompt: '{"entities": []}')
        translator = self._make_translator(engine, glossary=[
            {'source': 'Fidelma', 'translation': 'Fidelma'},
            {'source': 'Canterbury', 'translation': 'Canterbury'},
        ])
        translator._extract_glossary_updates(
            Chapter(2, 'Two', ['b'], []),
            'Fidelma crossed the courtyard.',
            'Fidelma attraverso il cortile.')

        sent = engine.translate_calls[-1]['text']
        self.assertIn('Fidelma', sent)
        self.assertNotIn('Canterbury', sent)

    def test_the_whole_glossary_is_sent_when_the_filter_is_off(self):
        engine = FakeEngine(
            translate_side_effect=lambda text, prompt: '{"entities": []}')
        translator = self._make_translator(
            engine, {'novel_glossary_relevant_only': False}, glossary=[
                {'source': 'Fidelma', 'translation': 'Fidelma'},
                {'source': 'Canterbury', 'translation': 'Canterbury'},
            ])
        translator._extract_glossary_updates(
            Chapter(2, 'Two', ['b'], []), 'Fidelma walked.', 'Fidelma.')

        self.assertIn('Canterbury', engine.translate_calls[-1]['text'])

    def test_prompt_entry_cap_is_enforced(self):
        engine = FakeEngine(
            translate_side_effect=lambda text, prompt: '{"entities": []}')
        names = ['Name%02d' % i for i in range(10)]
        translator = self._make_translator(
            engine, {'novel_glossary_prompt_max_entries': 3}, glossary=[
                {'source': n, 'translation': n} for n in names])
        translator._extract_glossary_updates(
            Chapter(2, 'Two', ['b'], []), ' '.join(names), ' '.join(names))

        sent = engine.translate_calls[-1]['text']
        listed = [n for n in names if n in sent.split('Source:')[0]]
        self.assertEqual(names[-3:], listed)

    def test_repeated_and_known_entries_are_dropped(self):
        reply = json.dumps({'entities': [
            {'source': 'Fidelma', 'translation': 'Fidelma'},
            {'source': 'Eadulf', 'translation': 'Eadulf'},
            {'source': 'Eadulf', 'translation': 'Eadulf'},
        ]})
        engine = FakeEngine(
            translate_side_effect=lambda text, prompt: reply)
        translator = self._make_translator(engine, glossary=[
            {'source': 'Fidelma', 'translation': 'Fidelma'}])
        updates = translator._extract_glossary_updates(
            Chapter(2, 'Two', ['b'], []),
            'Fidelma and Eadulf.', 'Fidelma ed Eadulf.')

        self.assertEqual(['Eadulf'], [u['source'] for u in updates])

    def test_a_reply_cut_off_mid_object_is_salvaged(self):
        # What the output cap produces: complete entries, then a broken
        # one and no closing brace.
        reply = (
            '{"entities": ['
            '{"source": "Eadulf", "translation": "Eadulf", '
            '"type": "character", "notes": ""}, '
            '{"source": "Bieda", "translation": "Bieda", '
            '"type": "character", "notes": ""}, '
            '{"source": "Sebbi", "transl')
        engine = FakeEngine(
            translate_side_effect=lambda text, prompt: reply)
        translator = self._make_translator(engine)
        updates = translator._extract_glossary_updates(
            Chapter(2, 'Two', ['b'], []), 'Source.', 'Traduzione.')

        self.assertEqual(
            ['Eadulf', 'Bieda'], [u['source'] for u in updates])

    def test_context_calls_are_capped_and_the_engine_restored(self):
        engine = CappedEngine()
        translator = self._make_translator(engine)
        translator._translate_context_call('system', 'user', 'glossary')

        self.assertEqual([8000], engine.seen_caps)
        self.assertEqual(0, engine.max_tokens)

    def test_a_tighter_user_cap_is_left_alone(self):
        engine = CappedEngine(max_tokens=512)
        translator = self._make_translator(engine)
        translator._translate_context_call('system', 'user', 'glossary')

        self.assertEqual([512], engine.seen_caps)
        self.assertEqual(512, engine.max_tokens)

    def test_the_cap_can_be_disabled(self):
        engine = CappedEngine()
        translator = self._make_translator(
            engine, {'novel_context_max_tokens': 0})
        translator._translate_context_call('system', 'user', 'glossary')

        self.assertEqual([0], engine.seen_caps)

    def test_an_engine_without_an_output_limit_is_untouched(self):
        engine = FakeEngine()
        translator = self._make_translator(engine)
        self.assertEqual(
            'user',
            translator._translate_context_call('system', 'user', 'glossary'))


class TestSummaryClipping(unittest.TestCase):
    """A summary is re-read in every later prompt, so it must stay short."""

    def _make_translator(self, engine, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        base = {'novel_min_chars_for_context': 0}
        base.update(config or {})
        translator = NovelTranslator(engine, [], ctx, cache, config=base)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_a_normal_summary_is_stored_verbatim(self):
        summary = 'Fidelma indaga. ' * 20
        engine = FakeEngine(
            translate_side_effect=lambda text, prompt: summary)
        translator = self._make_translator(engine)
        stored = translator._generate_summary(
            Chapter(1, 'One', ['a'], []), 'Testo tradotto.')

        self.assertEqual(summary.strip(), stored)

    def test_a_runaway_summary_is_truncated_at_a_sentence(self):
        # The failure seen on a real book: asked for 150-350 words, the
        # model answered with the whole translated chapter.
        engine = FakeEngine(
            translate_side_effect=lambda text, prompt: 'Frase lunga. ' * 2000)
        translator = self._make_translator(
            engine, {'novel_summary_tokens': 100})
        stored = translator._generate_summary(
            Chapter(1, 'One', ['a'], []), 'Testo tradotto.')

        self.assertLessEqual(len(stored), 800)  # 100 tokens * 4 * 2
        self.assertTrue(stored.endswith('.'), stored[-40:])
        self.assertTrue(
            any('crowd out' in c.args[0]
                for c in translator.log.call_args_list))

    def test_the_limit_can_be_set_explicitly(self):
        engine = FakeEngine(
            translate_side_effect=lambda text, prompt: 'x' * 5000)
        translator = self._make_translator(
            engine, {'novel_summary_max_chars': 600})
        stored = translator._generate_summary(
            Chapter(1, 'One', ['a'], []), 'Testo tradotto.')

        self.assertEqual(600, len(stored))


class TestGlossarySchema(unittest.TestCase):
    """The extraction call is constrained by the engine when it can be."""

    class SchemaEngine(FakeEngine):
        structured_output_mode = 'schema'

    def _make_translator(self, engine, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        base = {'novel_min_chars_for_context': 0}
        base.update(config or {})
        translator = NovelTranslator(engine, [], ctx, cache, config=base)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_the_entities_schema_is_sent_to_a_capable_engine(self):
        translator = self._make_translator(self.SchemaEngine())
        with patch.object(
                translator, '_translate_with_retry_structured',
                return_value='{"entities": []}') as structured:
            translator._extract_glossary_updates(
                Chapter(1, 'One', ['a'], []), 'Source.', 'Traduzione.')

        sent = structured.call_args.kwargs['schema']
        # The class schema, with the per-chapter limit on the list.
        self.assertEqual(50, sent['properties']['entities'].pop('maxItems'))
        self.assertEqual(NovelTranslator._GLOSSARY_RESPONSE_SCHEMA, sent)

    def test_an_engine_without_structured_support_is_asked_in_prose(self):
        translator = self._make_translator(FakeEngine())
        with patch.object(
                translator, '_translate_with_retry_structured') as structured:
            with patch.object(
                    translator, '_translate_with_retry',
                    return_value='{"entities": []}') as plain:
                translator._extract_glossary_updates(
                    Chapter(1, 'One', ['a'], []), 'Source.', 'Traduzione.')

        structured.assert_not_called()
        plain.assert_called_once()


class TestCombinedContextCall(unittest.TestCase):
    """Summary and glossary in one request instead of two."""

    def setUp(self):
        self.cache = Mock()
        self.cache.get_info.return_value = None
        self.ctx = ContextManager(self.cache).load()
        self.cache.reset_mock()
        self.paragraphs = [
            make_paragraph(0, 'Alpha walked home.', page='a'),
            make_paragraph(1, 'Beta followed.', page='b'),
        ]
        self.chapters = [
            Chapter(1, 'Chapter One', ['a'], self.paragraphs[:1]),
            Chapter(2, 'Chapter Two', ['b'], self.paragraphs[1:]),
        ]

    def _make_translator(self, engine, config=None):
        base = {'novel_min_chars_for_context': 0}
        base.update(config or {})
        translator = NovelTranslator(
            engine, self.chapters, self.ctx, self.cache, config=base)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def _engine(self):
        reply = json.dumps({
            'summary': 'Alpha cammina.',
            'entities': [{'source': 'Alpha', 'translation': 'Alpha',
                          'type': 'character', 'notes': ''}]})

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            return reply
        return FakeEngine(translate_side_effect=side_effect)

    def _aux_calls(self, engine):
        return [c for c in engine.translate_calls
                if not _is_translation_call(c['prompt'])]

    def test_one_chapter_costs_one_auxiliary_call(self):
        engine = self._engine()
        translator = self._make_translator(engine)
        translator._translate_chapter(self.chapters[0])

        self.assertEqual(1, len(self._aux_calls(engine)))
        summary = self.ctx.get_summaries()[0]['summary']
        self.assertEqual('Alpha cammina.', summary)
        self.assertIn('Alpha', self.ctx.get_glossary())

    def test_the_chapter_text_travels_once(self):
        engine = self._engine()
        translator = self._make_translator(engine)
        translator._generate_context(
            self.chapters[0], 'Alpha walked home.', 'Alpha cammina a casa.')

        sent = self._aux_calls(engine)[0]['text']
        # Source and translation, one copy each -- where the two
        # separate calls sent the translation twice.
        self.assertEqual(1, sent.count('Alpha walked home.'))
        self.assertEqual(1, sent.count('Alpha cammina a casa.'))

    def test_the_structural_prompts_are_not_settings(self):
        # A prompt typed for the summary or the glossary used to be a
        # setting, and broke the shape the code parses more often than
        # it helped. Whatever a configuration still carries is ignored.
        engine = self._engine()
        translator = self._make_translator(
            engine, {'novel_summary_prompt': 'Riassumi in due righe.',
                     'novel_glossary_prompt': 'Elenca i nomi.',
                     'novel_context_prompt': 'Tutto insieme.'})
        translator._translate_chapter(self.chapters[0])

        aux = self._aux_calls(engine)
        self.assertEqual(1, len(aux))
        sent = ' '.join(c['text'] + c['prompt'] for c in aux)
        for typed in ('Riassumi in due righe.', 'Elenca i nomi.',
                      'Tutto insieme.'):
            self.assertNotIn(typed, sent)

    def test_the_last_chapter_skips_the_call(self):
        engine = self._engine()
        translator = self._make_translator(engine)
        translator._translate_chapter(self.chapters[-1])

        self.assertEqual([], self._aux_calls(engine))
        self.assertTrue(
            any('last one' in c.args[0]
                for c in translator.log.call_args_list))

    def test_the_last_chapter_can_keep_its_context(self):
        engine = self._engine()
        translator = self._make_translator(
            engine, {'novel_skip_context_last_chapter': False})
        translator._translate_chapter(self.chapters[-1])

        self.assertEqual(1, len(self._aux_calls(engine)))

    def test_a_truncated_reply_keeps_the_summary_and_the_entries(self):
        # The summary is written first, so it survives a reply cut off
        # inside the entity list.
        reply = (
            '{"summary": "Alpha cammina fino a casa.", "entities": ['
            '{"source": "Alpha", "translation": "Alpha", "type": '
            '"character", "notes": ""}, {"source": "Bet')

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers(text)
            return reply
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(engine)
        summary, entities = translator._generate_context(
            self.chapters[0], 'Alpha walked home.', 'Alpha cammina.')

        self.assertEqual('Alpha cammina fino a casa.', summary)
        self.assertEqual(['Alpha'], [e['source'] for e in entities])

    def test_shipped_combined_prompt_is_literal(self):
        self.assertNotIn('{{', DEFAULT_NOVEL_CONTEXT_PROMPT)
        self.assertIn('"summary"', DEFAULT_NOVEL_CONTEXT_PROMPT)
        self.assertIn('"narrative"', DEFAULT_NOVEL_CONTEXT_PROMPT)

    def _engine_reporting(self, narrative):
        reply = json.dumps({
            'narrative': narrative,
            'summary': 'A list of the other books by the author.',
            'entities': [{'source': 'Hachette', 'translation': 'Hachette',
                          'type': 'organization', 'notes': ''}]})
        return FakeEngine(translate_side_effect=lambda text, prompt:
                          _echo_markers(text)
                          if _is_translation_call(prompt) else reply)

    def test_a_chapter_outside_the_story_leaves_no_context(self):
        # The case seen on a real book: "Also by ..." and the copyright
        # page were summarised and carried into every later prompt.
        engine = self._engine_reporting(narrative=False)
        translator = self._make_translator(engine)
        translator._translate_chapter(self.chapters[0])

        self.assertEqual(
            '', self.ctx.get_summaries()[0]['summary'])
        self.assertNotIn('Hachette', self.ctx.get_glossary())
        self.assertTrue(any(
            'not part of the story' in (c.args[0] if c.args else '')
            for c in translator.log.call_args_list))
        # The chapter itself is done and counted.
        self.assertEqual(1, self.ctx.get_progress())

    def test_a_story_chapter_keeps_its_context(self):
        engine = self._engine_reporting(narrative=True)
        translator = self._make_translator(engine)
        translator._translate_chapter(self.chapters[0])
        self.assertIn('other books', self.ctx.get_summaries()[0]['summary'])
        self.assertIn('Hachette', self.ctx.get_glossary())

    def test_a_reply_cut_before_the_summary_asks_for_it_again(self):
        # The names came first and the reply was cut at the output
        # limit: the entries are kept and the summary is asked on its
        # own instead of leaving a hole in every later prompt.
        calls = []

        def side_effect(text, prompt):
            calls.append(prompt)
            if _is_translation_call(prompt):
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return ('{"narrative": true, "entities": [{"source": '
                        '"Alpha", "translation": "Alpha", "type": '
                        '"character", "notes": ""}], "summary": "Alph')
            return 'Riassunto a parte.'
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(engine)
        translator._translate_chapter(self.chapters[0])
        self.assertEqual(
            'Riassunto a parte.', self.ctx.get_summaries()[0]['summary'])
        self.assertIn('Alpha', self.ctx.get_glossary())
        self.assertTrue(any(_is_summary_call(p) for p in calls))

    def test_a_missing_flag_is_read_as_story(self):
        # A model that ignored the field is no reason to drop a summary.
        engine = self._engine()
        translator = self._make_translator(engine)
        translator._translate_chapter(self.chapters[0])
        self.assertEqual('Alpha cammina.', self.ctx.get_summaries()[0]['summary'])

    def test_the_check_can_be_turned_off(self):
        engine = self._engine_reporting(narrative=False)
        translator = self._make_translator(
            engine, {'novel_context_narrative_only': False})
        translator._translate_chapter(self.chapters[0])
        self.assertIn('other books', self.ctx.get_summaries()[0]['summary'])
        self.assertIn('Hachette', self.ctx.get_glossary())

    def test_the_separate_summary_call_answers_the_same_way(self):
        calls = []

        def side_effect(text, prompt):
            calls.append(prompt)
            if _is_translation_call(prompt):
                return _echo_markers(text)
            if _is_glossary_call(prompt):
                return ('{"entities": [{"source": "Hachette", '
                        '"translation": "Hachette"}]}')
            return 'Not a story chapter.'
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = self._make_translator(
            engine, {'novel_combined_context_call': False,
                     'novel_context_timing': 'after'})
        translator._translate_chapter(self.chapters[0])

        self.assertEqual('', self.ctx.get_summaries()[0]['summary'])
        # No glossary call was made for a chapter outside the story.
        self.assertFalse(any(_is_glossary_call(p) for p in calls))
        self.assertNotIn('Hachette', self.ctx.get_glossary())


class TestExtractJsonString(unittest.TestCase):
    def test_a_field_of_a_broken_object_is_recovered(self):
        self.assertEqual(
            'Una frase.',
            _extract_json_string('{"summary": "Una frase.", "entities": [{',
                                 'summary'))

    def test_escapes_are_decoded(self):
        self.assertEqual(
            'Dice "ciao".',
            _extract_json_string('{"summary": "Dice \\"ciao\\".", "e',
                                 'summary'))

    def test_a_truncated_field_yields_nothing(self):
        self.assertEqual(
            '', _extract_json_string('{"summary": "Una fra', 'summary'))

    def test_a_missing_field_yields_nothing(self):
        self.assertEqual(
            '', _extract_json_string('{"entities": []}', 'summary'))


class TestRequestTimingLog(unittest.TestCase):
    """Every request is bracketed by a sent/received log line."""

    def _make_translator(self, engine):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        translator = NovelTranslator(engine, [], ctx, cache, config={})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_request_and_response_are_logged_with_the_label(self):
        translator = self._make_translator(FakeEngine())
        translator._translate_with_retry(
            'system', 'user text', label='chapter 3 chunk 1/2')
        messages = [c.args[0] for c in translator.log.call_args_list]
        self.assertTrue(
            any('chapter 3 chunk 1/2' in m and 'sent' in m
                for m in messages), messages)
        self.assertTrue(
            any('chapter 3 chunk 1/2' in m and 's.' in m
                for m in messages), messages)


# ---------------------------------------------------------------------------
# Dialogue punctuation
# ---------------------------------------------------------------------------


class TestDetectDialogueStyle(unittest.TestCase):
    """The source is the only thing that knows how the book punctuates
    speech, and it has to be asked once for the whole book: asking per
    chapter is exactly what produced guillemets in one chapter and
    straight quotes in the next.
    """

    def test_guillemets_win_over_a_stray_quotation(self):
        texts = ['«Buongiorno», disse Anna.'] * 10 \
            + ['He called it a "problem".'] + ['Il sole era alto.'] * 20
        style = detect_dialogue_style(texts)
        self.assertEqual('«', style['primary'])
        self.assertIsNone(style['nested'])
        self.assertFalse(style['dash'])

    def test_a_nested_mark_is_reported_separately(self):
        texts = ['"Hello," she said, "he told me \u2018go away\u2019."'] * 8
        style = detect_dialogue_style(texts)
        self.assertEqual('"', style['primary'])
        self.assertEqual('\u2018', style['nested'])

    def test_dash_dialogue_is_detected(self):
        texts = ['\u2014Hola \u2014dijo Juan.'] * 12 \
            + ['El sol estaba alto.'] * 20
        style = detect_dialogue_style(texts)
        self.assertTrue(style['dash'])
        self.assertEqual(12, style['dash_paragraphs'])

    def test_a_dash_inside_a_sentence_is_not_dialogue(self):
        texts = ['The house \u2014 the old one \u2014 was empty.'] * 30
        self.assertFalse(detect_dialogue_style(texts)['dash'])

    def test_prose_without_dialogue_prescribes_nothing(self):
        style = detect_dialogue_style(['Plain narrative prose.'] * 30)
        self.assertIsNone(style['primary'])
        self.assertEqual('', dialogue_instruction(style))

    def test_a_handful_of_marks_is_noise_not_a_convention(self):
        style = detect_dialogue_style(['She said "no".'] * 2)
        self.assertIsNone(style['primary'])

    def test_the_instruction_names_the_marks(self):
        text = dialogue_instruction(
            detect_dialogue_style(['«Ciao», disse.'] * 10))
        self.assertIn('«…»', text)
        self.assertIn('every chapter', text)


class TestDetectBookDialogueStyle(unittest.TestCase):
    """A preface that quotes at length must not decide for the novel."""

    def test_most_chapters_win_not_most_marks(self):
        # Eighty straight marks in the preface against forty guillemets
        # in the novel: the raw count picked the preface.
        preface = ['The critic wrote: "a masterpiece", "unmatched".'] * 40
        novel = [['«Buongiorno», disse Anna.'] * 8 + ['Prosa.'] * 10] * 5
        style = detect_book_dialogue_style([preface] + novel)
        self.assertEqual('«', style['primary'])
        self.assertEqual(5, style['votes'])
        self.assertEqual(6, style['voters'])
        # The counts come from the chapters that follow the convention,
        # so the preface's marks do not turn up as a nested pair.
        self.assertIsNone(style['nested'])
        self.assertNotIn('"', style['counts'])

    def test_dash_dialogue_can_win_the_vote(self):
        chapters = [['\u2014Hola \u2014dijo Juan.'] * 12
                    + ['El sol estaba alto.'] * 20] * 3
        chapters.append(['She said "no" and "never".'] * 20)
        style = detect_book_dialogue_style(chapters)
        self.assertTrue(style['dash'])
        self.assertIsNone(style['primary'])
        self.assertEqual(3, style['votes'])

    def test_a_book_without_dialogue_votes_for_nothing(self):
        style = detect_book_dialogue_style([['Prose.'] * 10] * 4)
        self.assertIsNone(style['primary'])
        self.assertFalse(style['dash'])
        self.assertEqual(0, style['voters'])

    def test_a_single_chapter_is_read_as_before(self):
        style = detect_book_dialogue_style([['«Ciao», disse.'] * 10])
        self.assertEqual('«', style['primary'])
        self.assertEqual(1, style['votes'])


class TestDialogueConventions(unittest.TestCase):
    def test_every_convention_yields_an_instruction_naming_its_pair(self):
        for key, convention in DIALOGUE_CONVENTIONS.items():
            with self.subTest(convention=key):
                text = dialogue_instruction(dialogue_convention_style(key))
                self.assertIn('every chapter', text)
                if convention['primary']:
                    self.assertIn(convention['primary'][0], text)
                self.assertIn(convention['nested'][0], text)
                if convention['dash']:
                    self.assertIn('dash', text)

    def test_reversed_guillemets_are_not_looked_up_in_the_marks(self):
        text = dialogue_instruction(
            dialogue_convention_style('reversed_guillemets'))
        self.assertIn('»…«', text)
        self.assertIn('›…‹', text)

    def test_unknown_key_is_none(self):
        self.assertIsNone(dialogue_convention_style('nonsense'))


class TestDialogueRules(unittest.TestCase):
    def _translator(self, config=None, chapters=()):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        translator = NovelTranslator(
            FakeEngine(), list(chapters), ctx, cache, config=config or {})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def _chapter(self, texts):
        return Chapter(
            index=1, title='One', page_ids=['a'],
            paragraphs=[make_paragraph(i, text)
                        for i, text in enumerate(texts)])

    def test_the_rule_is_read_off_the_source_and_reaches_the_prompt(self):
        translator = self._translator(
            chapters=[self._chapter(['«Ciao», disse.'] * 10)])
        prompt = translator._translation_system_prompt('CONTEXT')
        self.assertIn('«…»', prompt)

    def test_the_rule_can_be_turned_off(self):
        translator = self._translator(
            {'novel_dialogue_convention': 'off'},
            chapters=[self._chapter(['«Ciao», disse.'] * 10)])
        self.assertEqual('', translator.dialogue_rules)
        self.assertNotIn('«…»', translator._translation_system_prompt('C'))

    def test_a_convention_from_the_settings_replaces_detection(self):
        translator = self._translator(
            {'novel_dialogue_convention': 'guillemets'},
            chapters=[self._chapter(['"Hi," she said.'] * 10)])
        self.assertIn('«…»', translator.dialogue_rules)
        self.assertNotIn('straight', translator.dialogue_rules)
        self.assertEqual('guillemets', translator.dialogue_convention)

    def test_an_unknown_convention_falls_back_to_detection(self):
        translator = self._translator(
            {'novel_dialogue_convention': 'nonsense'},
            chapters=[self._chapter(['"Hi," she said.'] * 10)])
        self.assertEqual('auto', translator.dialogue_convention)
        self.assertIn('"…"', translator.dialogue_rules)

    def test_a_rule_from_the_settings_replaces_the_detected_one(self):
        translator = self._translator(
            {'novel_dialogue_rules': 'Always use « guillemets ».'},
            chapters=[self._chapter(['"Hi," she said.'] * 10)])
        self.assertEqual(
            'Always use « guillemets ».', translator.dialogue_rules)

    def test_the_source_is_read_once(self):
        translator = self._translator(
            chapters=[self._chapter(['«Ciao», disse.'] * 10)])
        first = translator.dialogue_rules
        translator.chapters = []
        self.assertEqual(first, translator.dialogue_rules)


# ---------------------------------------------------------------------------
# Author brief
# ---------------------------------------------------------------------------


class SearchingEngine(FakeEngine):
    """A FakeEngine that records which body builder each request went
    through, with one alternative builder for the body-swap tests.

    ``get_body_for_search`` builds on ``get_body``, exactly as every real
    alternative builder does: it takes the normal body apart and adds to
    it. A double that returned a constant instead would not notice a
    body swap that makes the two call each other.
    """

    request_timeout = 30.0

    def __init__(self, reply):
        FakeEngine.__init__(self)
        self.reply = reply
        self.bodies = []

    def get_body(self, text):
        return 'plain'

    def get_body_for_search(self, text):
        return 'search+' + self.get_body(text)

    def translate(self, text):
        self.bodies.append(self.get_body(text))
        return self.reply


BRIEF = (
    'Calvino writes a limpid, ironic prose, with long paratactic '
    'sentences and a light narrative voice that never raises itself.')


class TestAuthorBrief(unittest.TestCase):
    """One request per book, and every way it can go wrong ends with no
    brief rather than with an invented one."""

    def _translator(self, config=None, engine=None, style=None):
        cache = Mock()
        stored = {INFO_NOVEL_STYLE: style} if style else {}
        cache.get_info.side_effect = stored.get
        ctx = ContextManager(cache).load()
        config = dict(config or {})
        config.setdefault('novel_book_author', 'Italo Calvino')
        config.setdefault('novel_book_title', 'Il barone rampante')
        # The recognition step has tests of its own below.
        config.setdefault('novel_author_check', False)
        translator = NovelTranslator(
            engine or SearchingEngine(BRIEF), [], ctx, cache, config=config)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_model_only_never_searches(self):
        translator = self._translator({'novel_author_style': 'model'})
        translator._ensure_author_style()
        self.assertEqual(['plain'], translator.translator.bodies)

    def _note(self, translator):
        calls = [c for c in translator.cache.set_info.call_args_list
                 if c.args and c.args[0] == INFO_NOVEL_STYLE_NOTE]
        return calls[-1].args[1] if calls else None

    def test_an_admission_of_ignorance_leaves_a_note_for_the_window(self):
        # The tab used to stay empty; it says what happened instead.
        translator = self._translator(
            {'novel_author_style': 'model'},
            engine=SearchingEngine(NO_AUTHOR_INFORMATION))
        self.assertEqual('', translator._ensure_author_style())
        note = self._note(translator)
        self.assertIn('Italo Calvino', note)
        self.assertIn('no reliable information', note)
        self.assertIn('Reset context', note)

    def test_a_brief_replaces_the_note(self):
        translator = self._translator()
        translator.ctx.set_style_note('No brief: earlier attempt.')
        translator._ensure_author_style()
        self.assertEqual('', self._note(translator))
        self.assertEqual(BRIEF, translator.ctx.get_style())

    def test_every_other_way_to_end_without_a_brief_says_why(self):
        cases = (
            ({'novel_author_style': 'off'}, None, 'turned off'),
            ({'novel_book_author': ''}, None, 'no author'),
            ({}, SearchingEngine('Bello.'), 'too little'),
        )
        for config, engine, expected in cases:
            with self.subTest(expected=expected):
                translator = self._translator(config, engine=engine)
                self.assertEqual('', translator._ensure_author_style())
                self.assertIn(expected, self._note(translator))

    def test_the_prompt_asks_for_what_sets_the_author_apart(self):
        engine = FakeEngine(translate_side_effect=lambda text, prompt: BRIEF)
        translator = self._translator(engine=engine)
        self.assertEqual(BRIEF, translator._ensure_author_style())
        sent = engine.translate_calls[-1]['text']
        self.assertIn('Author: Italo Calvino', sent)
        self.assertIn('Book: Il barone rampante', sent)
        self.assertIn('do not search anything', sent)
        self.assertIn('Be specific', sent)
        self.assertIn('from the title alone', sent)
        self.assertIn('leave it out silently', sent)
        self.assertIn('formal from familiar address, end with', sent)
        self.assertNotIn('{author}', sent)
        self.assertNotIn('{excerpt}', sent)
        # No chapters, no excerpt, and no dangling blank at the end.
        self.assertNotIn('Excerpts from this book', sent)
        self.assertTrue(sent.endswith('Translation language: Italian'),
                        sent[-80:])

    def _chapters(self):
        story = ('Cosimo climbed the holm oak and said he would never come '
                 'down again, and his father stood below it shouting. ')
        opening = [
            make_paragraph(0, 'Chapter One'),
            make_paragraph(1, 'Copyright notice {{id_00000}} ' * 10),
            make_paragraph(2, story * 3, ignored=True),
            make_paragraph(3, story * 2)]
        middle = [make_paragraph(10 + i, 'Middle %d. ' % i + story * 2)
                  for i in range(6)]
        return [Chapter(1, 'One', ['a'], opening),
                Chapter(2, 'Two', ['b'], middle)]

    def test_the_request_carries_an_excerpt_of_the_book(self):
        engine = FakeEngine(translate_side_effect=lambda text, prompt: BRIEF)
        translator = self._translator(
            {'novel_author_excerpt_words': 60}, engine=engine)
        translator.chapters = self._chapters()
        self.assertEqual(BRIEF, translator._ensure_author_style())
        sent = engine.translate_calls[-1]['text']
        self.assertIn('Excerpts from this book', sent)
        self.assertIn('[Opening]\nCopyright notice', sent)
        self.assertIn('[From the middle]\nMiddle', sent)
        self.assertNotIn('{{id_', sent)
        self.assertNotIn('Chapter One', sent)
        logged = ' '.join(
            str(c.args[0]) for c in translator.log.call_args_list)
        self.assertIn('words of the book', logged)

    def test_the_excerpt_can_be_turned_off(self):
        engine = FakeEngine(translate_side_effect=lambda text, prompt: BRIEF)
        translator = self._translator(
            {'novel_author_excerpt_words': 0}, engine=engine)
        translator.chapters = self._chapters()
        translator._ensure_author_style()
        self.assertNotIn('Excerpts from this book',
                         engine.translate_calls[-1]['text'])

    def test_the_excerpt_takes_whole_prose_paragraphs_from_two_places(self):
        chapters = self._chapters()
        excerpt = book_excerpt(chapters, 60)
        opening, middle = excerpt.split('\n\n[From the middle]\n')
        # Headings and ignored paragraphs are not prose to show.
        self.assertNotIn('Chapter One', excerpt)
        # Paragraph 3 says it twice; the ignored one would add three.
        self.assertEqual(2, opening.count('holm oak'))
        # About 30 words each, in whole paragraphs; the second stretch
        # starts halfway through the prose, not where the first stopped.
        self.assertTrue(middle.startswith('Middle 2.'), middle)
        self.assertNotIn('Middle 3.', middle)
        # A book too short for two stretches gives one.
        self.assertNotIn('[From the middle]', book_excerpt(chapters, 2000))
        self.assertEqual('', book_excerpt(chapters, 0))
        self.assertEqual('', book_excerpt([], 2000))

    def test_a_setting_stored_as_auto_asks_the_model(self):
        translator = self._translator({'novel_author_style': 'auto'})
        self.assertEqual(BRIEF, translator._ensure_author_style())
        self.assertEqual(['plain'], translator.translator.bodies)

    def test_off_asks_nothing(self):
        translator = self._translator({'novel_author_style': 'off'})
        self.assertEqual('', translator._ensure_author_style())
        self.assertEqual([], translator.translator.bodies)

    def test_a_brief_on_file_is_used_even_when_asking_is_off(self):
        # Written in the Author tab: "Do not ask" is about the model.
        translator = self._translator(
            {'novel_author_style': 'off'}, style=BRIEF)
        self.assertEqual(BRIEF, translator._ensure_author_style())
        self.assertEqual([], translator.translator.bodies)
        self.assertIn(BRIEF, translator._translation_system_prompt('CONTEXT'))

    def test_a_book_without_an_author_asks_nothing(self):
        translator = self._translator({'novel_book_author': ''})
        self.assertEqual('', translator._ensure_author_style())
        self.assertEqual([], translator.translator.bodies)

    def test_a_stored_brief_is_reused_instead_of_researched_again(self):
        translator = self._translator(style=BRIEF)
        self.assertEqual(BRIEF, translator._ensure_author_style())
        self.assertEqual([], translator.translator.bodies)

    def test_an_admission_of_ignorance_is_discarded(self):
        translator = self._translator(
            engine=SearchingEngine(NO_AUTHOR_INFORMATION))
        self.assertEqual('', translator._ensure_author_style())
        self.assertEqual('', translator.ctx.get_style())

    def test_an_answer_too_short_to_be_a_brief_is_discarded(self):
        translator = self._translator(engine=SearchingEngine('Bello.'))
        self.assertEqual('', translator._ensure_author_style())

    @patch('%s.time.sleep' % module_name)
    def test_a_failed_request_does_not_stop_the_translation(self, _sleep):
        engine = SearchingEngine(BRIEF)
        engine.translate = Mock(side_effect=RuntimeError('boom'))
        translator = self._translator(engine=engine)
        self.assertEqual('', translator._ensure_author_style())

    def test_the_brief_is_persisted_and_reaches_the_prompt(self):
        translator = self._translator()
        translator._ensure_author_style()
        translator.cache.set_info.assert_any_call(INFO_NOVEL_STYLE, BRIEF)
        prompt = translator._translation_system_prompt('CONTEXT')
        self.assertIn(BRIEF, prompt)
        # Every chunk is told that the forms of address it settles bind.
        self.assertIn('follow it in every line they exchange', prompt)

    def test_no_brief_leaves_no_hole_in_the_prompt(self):
        translator = self._translator({'novel_author_style': 'off'})
        translator._ensure_author_style()
        prompt = translator._translation_system_prompt('CONTEXT')
        self.assertNotIn('\n\n\n', prompt)
        self.assertTrue(prompt.rstrip().endswith('CONTEXT'))


class TestBodyBuilder(unittest.TestCase):
    """Every alternative request body is built by taking the normal one
    apart and adding to it, so the swap that puts one in place must not
    be visible to the builder itself."""

    def _translator(self, engine):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        translator = NovelTranslator(engine, [], ctx, cache, config={})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def test_a_builder_that_calls_get_body_does_not_recurse(self):
        engine = SearchingEngine('reply')
        translator = self._translator(engine)
        with translator._body_builder(engine.get_body_for_search):
            self.assertEqual('search+plain', engine.get_body('text'))
            # And again: the swap is put back after every call.
            self.assertEqual('search+plain', engine.get_body('text'))

    def test_the_original_builder_is_restored(self):
        engine = SearchingEngine('reply')
        translator = self._translator(engine)
        original = engine.get_body
        with translator._body_builder(engine.get_body_for_search):
            pass
        self.assertEqual(original, engine.get_body)

    def test_it_is_restored_after_a_failure_too(self):
        engine = SearchingEngine('reply')
        translator = self._translator(engine)
        original = engine.get_body
        with self.assertRaises(RuntimeError):
            with translator._body_builder(engine.get_body_for_search):
                raise RuntimeError('boom')
        self.assertEqual(original, engine.get_body)


class TestCollapseBlankLines(unittest.TestCase):
    def test_runs_of_blank_lines_become_one(self):
        self.assertEqual('a\n\nb', collapse_blank_lines('a\n\n\n\nb\n\n'))

    def test_empty_input(self):
        self.assertEqual('', collapse_blank_lines(None))


if __name__ == '__main__':
    unittest.main()


# ---------------------------------------------------------------------------
# Verification of the translations a reply brings back
# ---------------------------------------------------------------------------


class TestSuspiciousTranslations(unittest.TestCase):
    """The checks that tell a translation filed under the wrong number."""

    NARRATIVE = (
        'Claudia lay on her bed and stared up at the ceiling. From the '
        'cookshop below came the smell of boiled cabbage and the noise '
        'of the drinkers.')
    NARRATIVE_IT = (
        'Claudia giaceva sul letto e fissava il soffitto. Dalla cucina '
        'sottostante salivano l\u2019odore di cavolo bollito e il '
        'chiasso dei bevitori.')

    def test_a_sound_reply_raises_no_flag(self):
        sources = {
            1: '\u2018Leave the rest to the Gods.\u2019',
            2: self.NARRATIVE,
            3: 'Horace, Odes, I.9',
            4: 'The drunk swayed. Claudia could see he wasn\u2019t acting.',
        }
        translations = {
            1: '\u00abLascia il resto agli Dei.\u00bb',
            2: self.NARRATIVE_IT,
            3: 'Orazio, Odi, I.9',
            4: 'L\u2019ubriaco barcoll\u00f2. Claudia cap\u00ec che non '
               'stava fingendo.',
        }
        self.assertEqual({}, suspicious_translations(sources, translations))

    def test_an_ellipsis_in_brackets_is_a_hard_sign(self):
        for ellipsis in ('[\u2026]', '[...]', '(\u2026)', '[. . .]'):
            flagged = suspicious_translations(
                {1: self.NARRATIVE},
                {1: 'Claudia giaceva sul letto %s bevitori.' % ellipsis})
            self.assertEqual(('abbreviated', True), flagged.get(1), ellipsis)

    def test_an_ellipsis_the_source_has_is_kept(self):
        flagged = suspicious_translations(
            {1: 'He said [\u2026] and left.'},
            {1: 'Disse [\u2026] e se ne and\u00f2.'})
        self.assertEqual({}, flagged)

    def test_placeholders_must_match_the_source(self):
        sources = {1: '{{id_00000}}Silent they fell.', 2: 'Plain text.'}
        translations = {1: 'Tacquero.', 2: '{{id_00000}}Testo piano.'}
        flagged = suspicious_translations(sources, translations)
        self.assertEqual(('placeholders', True), flagged[1])
        self.assertEqual(('placeholders', True), flagged[2])

    def test_placeholders_tolerate_single_braces_and_spaces(self):
        flagged = suspicious_translations(
            {1: '{{id_00001}} the text {{id_00002}}'},
            {1: '{ id_00001 } il testo {id_00002}'})
        self.assertEqual({}, flagged)

    def test_the_same_long_text_under_two_numbers_is_hard(self):
        sources = {1: self.NARRATIVE, 2: 'The drunk swayed and fell over.'}
        translations = {1: self.NARRATIVE_IT, 2: self.NARRATIVE_IT}
        flagged = suspicious_translations(sources, translations)
        self.assertEqual(('duplicate', True), flagged[1])
        self.assertEqual(('duplicate', True), flagged[2])

    def test_the_same_short_text_is_a_doubt(self):
        sources = {1: '\u2018No.\u2019', 2: '\u2018No,\u2019'}
        translations = {1: '\u00abNo.\u00bb', 2: '\u00abNo.\u00bb'}
        flagged = suspicious_translations(sources, translations)
        self.assertEqual(('duplicate', False), flagged[1])
        self.assertEqual(('duplicate', False), flagged[2])

    def test_identical_sources_may_share_a_translation(self):
        flagged = suspicious_translations(
            {1: '\u2018Yes.\u2019', 2: '\u2018Yes.\u2019'},
            {1: '\u00abS\u00ec.\u00bb', 2: '\u00abS\u00ec.\u00bb'})
        self.assertEqual({}, flagged)

    def test_a_duplicate_of_an_accepted_translation(self):
        sources = {1: self.NARRATIVE, 2: 'The drunk swayed and fell over.'}
        flagged = suspicious_translations(
            sources, {2: self.NARRATIVE_IT}, accepted={1: self.NARRATIVE_IT})
        self.assertEqual({2: ('duplicate', True)}, flagged)

    def test_dialogue_on_one_side_only_is_soft(self):
        flagged = suspicious_translations(
            {1: '\u2018And what does she want with me?\u2019',
             2: 'She put a sandalled foot out as if to move away.'},
            {1: 'Mise un piede calzato di sandalo come per allontanarsi.',
             2: '\u00abE cosa vuole da me?\u00bb'})
        self.assertEqual(('dialogue', False), flagged[1])
        self.assertEqual(('dialogue', False), flagged[2])

    def test_a_dash_opens_dialogue_too(self):
        flagged = suspicious_translations(
            {1: '\u2018Go home,\u2019 she murmured.'},
            {1: '\u2014 Vai a casa, \u2014 mormor\u00f2.'})
        self.assertEqual({}, flagged)

    def test_length_is_measured_against_the_reply(self):
        sources = {
            1: self.NARRATIVE, 2: self.NARRATIVE, 3: self.NARRATIVE,
            4: self.NARRATIVE + ' ' + self.NARRATIVE}
        translations = {
            1: self.NARRATIVE_IT, 2: self.NARRATIVE_IT,
            3: self.NARRATIVE_IT, 4: 'Nessun segno di luce.'}
        flagged = suspicious_translations(sources, translations)
        self.assertEqual({4: ('length', False)}, flagged)

    def test_short_sources_are_not_measured(self):
        flagged = suspicious_translations(
            {1: 'Chapter 4'}, {1: 'Capitolo quattro, in cui si torna a Roma'})
        self.assertEqual({}, flagged)

    def test_a_hard_sign_wins_over_a_soft_one(self):
        flagged = suspicious_translations(
            {1: '\u2018A long line of speech that goes on for a while.\u2019'},
            {1: 'Una lunga battuta [\u2026] per un po\u2019.'})
        self.assertEqual(('abbreviated', True), flagged[1])

    def test_ranges(self):
        self.assertEqual('1-3, 7, 9-10', _ranges([9, 1, 2, 3, 7, 10]))
        self.assertEqual('', _ranges([]))


class TestReplyCheck(unittest.TestCase):
    """How the pipeline acts on what the checks find."""

    NARRATIVE = TestSuspiciousTranslations.NARRATIVE
    NARRATIVE_IT = TestSuspiciousTranslations.NARRATIVE_IT

    def _make_translator(self, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        translator = NovelTranslator(
            StructuredEngine(), [], ctx, cache, config=config or {})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        return translator

    def _paragraphs(self, *texts):
        return [make_paragraph(i, text) for i, text in enumerate(texts)]

    def test_a_hard_sign_is_dropped_and_logged(self):
        translator = self._make_translator()
        paragraphs = self._paragraphs(self.NARRATIVE, self.NARRATIVE)
        parsed = {1: self.NARRATIVE_IT, 2: 'Claudia [\u2026] bevitori.'}
        translator._check_reply(paragraphs, parsed, {}, set(), 'chunk')
        self.assertEqual({1: self.NARRATIVE_IT}, parsed)
        logged = ' '.join(
            str(c.args[0]) for c in translator.log.call_args_list)
        self.assertIn('set aside', logged)
        self.assertIn('shortened with an ellipsis (2)', logged)
        # What was set aside is in the log, source and translation.
        self.assertIn('[2] ' + self.NARRATIVE[:60], logged)
        self.assertIn('-> Claudia [\u2026] bevitori.', logged)

    def test_a_soft_sign_is_kept_the_second_time(self):
        translator = self._make_translator()
        paragraphs = self._paragraphs(
            '\u2018Tis the night.', self.NARRATIVE, self.NARRATIVE)
        doubted = set()
        first = {1: 'Era la notte.', 2: self.NARRATIVE_IT,
                 3: self.NARRATIVE_IT + ' Ancora.'}
        translator._check_reply(paragraphs, first, {}, doubted, 'chunk')
        self.assertNotIn(1, first)
        self.assertEqual({1}, doubted)
        # Asked for on its own, it comes back the same way: kept.
        second = {1: 'Era la notte, di nuovo.'}
        translator._check_reply(paragraphs, second, first, doubted, 'retry')
        self.assertEqual({1: 'Era la notte, di nuovo.'}, second)

    def test_a_reply_wrong_all_over_gets_no_second_chance(self):
        translator = self._make_translator()
        speech = '\u2018A line of speech, long enough to be measured.\u2019'
        paragraphs = self._paragraphs(speech, speech, speech, speech)
        doubted = {1, 2, 3, 4}
        reply = {
            1: 'Narrazione al posto della battuta numero uno.',
            2: 'Narrazione al posto della battuta numero due.',
            3: 'Narrazione al posto della battuta numero tre.',
            4: '\u00abUna battuta, abbastanza lunga da essere misurata.\u00bb'}
        translator._check_reply(paragraphs, reply, {}, doubted, 'retry')
        self.assertEqual([4], sorted(reply))

    def test_the_setting_turns_it_off(self):
        translator = self._make_translator(
            {'novel_verify_alignment': False})
        paragraphs = self._paragraphs(self.NARRATIVE)
        parsed = {1: 'Claudia [\u2026] bevitori.'}
        translator._check_reply(paragraphs, parsed, {}, set(), 'chunk')
        self.assertEqual({1: 'Claudia [\u2026] bevitori.'}, parsed)

    def test_a_partial_reply_numbered_from_one_is_not_guessed(self):
        # Two of the forty paragraphs asked for, numbered 1 and 2: which
        # two they are is unknown, so none of them is taken.
        translator = self._make_translator()
        response = ('{"paragraphs": ['
                    '{"n": 1, "translation": "A"},'
                    '{"n": 2, "translation": "B"}]}')
        parsed = translator._parse_structured_response(
            response, list(range(36, 76)))
        self.assertEqual({}, parsed)

    def test_a_short_reply_is_shown_in_the_log(self):
        translator = self._make_translator(
            {'novel_log_reply_excerpt': 20})
        translator.translator.last_generation_id = 'gen-abc'
        translator._log_reply_coverage(
            '{"paragraphs": [{"n": 1, "translation": "Uno"}]}',
            'chunk', [1, 2, 3], {1: 'Uno'})
        logged = str(translator.log.call_args[0][0])
        self.assertIn('covered 1 of 3 paragraphs', logged)
        self.assertIn('numbers kept: 1', logged)
        self.assertIn('Generation id: gen-abc.', logged)
        self.assertIn('It begins: {"paragraphs": [{"n"\u2026', logged)

    def test_a_full_reply_is_not_shown(self):
        translator = self._make_translator()
        translator._log_reply_coverage('...', 'chunk', [1], {1: 'Uno'})
        translator.log.assert_not_called()

    def test_the_provider_and_the_finish_reason_are_logged(self):
        translator = self._make_translator()
        translator.translator.last_provider = 'DeepInfra'
        translator.translator.last_finish_reason = 'length'
        translator.translator.last_generation_id = 'gen-42'
        translator._translate_with_retry('system', 'text', label='chunk')
        logged = str(translator.log.call_args[0][0])
        self.assertIn('via DeepInfra', logged)
        self.assertIn('stopped early: length', logged)
        self.assertIn('[gen-42]', logged)

    def test_the_report_is_on_disk_before_the_chapter_is_reported_done(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [make_paragraph(0, 'Alpha 1', page='a')]
        chapter = Chapter(1, 'Chapter 1', ['a'], paragraphs)
        engine = FakeEngine(translate_side_effect=lambda text, prompt: (
            _echo_markers(text) if _is_translation_call(prompt)
            else '{"narrative": true, "summary": "S", "entities": []}'))
        translator = NovelTranslator(
            engine, [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        order = []
        cache.set_info.side_effect = lambda key, value: order.append(key)
        translator.set_chapter_done(
            lambda *args: order.append('chapter_done'))
        translator.run()
        self.assertLess(
            order.index(INFO_NOVEL_REPORT), order.index('chapter_done'))

    def test_a_reply_that_simply_finished_adds_nothing(self):
        translator = self._make_translator()
        translator.translator.last_finish_reason = 'stop'
        translator._translate_with_retry('system', 'text', label='chunk')
        logged = str(translator.log.call_args[0][0])
        self.assertNotIn('stopped', logged)
        self.assertNotIn('via', logged)


class TestVerificationInThePipeline(unittest.TestCase):
    """A translation the checks refuse is asked for again like a
    missing one, and the answer to that takes its place."""

    def test_an_abbreviated_translation_is_asked_for_again(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [
            make_paragraph(0, 'First paragraph, plain.', page='a'),
            make_paragraph(1, 'Second paragraph, plain too.', page='a'),
        ]
        chapter = Chapter(1, 'C1', ['a'], paragraphs)
        calls = []

        def side_effect(text, prompt):
            if not _is_translation_call(prompt):
                return '{"entities": [], "summary": "S", "narrative": true}'
            calls.append(text)
            reply = _echo_markers_as_json(text)
            if len(calls) == 1:
                reply = reply.replace(
                    'Second paragraph, plain too. (IT)',
                    'Second [\u2026] too. (IT)')
            return reply

        engine = StructuredEngine(translate_side_effect=side_effect)
        translator = NovelTranslator(
            engine, [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.run()
        self.assertEqual(
            'Second paragraph, plain too. (IT)', paragraphs[1].translation)
        self.assertEqual('First paragraph, plain. (IT)',
                         paragraphs[0].translation)
        # One chunk, then one retry for the paragraph set aside.
        self.assertEqual(2, len(calls))
        self.assertIn('"n": 2', calls[1])
        self.assertNotIn('"n": 1', calls[1])


# ---------------------------------------------------------------------------
# Replies whose numbers slipped, retries in halves, titles, the report,
# the probe
# ---------------------------------------------------------------------------


NARR = ('The house was quiet. From the street below came the smell of '
        'bread and the noise of carts; nobody had noticed the lamp.')
NARR_IT = ('La casa era silenziosa. Dalla strada saliva l\u2019odore del '
           'pane e il rumore dei carri; nessuno aveva notato la lampada.')
DLG = '\u2018And what does she want with me, after all this time?\u2019'
DLG_IT = '\u00abE cosa vuole da me, dopo tutto questo tempo?\u00bb'


class TestRealignShifted(unittest.TestCase):
    """A reply whose numbers slipped is read as it was meant, and only
    then."""

    def _sources(self):
        # Narrative and dialogue alternate, so a slip shows.
        return {i: (DLG if i % 2 == 0 else NARR) for i in range(1, 9)}

    def _translation_of(self, i):
        return (DLG_IT if i % 2 == 0 else NARR_IT) + ' (%d)' % i

    def test_a_skipped_paragraph_shifts_the_rest_back(self):
        sources = self._sources()
        # The model skipped 3 and numbered on: what it calls 3 is 4.
        reply = {1: self._translation_of(1), 2: self._translation_of(2)}
        for n in range(3, 8):
            reply[n] = self._translation_of(n + 1)
        realigned, moves = realign_shifted(sources, reply)
        self.assertEqual([(n, n + 1) for n in range(3, 8)], moves)
        self.assertEqual(
            {1, 2, 4, 5, 6, 7, 8}, set(realigned))
        for n in (4, 5, 6, 7, 8):
            self.assertEqual(self._translation_of(n), realigned[n])

    def test_a_sound_reply_moves_nothing(self):
        sources = self._sources()
        reply = {n: self._translation_of(n) for n in sources}
        realigned, moves = realign_shifted(sources, reply)
        self.assertEqual([], moves)
        self.assertEqual(reply, realigned)

    def test_one_odd_paragraph_moves_nothing(self):
        sources = self._sources()
        reply = {n: self._translation_of(n) for n in sources}
        # Paragraph 3 came back as dialogue: wrong, but the rest is fine.
        reply[3] = DLG_IT
        realigned, moves = realign_shifted(sources, reply)
        self.assertEqual([], moves)
        self.assertNotIn(3, realigned)
        self.assertEqual(7, len(realigned))

    def test_two_skips_grow_the_offset(self):
        sources = {i: (DLG if i % 2 == 0 else NARR) for i in range(1, 15)}
        reply = {1: self._translation_of(1), 2: self._translation_of(2)}
        # Skipped 3: 3..7 hold 4..8. Then skipped 9: 8..12 hold 10..14.
        for n in (3, 4, 5, 6, 7):
            reply[n] = self._translation_of(n + 1)
        for n in (8, 9, 10, 11, 12):
            reply[n] = self._translation_of(n + 2)
        realigned, moves = realign_shifted(sources, reply)
        self.assertEqual(self._translation_of(14), realigned[14])
        self.assertEqual(self._translation_of(8), realigned[8])
        self.assertNotIn(3, realigned)
        self.assertNotIn(9, realigned)
        self.assertEqual(10, len(moves))

    def test_a_second_skip_too_close_to_confirm_is_left_out(self):
        # Three paragraphs between two skips are not enough to confirm
        # either offset: nothing is moved on a guess.
        sources = {i: (DLG if i % 2 == 0 else NARR) for i in range(1, 13)}
        reply = {1: self._translation_of(1), 2: self._translation_of(2)}
        for n in (3, 4, 5):
            reply[n] = self._translation_of(n + 1)
        for n in (6, 7, 8, 9, 10):
            reply[n] = self._translation_of(n + 2)
        realigned, moves = realign_shifted(sources, reply)
        for number, target in moves:
            self.assertEqual(self._translation_of(target), realigned[target])

    def test_the_pipeline_moves_them_and_asks_for_the_hole(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        translator = NovelTranslator(
            StructuredEngine(), [], ctx, cache, config={})
        translator.set_logging(Mock())
        translator.translator.last_provider = 'Shifty'
        sources = self._sources()
        paragraphs = [make_paragraph(i, sources[i]) for i in sources]
        parsed = {1: self._translation_of(1), 2: self._translation_of(2)}
        for n in range(3, 8):
            parsed[n] = self._translation_of(n + 1)
        translator._check_reply(paragraphs, parsed, {}, set(), 'chunk')
        self.assertEqual({1, 2, 4, 5, 6, 7, 8}, set(parsed))
        self.assertEqual(5, translator.realigned)
        self.assertEqual(1, translator.provider_stats['Shifty']['failures'])
        logged = ' '.join(
            str(c.args[0]) for c in translator.log.call_args_list)
        self.assertIn('slipped from paragraph 3', logged)

    def test_it_can_be_turned_off(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        translator = NovelTranslator(
            StructuredEngine(), [], ctx, cache,
            config={'novel_realign_shifted_replies': False})
        translator.set_logging(Mock())
        sources = self._sources()
        paragraphs = [make_paragraph(i, sources[i]) for i in sources]
        parsed = {1: self._translation_of(1), 2: self._translation_of(2)}
        for n in range(3, 8):
            parsed[n] = self._translation_of(n + 1)
        translator._check_reply(paragraphs, parsed, {}, set(), 'chunk')
        self.assertEqual(0, translator.realigned)
        # The shifted ones are set aside instead, to be asked for again.
        self.assertEqual({1, 2}, set(parsed))


class TestProviderExclusion(unittest.TestCase):
    def _translator(self, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        engine = StructuredEngine()
        engine.last_provider = 'Flaky'
        engine.excluded = []
        engine.exclude_provider = lambda name: (
            engine.excluded.append(name) or True)
        translator = NovelTranslator(
            engine, [], ctx, cache, config=config or {})
        translator.set_logging(Mock())
        return translator, engine

    def test_excluded_after_the_configured_failures(self):
        translator, engine = self._translator()
        translator._note_provider_failure('short')
        self.assertEqual([], engine.excluded)
        translator._note_provider_failure('short')
        self.assertEqual(['Flaky'], engine.excluded)
        self.assertTrue(translator.provider_stats['Flaky']['excluded'])
        # Not asked again once excluded.
        translator._note_provider_failure('short')
        self.assertEqual(['Flaky'], engine.excluded)

    def test_zero_never_excludes(self):
        translator, engine = self._translator(
            {'novel_provider_failures_before_exclusion': 0})
        for _i in range(5):
            translator._note_provider_failure('short')
        self.assertEqual([], engine.excluded)
        self.assertEqual(5, translator.provider_stats['Flaky']['failures'])

    def test_a_very_short_reply_counts(self):
        translator, engine = self._translator()
        translator._log_reply_coverage('...', 'chunk', list(range(1, 11)),
                                       {1: 'a', 2: 'b'})
        self.assertEqual(1, translator.provider_stats['Flaky']['failures'])


class TestRetryBatches(unittest.TestCase):
    def _translator(self, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        translator = NovelTranslator(
            StructuredEngine(), [], ctx, cache, config=config or {})
        translator.set_logging(Mock())
        return translator

    def test_many_are_asked_for_in_halves(self):
        translator = self._translator()
        self.assertEqual(
            [[1, 2, 3, 4, 5], [6, 7, 8, 9]],
            translator._retry_batches(range(1, 10)))

    def test_a_handful_goes_at_once(self):
        translator = self._translator()
        self.assertEqual(
            [[1, 2, 3, 4]], translator._retry_batches([1, 2, 3, 4]))

    def test_the_setting_turns_it_off(self):
        translator = self._translator({'novel_retry_split': False})
        self.assertEqual(
            [list(range(1, 10))], translator._retry_batches(range(1, 10)))

    def test_a_retry_is_sent_in_two_requests(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [
            make_paragraph(i, 'Paragraph number %d, plain.' % i, page='a')
            for i in range(10)]
        chapter = Chapter(1, 'C1', ['a'], paragraphs)
        calls = []

        def side_effect(text, prompt):
            if not _is_translation_call(prompt):
                return '{"entities": [], "summary": "S", "narrative": true}'
            calls.append(text)
            reply = _echo_markers_as_json(text)
            if len(calls) == 1:
                # Only the first two paragraphs the first time.
                obj = json.loads(reply)
                obj['paragraphs'] = obj['paragraphs'][:2]
                reply = json.dumps(obj)
            return reply

        engine = StructuredEngine(translate_side_effect=side_effect)
        translator = NovelTranslator(
            engine, [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0,
                    'novel_context_timing': 'after'})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.run()
        # First call, then the eight missing in two halves.
        self.assertEqual(3, len(calls))
        self.assertIn('"n": 3', calls[1])
        self.assertNotIn('"n": 7', calls[1])
        self.assertIn('"n": 7', calls[2])
        for p in paragraphs:
            self.assertIn('(IT)', p.translation)


class TestTitleMatches(unittest.TestCase):
    def test_whole_words_case_insensitive(self):
        self.assertTrue(title_matches('Also by Paul Doherty', 'also by'))
        self.assertTrue(
            title_matches('COPYRIGHT PAGE', 'copyright, praise for'))
        self.assertFalse(title_matches('Discontents', 'contents'))
        self.assertFalse(title_matches('Chapter 1', 'copyright'))
        self.assertFalse(title_matches('', 'copyright'))
        self.assertFalse(title_matches('Contents', ''))


class TestTitledChapters(unittest.TestCase):
    """What a title says about a chapter spares a request."""

    def _run(self, title, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [
            make_paragraph(0, 'The Nightingale Gallery', page='a'),
            make_paragraph(1, 'The House of the Red Slayer', page='a'),
        ]
        chapter = Chapter(1, title, ['a'], paragraphs)
        calls = []

        def side_effect(text, prompt):
            calls.append(prompt)
            if _is_translation_call(prompt):
                return _echo_markers(text)
            return ('{"narrative": true, "summary": "Summary.", '
                    '"entities": []}')

        engine = FakeEngine(translate_side_effect=side_effect)
        base = {'novel_min_chars_for_context': 0,
                'novel_skip_context_last_chapter': False}
        base.update(config or {})
        translator = NovelTranslator(
            engine, [chapter], ctx, cache, config=base)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.run()
        return paragraphs, calls, ctx

    def test_a_list_of_other_books_stays_as_it_is(self):
        paragraphs, calls, ctx = self._run('Also by Paul Doherty')
        self.assertEqual('The Nightingale Gallery', paragraphs[0].translation)
        self.assertEqual([], calls)
        self.assertEqual(1, ctx.get_progress())

    def test_front_matter_is_translated_without_a_summary(self):
        paragraphs, calls, ctx = self._run('Praise for Paul Doherty')
        self.assertIn('(IT)', paragraphs[0].translation)
        self.assertTrue(all(_is_translation_call(p) for p in calls))
        self.assertEqual('', ctx.get_summaries()[0]['summary'])

    def test_a_story_chapter_gets_its_summary(self):
        paragraphs, calls, ctx = self._run('Chapter 1')
        self.assertTrue(any(not _is_translation_call(p) for p in calls))
        self.assertEqual('Summary.', ctx.get_summaries()[0]['summary'])

    def test_the_lists_can_be_replaced(self):
        paragraphs, calls, ctx = self._run(
            'Praise for Paul Doherty',
            {'novel_front_matter_titles': 'colophon',
             'novel_untranslated_titles': 'praise for'})
        self.assertEqual('The Nightingale Gallery', paragraphs[0].translation)


class TestContextBeforeTheChapter(unittest.TestCase):
    def test_the_glossary_guides_the_chapter_itself(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [
            make_paragraph(0, 'Alpha walked into the room.', page='a'),
            make_paragraph(1, 'Alpha sat down.', page='a'),
        ]
        chapter = Chapter(1, 'Chapter 1', ['a'], paragraphs)
        calls = []

        def side_effect(text, prompt):
            calls.append((prompt, text))
            if _is_translation_call(prompt):
                return _echo_markers(text)
            self.assertNotIn('Translation:', text)
            self.assertIn('Alpha walked', text)
            return ('{"narrative": true, "summary": "Alpha arrives.", '
                    '"entities": [{"source": "Alpha", "translation": '
                    '"Alfa", "type": "character", "notes": ""}]}')

        engine = FakeEngine(translate_side_effect=side_effect)
        translator = NovelTranslator(
            engine, [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0,
                    'novel_skip_context_last_chapter': False})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.run()
        # The context call came first, and the translation prompt
        # carried what it learned.
        self.assertFalse(_is_translation_call(calls[0][0]))
        self.assertTrue(_is_translation_call(calls[1][0]))
        self.assertIn('Alfa', calls[1][0])
        self.assertIn('Alpha arrives.', calls[1][0])
        self.assertEqual('Alfa', ctx.get_glossary()['Alpha']['translation'])
        self.assertEqual('Alpha arrives.', ctx.get_summaries()[0]['summary'])
        self.assertEqual(2, len(calls))


class TestUsageReport(unittest.TestCase):
    def _translator(self, engine=None, stored=None):
        cache = Mock()
        cache.get_info.side_effect = lambda key: (
            stored if key == INFO_NOVEL_USAGE else None)
        ctx = ContextManager(cache).load()
        translator = NovelTranslator(
            engine or StructuredEngine(), [], ctx, cache, config={})
        translator.set_logging(Mock())
        return translator, cache

    def test_replies_are_added_up_per_kind(self):
        translator, cache = self._translator()
        engine = translator.translator
        engine.last_usage = {'prompt_tokens': 100, 'completion_tokens': 40,
                             'cost': 0.001}
        engine.last_provider = 'DeepInfra'
        translator._request_kind = 'translation'
        translator._translate_with_retry('system', 'text', label='chunk')
        translator._request_kind = 'retry'
        translator._translate_with_retry('system', 'text', label='retry')
        self.assertEqual(2, translator.usage['translation']['requests']
                         + translator.usage['retry']['requests'])
        self.assertEqual(100, translator.usage['retry']['prompt_tokens'])
        self.assertAlmostEqual(0.001, translator.usage['retry']['cost'])
        self.assertEqual(2, translator.provider_stats['DeepInfra']['requests'])
        logged = str(translator.log.call_args[0][0])
        self.assertIn('100 in + 40 out tokens', logged)
        self.assertIn('$0.001', logged)

    def test_the_service_tier_that_served_is_noted_and_counted(self):
        translator, cache = self._translator()
        engine = translator.translator
        engine.service_tier = 'flex'
        engine.last_service_tier = 'flex'
        translator._request_kind = 'translation'
        translator._translate_with_retry('system', 'text', label='chunk')
        engine.last_service_tier = 'default'
        translator._translate_with_retry('system', 'text', label='chunk')
        self.assertEqual({'flex': 1, 'default': 1}, translator.tier_stats)
        logged = str(translator.log.call_args[0][0])
        # Asked for flex, served at the default price: worth a word.
        self.assertIn('default tier', logged)
        self.assertIn('Service tier that served the replies',
                      translator.build_report())
        self.assertIn('flex 1', translator.build_report_html())

    def test_the_default_tier_is_not_mentioned_when_nothing_else_was_asked(
            self):
        translator, cache = self._translator()
        engine = translator.translator
        engine.last_service_tier = 'default'
        translator._request_kind = 'translation'
        translator._translate_with_retry('system', 'text', label='chunk')
        self.assertNotIn('tier', str(translator.log.call_args[0][0]))
        self.assertNotIn('Service tier', translator.build_report())

    def test_a_provider_s_own_name_for_the_standard_tier_is_not_noted(
            self):
        translator, cache = self._translator()
        engine = translator.translator
        engine.last_service_tier = 'on_demand'   # Groq
        translator._request_kind = 'translation'
        translator._translate_with_retry('system', 'text', label='chunk')
        self.assertNotIn('tier', str(translator.log.call_args[0][0]))
        self.assertNotIn('Service tier', translator.build_report())

    def test_a_flex_run_that_gives_up_says_flex_does_not_fall_back(self):
        translator, cache = self._translator()
        translator.translator.service_tier = 'flex'
        translator.translator.translate = Mock(
            side_effect=Exception('no flex capacity'))
        translator._wait = Mock()
        with self.assertRaises(TranslationFailed) as caught:
            translator._translate_with_retry(
                'system', 'text', attempts=1, label='chunk')
        self.assertIn('flex', str(caught.exception))
        self.assertIn('default tier', str(caught.exception))

    def test_tokens_are_estimated_when_not_reported(self):
        translator, cache = self._translator()
        translator._request_kind = 'translation'
        translator._translate_with_retry('system', 'x' * 400, label='chunk')
        entry = translator.usage['translation']
        self.assertEqual(1, entry['estimated'])
        self.assertGreater(entry['prompt_tokens'], 0)
        self.assertIn('estimated', translator.build_report())

    def test_the_report_names_the_totals_and_the_advice(self):
        translator, cache = self._translator()
        translator.usage = {
            'translation': {'requests': 10, 'prompt_tokens': 1000,
                            'completion_tokens': 500, 'cost': 0.02,
                            'estimated': 0},
            'context': {'requests': 2, 'prompt_tokens': 300,
                        'completion_tokens': 100, 'cost': 0.005,
                        'estimated': 0}}
        translator.provider_stats = {
            'Flaky': {'requests': 8, 'failures': 3, 'excluded': True},
            'Solid': {'requests': 4, 'failures': 0, 'excluded': False}}
        translator.realigned = 7
        report = translator.build_report()
        self.assertIn('total', report)
        self.assertIn('12', report)
        self.assertIn('1300', report)
        self.assertIn('$0.025', report)
        self.assertIn('Flaky: 8 replies, 3 unreliable', report)
        self.assertIn('excluded from the current run', report)
        self.assertIn('Flaky gave 3 unreliable replies out of 8', report)
        self.assertIn('7 translation(s) came back under the wrong number',
                      report)

    def test_the_totals_survive_a_resume(self):
        stored = json.dumps({
            'usage': {'translation': {
                'requests': 5, 'prompt_tokens': 50, 'completion_tokens': 20,
                'cost': 0.0, 'estimated': 0}},
            'providers': {'Old': {'requests': 5, 'failures': 2,
                                  'excluded': True}},
            'realigned': 3})
        translator, cache = self._translator(stored=stored)
        translator._load_usage()
        self.assertEqual(5, translator.usage['translation']['requests'])
        self.assertEqual(3, translator.realigned)
        # An exclusion lasts one run.
        self.assertFalse(translator.provider_stats['Old']['excluded'])
        translator._publish_report()
        keys = [c.args[0] for c in cache.set_info.call_args_list]
        self.assertIn(INFO_NOVEL_USAGE, keys)
        self.assertIn(INFO_NOVEL_REPORT, keys)

    def test_a_run_ends_with_the_report(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [make_paragraph(0, 'Alpha 1', page='a')]
        chapter = Chapter(1, 'Chapter 1', ['a'], paragraphs)
        engine = FakeEngine(translate_side_effect=lambda text, prompt: (
            _echo_markers(text) if _is_translation_call(prompt)
            else '{"narrative": true, "summary": "S", "entities": []}'))
        translator = NovelTranslator(
            engine, [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0})
        reports = []
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.set_report(reports.append)
        translator.run()
        self.assertTrue(reports)
        self.assertIn('Requests and tokens', reports[-1])
        logged = ' '.join(
            str(c.args[0]) for c in translator.log.call_args_list)
        self.assertIn('Requests and tokens', logged)


class TestReportTimeAndShare(unittest.TestCase):
    def _translator(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        translator = NovelTranslator(
            StructuredEngine(), [], ctx, cache, config={})
        translator.set_logging(Mock())
        return translator

    def test_request_time_is_added_up_per_kind(self):
        translator = self._translator()
        translator._request_kind = 'translation'
        with patch(module_name + '.time.time', side_effect=[
                100.0, 107.5, 107.5, 107.5, 107.5, 107.5, 107.5, 107.5]):
            translator._translate_with_retry('system', 'text', label='c')
        self.assertAlmostEqual(
            7.5, translator.usage['translation']['seconds'])
        self.assertIn('8s', translator.build_report())

    def test_the_share_of_troubled_text_is_measured_on_its_length(self):
        translator = self._translator()
        translator.translator.last_provider = 'P'
        paragraphs = [
            make_paragraph(0, 'a' * 300), make_paragraph(1, 'b' * 100)]
        # The long one came back, the short one did not.
        translator._check_reply(
            paragraphs, {1: 'x' * 300}, {}, set(), 'chunk', [1, 2])
        self.assertEqual(400, translator.text_stats['chars'])
        self.assertEqual(100, translator.text_stats['bad_chars'])
        self.assertEqual(400, translator.provider_stats['P']['chars'])
        self.assertEqual(100, translator.provider_stats['P']['bad_chars'])
        report = translator.build_report()
        self.assertIn('25% of its text set aside or moved', report)
        self.assertIn('400 characters, of which 25%', report)

    def test_the_html_report_is_a_table(self):
        translator = self._translator()
        translator.usage = {'translation': {
            'requests': 3, 'prompt_tokens': 12000, 'completion_tokens': 4000,
            'cost': 0.01, 'estimated': 0, 'seconds': 65}}
        translator.run_seconds = 3700
        html = translator.build_report_html()
        self.assertIn('<table', html)
        self.assertIn('<td align="right">12,000</td>', html)
        self.assertIn('1m 05s', html)
        self.assertIn('1h 01m 40s', html)
        self.assertIn('<b>total</b>', html)

    def test_durations_and_shares(self):
        self.assertEqual('12s', NovelTranslator._duration(12.4))
        self.assertEqual('4m 05s', NovelTranslator._duration(245))
        self.assertEqual('0%', NovelTranslator._share(0, 0))
        self.assertEqual('2.5%', NovelTranslator._share(25, 1000))
        self.assertEqual('40%', NovelTranslator._share(400, 1000))

    def test_the_run_time_is_kept_across_runs(self):
        stored = json.dumps({'usage': {}, 'providers': {}, 'realigned': 0,
                             'text': {'chars': 10, 'bad_chars': 0},
                             'run_seconds': 120.0})
        cache = Mock()
        cache.get_info.side_effect = lambda key: (
            stored if key == INFO_NOVEL_USAGE else None)
        ctx = ContextManager(cache).load()
        translator = NovelTranslator(
            StructuredEngine(), [], ctx, cache, config={})
        translator.set_logging(Mock())
        translator._load_usage()
        self.assertEqual(120.0, translator.run_seconds)
        self.assertIn('2m 00s', translator.build_report())


class TestProbe(unittest.TestCase):
    def test_a_well_behaved_model_passes(self):
        def side_effect(text, prompt):
            return _echo_markers_as_json(text)
        engine = StructuredEngine(translate_side_effect=side_effect)
        engine.model = 'fake/model'
        report, reliable = probe_engine(engine, {}, details=True)
        self.assertTrue(reliable)
        self.assertIn('Paragraphs back: %d of %d' % (
            len(PROBE_PARAGRAPHS), len(PROBE_PARAGRAPHS)), report)
        self.assertIn('aligned, every check passed', report)
        self.assertIn('fake/model', report)
        self.assertIsInstance(probe_engine(engine, {}), str)

    def test_a_model_that_drops_paragraphs_fails(self):
        def side_effect(text, prompt):
            obj = json.loads(_echo_markers_as_json(text))
            obj['paragraphs'] = obj['paragraphs'][:1]
            return json.dumps(obj)
        engine = StructuredEngine(translate_side_effect=side_effect)
        report, reliable = probe_engine(engine, {}, details=True)
        self.assertFalse(reliable)
        self.assertIn('NOT reliable', report)
        self.assertIn('(missing)', report)

    def test_a_failing_engine_is_reported_not_raised(self):
        engine = StructuredEngine(
            translate_side_effect=lambda text, prompt: Exception('boom'))
        report, reliable = probe_engine(engine, {}, details=True)
        self.assertFalse(reliable)
        self.assertIn('Failed:', report)


class TestProgressByText(unittest.TestCase):
    def test_the_bar_follows_the_characters(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [
            make_paragraph(0, 'Short front matter.', page='a'),
            make_paragraph(1, 'x' * 981, page='b'),
        ]
        chapters = [
            Chapter(1, 'Copyright', ['a'], paragraphs[:1]),
            Chapter(2, 'Chapter 1', ['b'], paragraphs[1:]),
        ]
        engine = FakeEngine(translate_side_effect=lambda text, prompt: (
            _echo_markers(text) if _is_translation_call(prompt)
            else '{"narrative": true, "summary": "S", "entities": []}'))
        translator = NovelTranslator(
            engine, chapters, ctx, cache,
            config={'novel_min_chars_for_context': 0})
        translator.set_logging(Mock())
        fractions = []
        translator.set_progress(lambda f, m: fractions.append((f, m)))
        translator.run()
        # The copyright page is 19 of 1000 characters: not a third.
        after_front_matter = [
            f for f, m in fractions if 'Chapter 1/2' in m]
        self.assertTrue(after_front_matter)
        self.assertLess(after_front_matter[0], 0.05)
        self.assertIn('1% of the text', [
            m for f, m in fractions if 'Chapter 1/2' in m][0])
        self.assertEqual(1.0, fractions[-1][0])


class TestSourceContextAndChunksInFlight(unittest.TestCase):
    def _book(self, n=6):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [
            make_paragraph(i, 'Paragraph %d of the chapter.' % i, page='a')
            for i in range(n)]
        chapter = Chapter(1, 'Chapter 1', ['a'], paragraphs)
        return cache, ctx, paragraphs, chapter

    def _engine(self, calls):
        def side_effect(text, prompt):
            if not _is_translation_call(prompt):
                return '{"narrative": true, "summary": "S", "entities": []}'
            calls.append(text)
            return _echo_markers_as_json(text)
        return StructuredEngine(translate_side_effect=side_effect)

    def test_the_source_around_a_chunk_is_shown_not_translated(self):
        cache, ctx, paragraphs, chapter = self._book()
        calls = []
        translator = NovelTranslator(
            self._engine(calls), [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0,
                    'novel_max_paragraphs_per_chunk': 2,
                    'novel_chunk_context': 'source',
                    'novel_source_context_paragraphs': 1})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.run()
        self.assertEqual(3, len(calls))
        first, middle, last = calls
        self.assertNotIn('BEFORE', first)
        self.assertIn('AFTER', first)
        self.assertIn('Paragraph 2 of the chapter.', first)
        self.assertIn('BEFORE', middle)
        self.assertIn('AFTER', middle)
        self.assertIn('Paragraph 1 of the chapter.', middle)
        self.assertIn('Paragraph 4 of the chapter.', middle)
        self.assertNotIn('AFTER', last)
        self.assertNotIn('already translated', middle)
        # Only the chunk's own paragraphs are in the JSON to translate.
        self.assertNotIn('"source": "Paragraph 1 of the chapter."', middle)
        for p in paragraphs:
            self.assertIn('(IT)', p.translation)

    def test_the_translated_overlap_is_the_default(self):
        cache, ctx, paragraphs, chapter = self._book()
        calls = []
        translator = NovelTranslator(
            self._engine(calls), [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0,
                    'novel_max_paragraphs_per_chunk': 2})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.run()
        self.assertIn('already translated', calls[1])
        self.assertNotIn('BEFORE', calls[1])

    def test_chunks_in_flight_translate_the_whole_chapter(self):
        cache, ctx, paragraphs, chapter = self._book(10)
        calls = []
        engine = self._engine(calls)
        translator = NovelTranslator(
            engine, [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0,
                    'novel_max_paragraphs_per_chunk': 3,
                    'novel_parallel_chunks': 4})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.run()
        self.assertEqual(4, len(calls))
        for p in paragraphs:
            self.assertEqual(
                'Paragraph %d of the chapter. (IT)' % p.id, p.translation)
        # The translated overlap gave way to the source context.
        self.assertTrue(all('already translated' not in c for c in calls))
        self.assertTrue(any('BEFORE' in c for c in calls))
        logged = ' '.join(
            str(c.args[0]) for c in translator.log.call_args_list)
        self.assertIn('Chunks in flight at once: 4', logged)
        # Every chunk ran on its own engine copy; the root is clean.
        self.assertEqual([], engine.clones)
        self.assertEqual(4, translator.usage['translation']['requests'])

    def test_a_failing_chunk_ends_the_chapter(self):
        cache, ctx, paragraphs, chapter = self._book(6)

        def side_effect(text, prompt):
            # The second chunk, by the source it carries to translate
            # (the numbers are chunk-local, 1 and 2 in every chunk).
            if '"source": "Paragraph 2 of the chapter."' in text:
                raise TranslationFailed('boom')
            return _echo_markers_as_json(text)
        engine = StructuredEngine(translate_side_effect=side_effect)
        engine.request_attempt = 1
        engine.abort = Mock()
        translator = NovelTranslator(
            engine, [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0,
                    'novel_max_paragraphs_per_chunk': 2,
                    'novel_parallel_chunks': 3})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        with self.assertRaises(TranslationFailed):
            translator.run()
        engine.abort.assert_called()

    def test_a_chunk_cut_short_by_another_failing_does_not_ask_again(self):
        """The abort that follows a failed chunk closes the response the
        other chunks are reading, and their read ends in an error. It is
        not a passing one: the chunk that took it for one asked again,
        and again, with the pauses in between, for a reply nobody was
        waiting for any more."""
        import threading
        cache, ctx, paragraphs, chapter = self._book(4)
        aborted = threading.Event()

        def side_effect(text, prompt):
            if '"source": "Paragraph 2 of the chapter."' in text:
                raise TranslationFailed('boom')
            # Still reading when the other chunk fails: the abort ends
            # the read the way a closed response does.
            aborted.wait(5)
            raise Exception(
                'PyMemoryView_FromBuffer(): info->buf must not be NULL')
        engine = StructuredEngine(translate_side_effect=side_effect)
        engine.request_attempt = 2
        engine.abort = Mock(side_effect=aborted.set)
        translator = NovelTranslator(
            engine, [chapter], ctx, cache,
            config={'novel_min_chars_for_context': 0,
                    'novel_max_paragraphs_per_chunk': 2,
                    'novel_parallel_chunks': 2})
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        with patch(module_name + '.time.sleep'):
            with self.assertRaises(TranslationFailed):
                translator.run()
        asked = [c['text'] for c in engine.translate_calls
                 if _is_translation_call(c['prompt'])]
        # The failing chunk spent its two attempts; the one cut short
        # sent one request and did not send it again.
        self.assertEqual(3, len(asked))
        self.assertEqual(
            1, sum(1 for text in asked
                   if '"source": "Paragraph 0 of the chapter."' in text))

    def test_a_copy_of_the_engine_carries_no_copies_of_its_own(self):
        """The list of copies belongs to the root engine. A copy made
        once the list existed used to share it, and an abort that asked
        every copy to abort its copies went round it without end."""
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        engine = StructuredEngine()
        translator = NovelTranslator(engine, [], ctx, cache, config={})
        first = translator._clone()
        second = translator._clone()
        self.assertEqual([first.translator, second.translator],
                         engine.clones)
        self.assertIsNone(first.translator.clones)
        self.assertIsNone(second.translator.clones)

    def test_a_refused_exclusion_is_said_once_and_not_asked_again(self):
        """An engine that cannot leave the provider out -- it is the
        only one the settings allow -- says so; the run keeps counting
        the failures but does not ask again at each of them."""
        class PinnedEngine(StructuredEngine):
            def __init__(self):
                super().__init__()
                self.asked = 0

            def exclude_provider(self, name):
                self.asked += 1
                raise ValueError(
                    'it is the only provider the settings allow')

        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        engine = PinnedEngine()
        translator = NovelTranslator(engine, [], ctx, cache, config={})
        log = Mock()
        translator.set_logging(log)
        engine.last_provider = 'Pinned'
        for _ in range(4):
            translator._note_provider_failure('short')
        self.assertEqual(1, engine.asked)
        stats = translator.provider_stats['Pinned']
        self.assertFalse(stats['excluded'])
        self.assertEqual(4, stats['failures'])
        logged = ' '.join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn('but it is not excluded: it is the only provider',
                      logged)
        self.assertEqual(1, logged.count('not excluded'))

    def test_an_exclusion_reaches_the_root_engine(self):
        class ExcludingEngine(StructuredEngine):
            def __init__(self):
                super().__init__()
                self.excluded = []

            def exclude_provider(self, name):
                self.excluded.append(name)
                return True

        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        engine = ExcludingEngine()
        translator = NovelTranslator(engine, [], ctx, cache, config={})
        translator.set_logging(Mock())
        clone = translator._clone()
        clone.translator.excluded = []
        clone.translator.last_provider = 'Flaky'
        clone._note_provider_failure('short')
        clone._note_provider_failure('short')
        self.assertEqual(['Flaky'], engine.excluded)
        self.assertEqual(['Flaky'], clone.translator.excluded)
        self.assertTrue(translator.provider_stats['Flaky']['excluded'])


class TestBalancedChunks(unittest.TestCase):
    def _paragraphs(self, sizes):
        return [make_paragraph(i, 'x' * (4 * size))
                for i, size in enumerate(sizes)]

    def test_chunks_in_flight_are_cut_to_the_same_size(self):
        # Filled to a cap of 1000 tokens: 1000 + 1000 + 200.
        budget = TokenBudget(budget=1000, max_paragraphs=0)
        paragraphs = self._paragraphs([250] * 8 + [100, 100])
        filled = budget.chunk_with_stats(paragraphs)
        self.assertEqual([1000, 1000, 200], [t for _c, t, _r in filled])
        balanced = budget.balance(filled)
        self.assertEqual(3, len(balanced))
        self.assertEqual([750, 750, 700], [t for _c, t, _r in balanced])
        # Nothing lost, nothing reordered.
        self.assertEqual(
            [p.id for p in paragraphs],
            [p.id for c, _t, _r in balanced for p in c])

    def test_the_caps_still_hold(self):
        budget = TokenBudget(budget=1000, max_paragraphs=3)
        paragraphs = self._paragraphs([100] * 7)
        balanced = budget.balance(budget.chunk_with_stats(paragraphs))
        self.assertTrue(all(
            len(c) <= 3 and t <= 1000 for c, t, _r in balanced))
        self.assertEqual(7, sum(len(c) for c, _t, _r in balanced))

    def test_one_chunk_is_left_alone(self):
        budget = TokenBudget(budget=1000, max_paragraphs=0)
        filled = budget.chunk_with_stats(self._paragraphs([100, 100]))
        self.assertEqual(filled, budget.balance(filled))

    def test_only_when_in_flight(self):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [
            make_paragraph(i, 'Paragraph %d of the chapter.' % i, page='a')
            for i in range(10)]
        chapter = Chapter(1, 'Chapter 1', ['a'], paragraphs)
        for parallel, expected in ((1, [4, 4, 2]), (3, [4, 3, 3])):
            calls = []

            def side_effect(text, prompt):
                if not _is_translation_call(prompt):
                    return ('{"narrative": true, "summary": "S", '
                            '"entities": []}')
                calls.append(text.count('"source":'))
                return _echo_markers_as_json(text)
            translator = NovelTranslator(
                StructuredEngine(translate_side_effect=side_effect),
                [chapter], ctx, cache,
                config={'novel_min_chars_for_context': 0,
                        'novel_max_paragraphs_per_chunk': 4,
                        'novel_parallel_chunks': parallel})
            translator.set_logging(Mock())
            translator.set_progress(Mock())
            translator.run()
            self.assertEqual(expected, sorted(calls, reverse=True),
                             'parallel=%d' % parallel)
            ctx.reset()
            for p in paragraphs:
                p.translation = None


class TestAuthorCheck(unittest.TestCase):
    """The model is asked whether it knows the author before it is
    asked how they write."""

    KNOWN = ('{"recognised": true, "works": ["Il barone rampante", '
             '"Le città invisibili"], "shared_name": false, '
             '"note": "Italian novelist of the twentieth century."}')
    UNKNOWN = ('{"recognised": false, "works": [], "shared_name": false, '
               '"note": "unknown"}')

    def _translator(self, replies):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        replies = list(replies)
        texts = []

        def side_effect(text, prompt):
            texts.append(text)
            return replies.pop(0)
        engine = FakeEngine(translate_side_effect=side_effect)
        translator = NovelTranslator(
            engine, [], ctx, cache,
            config={'novel_book_author': 'Italo Calvino',
                    'novel_book_title': 'Il barone rampante'})
        translator.set_logging(Mock())
        return translator, texts, cache

    def test_a_known_author_gets_a_brief_anchored_on_their_works(self):
        translator, texts, cache = self._translator([self.KNOWN, BRIEF])
        self.assertEqual(BRIEF, translator._ensure_author_style())
        self.assertEqual(2, len(texts))
        self.assertIn('Is "Italo Calvino" a published novelist', texts[0])
        self.assertIn('Known works of this author, for orientation: '
                      'Il barone rampante, Le città invisibili', texts[1])
        logged = ' '.join(
            str(c.args[0]) for c in translator.log.call_args_list)
        self.assertIn('the model knows "Italo Calvino"', logged)
        self.assertIn('works it names', logged)

    def test_an_unknown_author_gets_no_brief_and_a_note(self):
        translator, texts, cache = self._translator([self.UNKNOWN, BRIEF])
        self.assertEqual('', translator._ensure_author_style())
        # The brief was never asked for.
        self.assertEqual(1, len(texts))
        notes = [c.args[1] for c in cache.set_info.call_args_list
                 if c.args and c.args[0] == INFO_NOVEL_STYLE_NOTE]
        self.assertIn('cannot name any book by this author', notes[-1])
        self.assertIn('Reset context', notes[-1])

    def test_an_unreadable_check_does_not_cost_the_brief(self):
        translator, texts, cache = self._translator(['Boh.', BRIEF])
        self.assertEqual(BRIEF, translator._ensure_author_style())
        self.assertEqual(2, len(texts))
        self.assertIn('Known works of this author, for orientation:',
                      texts[1])

    def test_a_failed_check_does_not_cost_the_brief(self):
        translator, texts, cache = self._translator(
            [Exception('boom'), Exception('boom'), BRIEF])
        translator.translator.request_attempt = 2
        with patch.object(translator, '_wait'):
            self.assertEqual(BRIEF, translator._ensure_author_style())

    def test_the_check_can_be_turned_off(self):
        translator, texts, cache = self._translator([BRIEF])
        translator.config['novel_author_check'] = False
        self.assertEqual(BRIEF, translator._ensure_author_style())
        self.assertEqual(1, len(texts))
        self.assertNotIn('published novelist', texts[0])


class TestGlossaryChapterLimit(unittest.TestCase):
    def _run(self, entities, config=None):
        cache = Mock()
        cache.get_info.return_value = None
        ctx = ContextManager(cache).load()
        cache.reset_mock()
        paragraphs = [make_paragraph(0, 'Alpha walked in.', page='a')]
        chapter = Chapter(1, 'Chapter 1', ['a'], paragraphs)
        sent = []
        schemas = []

        def side_effect(text, prompt):
            if _is_translation_call(prompt):
                return _echo_markers_as_json(text)
            sent.append(text)
            return json.dumps({'narrative': True, 'summary': 'S',
                               'entities': entities})
        engine = StructuredEngine(translate_side_effect=side_effect)
        original = engine.get_body_for_structured

        def record(text, schema=None):
            schemas.append(schema)
            return original(text, schema)
        engine.get_body_for_structured = record
        base = {'novel_min_chars_for_context': 0,
                'novel_skip_context_last_chapter': False}
        base.update(config or {})
        translator = NovelTranslator(
            engine, [chapter], ctx, cache, config=base)
        translator.set_logging(Mock())
        translator.set_progress(Mock())
        translator.run()
        return ctx, sent, schemas, translator

    def _entities(self, n):
        return [{'source': 'Name%d' % i, 'translation': 'Nome%d' % i,
                 'type': 'character', 'notes': ''} for i in range(n)]

    def test_the_limit_is_stated_enforced_and_applied(self):
        ctx, sent, schemas, translator = self._run(self._entities(60))
        self.assertIn('Hard limit: 50 entries', sent[0])
        context_schema = [s for s in schemas
                          if s and 'summary' in s['properties']][0]
        self.assertEqual(
            50, context_schema['properties']['entities']['maxItems'])
        self.assertEqual(50, len(ctx.get_glossary()))
        logged = ' '.join(
            str(c.args[0]) for c in translator.log.call_args_list)
        self.assertIn('listed 60 new entries', logged)

    def test_the_limit_is_a_setting(self):
        ctx, sent, schemas, translator = self._run(
            self._entities(20), {'novel_glossary_chapter_max_entries': 10})
        self.assertIn('Hard limit: 10 entries', sent[0])
        self.assertEqual(10, len(ctx.get_glossary()))

    def test_the_class_schema_is_not_changed(self):
        self._run(self._entities(3))
        self.assertNotIn('maxItems', NovelTranslator._CONTEXT_RESPONSE_SCHEMA[
            'properties']['entities'])
        self.assertNotIn('maxItems', NovelTranslator._GLOSSARY_RESPONSE_SCHEMA[
            'properties']['entities'])

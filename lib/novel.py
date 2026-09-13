"""Novel Mode: chapter-aware sequential translation pipeline for LLMs.

This module implements a dedicated translation pipeline optimized for narrative
long-form content (novels). Unlike the default paragraph-by-paragraph parallel
pipeline (see ``lib/translation.py``), the novel pipeline:

  * groups paragraphs by chapter, using the ebook Table of Contents when
    available (fallback: one XHTML file per chapter);
  * translates chapters sequentially, one after the other, without concurrency;
  * chunks each chapter to fit an LLM token budget (default ~8k) without ever
    splitting a paragraph in half;
  * maintains a running summary of previously translated chapters, injected
    into each translation prompt so the model preserves narrative continuity;
  * maintains a dynamic glossary of characters, places and other named
    entities extracted after each chapter, kept consistent across the book;
  * persists progress, summaries and glossary in the existing SQLite cache
    (via the ``info`` key/value table -- no schema change) so an interrupted
    translation can be resumed from the last completed chapter.

The public entry points are:

    ChapterBuilder(page_ids, toc_nodes, manifest_items, paragraphs).build()
        -> list[Chapter], with auxiliary_paragraphs() for what is left out
    TokenBudget(budget).chunk(paragraphs, reserved) -> list[list[Paragraph]]
    ContextManager(cache).load() / append_chapter() / context_text()
    NovelTranslator(translator, chapters, context_manager, cache, config,
                    aux_paragraphs).run()

This module has no direct dependency on Qt so it is fully unit-testable.
"""

import re
import copy
import json
import time
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed

from calibre.utils.localization import _  # type: ignore

from .utils import log, sep, uid, dummy
from .exception import (
    TranslationCanceled, TranslationFailed, HTTPRequestError)


load_translations()  # type: ignore


def model_text(message):
    """Mark text that is sent to the model rather than shown to the user.

    It is a no-op at runtime; what it does is keep the string out of the
    translation catalogs. Prompt scaffolding must stay in one language,
    and it is not the language of the interface: a translator meeting
    these strings in a .po file has no way of telling them apart from
    labels, and translating them would hand the model instructions in
    one language wrapped around a prompt written in another.

    User-facing text -- log lines, progress, errors -- keeps using
    ``_()`` as usual.
    """
    return message


# ---------------------------------------------------------------------------
# Chapter model
# ---------------------------------------------------------------------------


class Chapter:
    """A logical chapter of the book.

    A chapter groups one or more page_ids together with the list of already
    extracted Paragraphs that belong to those pages. The ``index`` is 1-based
    and matches the order of the chapter inside the book (spine order).
    """

    def __init__(self, index, title, page_ids, paragraphs):
        self.index = index
        self.title = title or ''
        self.page_ids = list(page_ids)
        self.paragraphs = list(paragraphs)

    @property
    def char_count(self):
        return sum(len(p.original or '') for p in self.paragraphs
                   if not p.ignored)

    def translatable_paragraphs(self):
        return [p for p in self.paragraphs if not p.ignored]

    def __repr__(self):
        return ('Chapter(index=%s, title=%r, page_ids=%s, paragraphs=%d)'
                % (self.index, self.title, self.page_ids,
                   len(self.paragraphs)))


# ---------------------------------------------------------------------------
# Chapter builder
# ---------------------------------------------------------------------------


def _href_to_page_id(href, manifest_items):
    """Map an href (as found in TOC nodes) to a manifest item id.

    TOC hrefs often contain a fragment (``chap1.xhtml#section-2``) which must
    be stripped before comparing with the manifest item href.
    """
    if not href:
        return None
    clean = href.split('#', 1)[0]
    # Try exact match first.
    for item in manifest_items:
        if getattr(item, 'href', None) == clean:
            return item.id
    # Fallback: match by basename (some TOCs use relative paths).
    base = clean.rsplit('/', 1)[-1]
    for item in manifest_items:
        item_href = getattr(item, 'href', '') or ''
        if item_href.rsplit('/', 1)[-1] == base:
            return item.id
    return None


class ChapterBuilder:
    """Build ``Chapter`` objects from an OEB book and a flat paragraph list.

    Strategy:

    1. Determine the ordered list of "page ids" (the spine order used by the
       existing extraction pipeline: ``manifest.items`` filtered to xhtml
       and sorted by ``sorted_mixed_keys``).
    2. Determine chapter boundaries. Preference order:
         a. Top-level TOC nodes (level 1 only) if the TOC has 2+ nodes.
         b. Otherwise fall back to "one xhtml file == one chapter".
    3. Group paragraphs by (or between) those boundaries. Any paragraphs
       whose ``page`` id does not belong to any chapter range are attached
       to the closest previous chapter (typically front matter appended to
       chapter 1) so nothing is silently dropped.

    Only paragraphs coming from XHTML pages are considered as chapter
    content. Metadata and TOC paragraphs (page_id ``content.opf`` / ``toc.ncx``)
    are collected separately in ``self.aux_paragraphs`` so callers can decide
    what to do with them -- typically translated via the classic pipeline,
    keeping the novel pipeline focused on narrative content.
    """

    AUX_PAGES = {'content.opf', 'toc.ncx'}

    def __init__(self, ordered_page_ids, toc_nodes, manifest_items,
                 paragraphs, source='toc_level_1',
                 front_matter_min_chars=100):
        """
        :ordered_page_ids: list of page ids in spine order (xhtml only).
        :toc_nodes: list of TOC root nodes (each has ``.title`` and
            ``.href`` and ``.nodes``). May be an empty list.
        :manifest_items: iterable of manifest items exposing ``.id`` and
            ``.href``. Used to resolve TOC hrefs to page ids.
        :paragraphs: list of Paragraph rows extracted from the cache.
        :source: 'toc_level_1' | 'toc_level_2' | 'xhtml_file'.
        :front_matter_min_chars: XHTML pages whose total non-ignored text
            is shorter than this threshold are considered front/back matter
            (Cover, Titlepage, decorative pages). Their paragraphs are kept
            out of the chapters and handed back by
            :meth:`auxiliary_paragraphs` instead. Set to 0 to disable.
        """
        self.ordered_page_ids = list(ordered_page_ids)
        self.toc_nodes = list(toc_nodes or [])
        self.manifest_items = list(manifest_items or [])
        self.paragraphs = list(paragraphs)
        self.source = source
        self.front_matter_min_chars = max(0, int(front_matter_min_chars))
        self.aux_paragraphs = [
            p for p in self.paragraphs if p.page in self.AUX_PAGES]
        # Filled by build(): the paragraphs of the pages the front-matter
        # filter set aside.
        self.front_matter_paragraphs = []

        # Pre-compute per-page character counts (non-ignored paragraphs only)
        # used by the front-matter filter.
        self._page_char_count = {}
        for p in self.paragraphs:
            if p.page in self.AUX_PAGES or p.ignored:
                continue
            self._page_char_count[p.page] = (
                self._page_char_count.get(p.page, 0)
                + len(p.original or ''))

    def _is_front_matter(self, page_id):
        """Return True if ``page_id`` looks like a decorative / front-matter
        page that should be excluded from chapter content.

        Detection strategy:

        1. If ``front_matter_min_chars`` is 0, the filter is disabled.
        2. Pages with fewer non-ignored characters than the threshold are
           treated as front matter (e.g. a Titlepage that only carries
           ``"THE MAGICIAN'S NEPHEW"`` as a single heading paragraph, or a
           Cover page with nothing but an image).

        This handles the common EPUB pattern where each book inside a
        multi-book anthology has its own Cover.xhtml / Titlepage.xhtml that
        contains only a large-caps styled heading.  Sending those headings
        to the LLM as isolated paragraphs produces hallucinations because
        the model has no narrative context around them.
        """
        if self.front_matter_min_chars <= 0:
            return False
        return self._page_char_count.get(page_id, 0) < self.front_matter_min_chars

    # -- boundary discovery ------------------------------------------------

    def _boundaries_from_toc(self):
        """Return an ordered list of (page_id, title) marking chapter starts.

        Only top-level TOC nodes are considered. Nodes whose href cannot be
        resolved to a manifest item are silently skipped.
        """
        result = []
        seen = set()
        for node in self.toc_nodes:
            href = getattr(node, 'href', None)
            title = getattr(node, 'title', None) or ''
            page_id = _href_to_page_id(href, self.manifest_items)
            if page_id is None or page_id in seen:
                continue
            if page_id not in self.ordered_page_ids:
                continue
            seen.add(page_id)
            result.append((page_id, title.strip()))
        # Sort boundaries by spine order.
        order = {pid: i for i, pid in enumerate(self.ordered_page_ids)}
        result.sort(key=lambda t: order[t[0]])
        return result

    def _boundaries_from_toc_level2(self):
        """Return boundaries from the *second* level of the TOC hierarchy.

        Many multi-book anthologies have a two-level TOC:

          Level 1: Cover.xhtml   (entire book)
            Level 2: Chapter One  -> Chapter_1.xhtml
            Level 2: Chapter Two  -> Chapter_2.xhtml
            ...

        Using level-2 nodes as chapter boundaries maps each narrative chapter
        to its own translation unit, giving the LLM the chapter title in the
        prompt header and preventing front-matter pages (Cover, Titlepage,
        Contents) from polluting the first chunk.

        If the TOC has no level-2 children or fewer than 2 usable entries,
        falls back to level-1 boundaries.
        """
        result = []
        seen = set()
        for top_node in self.toc_nodes:
            children = getattr(top_node, 'nodes', []) or []
            for child in children:
                href = getattr(child, 'href', None)
                title = getattr(child, 'title', None) or ''
                page_id = _href_to_page_id(href, self.manifest_items)
                if page_id is None or page_id in seen:
                    continue
                if page_id not in self.ordered_page_ids:
                    continue
                seen.add(page_id)
                result.append((page_id, title.strip()))
        if len(result) < 2:
            # Not enough level-2 entries -- fall back to level-1.
            return self._boundaries_from_toc()
        order = {pid: i for i, pid in enumerate(self.ordered_page_ids)}
        result.sort(key=lambda t: order[t[0]])
        return result

    def _boundaries_from_files(self):
        """One xhtml file == one chapter. Titles are best-effort:

        we use the first paragraph text from the page (truncated) if a page
        has content, else 'Chapter N'.
        """
        result = []
        by_page = {}
        for p in self.paragraphs:
            if p.page in self.AUX_PAGES:
                continue
            by_page.setdefault(p.page, []).append(p)
        for i, pid in enumerate(self.ordered_page_ids, start=1):
            title = model_text('Chapter {}').format(i)
            content_ps = [p for p in by_page.get(pid, []) if not p.ignored]
            if content_ps:
                first = (content_ps[0].original or '').strip()
                if first:
                    title = first[:80]
            result.append((pid, title))
        return result

    def _resolve_boundaries(self):
        boundaries = []
        if self.source == 'toc_level_1':
            boundaries = self._boundaries_from_toc()
            if len(boundaries) < 2:
                boundaries = self._boundaries_from_files()
        elif self.source == 'toc_level_2':
            boundaries = self._boundaries_from_toc_level2()
            if len(boundaries) < 2:
                boundaries = self._boundaries_from_files()
        else:
            boundaries = self._boundaries_from_files()
        if not boundaries and self.ordered_page_ids:
            # Extreme fallback: single chapter with everything.
            boundaries = [
                (self.ordered_page_ids[0], model_text('Chapter 1'))]
        return boundaries

    # -- assembly ----------------------------------------------------------

    def build(self):
        boundaries = self._resolve_boundaries()
        if not boundaries:
            return []

        # Determine the (start_page_id -> [page_ids...]) mapping.
        order = {pid: i for i, pid in enumerate(self.ordered_page_ids)}
        boundary_positions = [(order[bid], bid, title)
                              for bid, title in boundaries]
        boundary_positions.sort(key=lambda t: t[0])

        # Build page_id -> chapter_index (1-based) mapping.
        page_to_chapter = {}
        chapter_page_ids = {i: [] for i in range(1, len(boundary_positions) + 1)}
        chapter_titles = {}
        for chap_i, (_pos, bid, title) in enumerate(
                boundary_positions, start=1):
            chapter_titles[chap_i] = (
                title or model_text('Chapter {}').format(chap_i))

        for idx, pid in enumerate(self.ordered_page_ids):
            # Find which chapter this page belongs to: the greatest chapter
            # start whose position <= idx.
            chap_i = 0
            for c_i, (pos, _bid, _t) in enumerate(
                    boundary_positions, start=1):
                if pos <= idx:
                    chap_i = c_i
                else:
                    break
            # If a page precedes the first boundary, attach it to chapter 1.
            if chap_i == 0:
                chap_i = 1
            page_to_chapter[pid] = chap_i
            chapter_page_ids[chap_i].append(pid)

        # Group paragraphs by chapter, applying the front-matter filter.
        # Pages identified as front matter (very short text, e.g. Cover,
        # Titlepage) are kept out of the narrative payload: sent to the
        # model as isolated lines inside a chapter they invite
        # hallucination. They are not dropped either -- a dedication or
        # a part divider left in the source language is a hole in the
        # book -- but set aside for ``auxiliary_paragraphs``, which the
        # translator handles apart from the chapters.
        chapter_paragraphs = {i: [] for i in chapter_titles}
        self.front_matter_paragraphs = []
        for p in self.paragraphs:
            if p.page in self.AUX_PAGES:
                continue
            chap_i = page_to_chapter.get(p.page)
            if chap_i is None:
                continue
            if self._is_front_matter(p.page):
                self.front_matter_paragraphs.append(p)
                continue
            chapter_paragraphs[chap_i].append(p)

        chapters = []
        for i in sorted(chapter_titles):
            ch = Chapter(
                index=i,
                title=chapter_titles[i],
                page_ids=chapter_page_ids[i],
                paragraphs=chapter_paragraphs[i],
            )
            chapters.append(ch)
        return chapters

    def auxiliary_paragraphs(self):
        """Everything :meth:`build` left out of the chapters: the metadata
        and table-of-contents entries, and the paragraphs of the pages the
        front-matter filter set aside. The translator sends them apart from
        the narrative, with no running context."""
        return list(self.aux_paragraphs) + list(self.front_matter_paragraphs)


# ---------------------------------------------------------------------------
# Token budget / chunking
# ---------------------------------------------------------------------------


_CJK_RE = re.compile(
    r'[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\u3400-\u4dbf'
    r'\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]')


class TokenBudget:
    """Cheap token estimation and dual-cap chunking, char-based.

    We deliberately avoid heavy dependencies (tiktoken and friends): they are
    accurate for GPT/Claude but wrong for Gemma/Mistral/others and would add
    a >1MB payload for marginal gain. We use a simple heuristic:

      * Latin/Cyrillic/etc: ~4 chars per token.
      * CJK: ~2 chars per token (each ideograph is often one token).

    Chunking is driven by **two independent caps**, both enforced
    simultaneously: whichever is reached first closes the current chunk.

      * ``budget``: maximum estimated tokens per chunk. Prevents overflowing
        the model's context window.
      * ``max_paragraphs``: maximum non-ignored paragraphs per chunk.
        This is the cap that stands in for the model's *output* limit,
        which nothing else here measures: the translation of a chunk is
        about as long as the chunk itself, while a model that reads
        200k tokens will only write 8k to 32k of them. A chunk sized
        purely by ``budget`` therefore asks for a reply the model
        cannot finish, and the answer arrives truncated. It also keeps
        the LLM from being overwhelmed by too many alignment markers
        when paragraphs are short (dialogue, TOC lists, one-line
        stanzas); medium-sized local models start losing markers
        reliably above ~60-80 per chunk. Set to 0 to disable this cap
        and fall back to token-only chunking.

    Paragraphs that alone exceed the per-chunk budget are still emitted as
    a single-paragraph chunk (with a warning) rather than being split
    mid-sentence.
    """

    # Reasons why a chunk was closed. Exposed via ``chunk_with_stats``.
    REASON_TOKENS = 'tokens'
    REASON_PARAGRAPHS = 'paragraphs'
    REASON_OVERSIZED = 'oversized'
    REASON_END = 'end'

    def __init__(self, budget=16000, max_paragraphs=100,
                 ratio_latin=4.0, ratio_cjk=2.0, cjk_threshold=0.30):
        if budget < 100:
            budget = 100
        self.budget = int(budget)
        # 0 (or negative) disables the paragraph cap.
        self.max_paragraphs = max(0, int(max_paragraphs or 0))
        self.ratio_latin = float(ratio_latin)
        self.ratio_cjk = float(ratio_cjk)
        self.cjk_threshold = float(cjk_threshold)

    def estimate(self, text):
        if not text:
            return 0
        total = len(text)
        cjk = len(_CJK_RE.findall(text))
        # If more than threshold of the text is CJK, use CJK ratio for the
        # whole string (which slightly overestimates -- desirable, we want
        # a safety margin).
        if total > 0 and (cjk / total) >= self.cjk_threshold:
            return max(1, int(total / self.ratio_cjk))
        # Mixed: apply CJK ratio to CJK chars and Latin ratio to the rest.
        latin = total - cjk
        return max(1, int(cjk / self.ratio_cjk + latin / self.ratio_latin))

    def chunk(self, paragraphs, reserved=0):
        """Split ``paragraphs`` into contiguous chunks respecting both the
        token budget and the paragraph cap.

        :reserved: number of tokens to leave available for the system prompt,
            the running summary, the glossary and the model reply. The
            effective per-chunk budget is ``self.budget - reserved`` with a
            minimum of 200 tokens.

        Returns a list of paragraph-lists. Use ``chunk_with_stats`` to also
        obtain the per-chunk token estimate and the reason why each chunk
        was closed (useful for tuning / logging).
        """
        return [c for c, _tok, _reason
                in self.chunk_with_stats(paragraphs, reserved)]

    def chunk_with_stats(self, paragraphs, reserved=0):
        """Same as :meth:`chunk` but returns list of tuples
        ``(chunk, tokens_estimate, reason)`` where ``reason`` is one of
        ``TokenBudget.REASON_*``. The reason indicates which limit closed
        the chunk (or ``REASON_END`` for the tail chunk).
        """
        available = max(200, self.budget - int(reserved))
        chunks = []
        current = []
        current_tokens = 0
        current_translatable = 0  # non-ignored paragraph count

        for p in paragraphs:
            if getattr(p, 'ignored', False):
                # Ignored paragraphs still travel through the pipeline (they
                # must be re-injected into the DOM) but they consume no
                # tokens for translation and do not count toward the
                # paragraph cap.
                current.append(p)
                continue

            text = p.original or ''
            tokens = self.estimate(text)

            # Oversized single paragraph: emit alone (do not split mid-
            # sentence). If ``current`` is non-empty flush it first so
            # ordering is preserved.
            if tokens >= available:
                if current:
                    # Determine closing reason for the flushed chunk.
                    reason = self.REASON_TOKENS
                    if (self.max_paragraphs
                            and current_translatable >= self.max_paragraphs):
                        reason = self.REASON_PARAGRAPHS
                    chunks.append((current, current_tokens, reason))
                    current = []
                    current_tokens = 0
                    current_translatable = 0
                log.warn(
                    'Novel mode: paragraph estimated at %d tokens exceeds '
                    'per-chunk budget %d; sending as-is.'
                    % (tokens, available))
                chunks.append(([p], tokens, self.REASON_OVERSIZED))
                continue

            would_exceed_tokens = current_tokens + tokens > available
            would_exceed_paragraphs = (
                self.max_paragraphs
                and current_translatable >= self.max_paragraphs)

            if (would_exceed_tokens or would_exceed_paragraphs) and current:
                # Which cap fired first? If both, tokens takes precedence
                # (the harder limit for the model).
                if would_exceed_tokens:
                    reason = self.REASON_TOKENS
                else:
                    reason = self.REASON_PARAGRAPHS
                chunks.append((current, current_tokens, reason))
                current = [p]
                current_tokens = tokens
                current_translatable = 1
                continue

            current.append(p)
            current_tokens += tokens
            current_translatable += 1

        if current:
            chunks.append((current, current_tokens, self.REASON_END))
        return chunks

    REASON_BALANCED = 'balanced'

    def balance(self, chunks_with_stats, reserved=0):
        """Redistribute the paragraphs of ``chunks_with_stats`` (as
        :meth:`chunk_with_stats` returns them) over the same number of
        chunks so that each carries about the same tokens.

        Filling every chunk to the cap and leaving the remainder to the
        last one is right when chunks go one after the other: it is the
        fewest requests. When they are in flight together a chapter
        takes as long as its longest chunk, and 3637 + 1945 + 674 tokens
        is a chapter that waits for the first while the third is done
        in a quarter of the time; 2085 + 2085 + 2086 is one that is
        done in two thirds of it. The caps still hold, and a chunk is
        closed at the paragraph that brings its total closest to its
        share.
        """
        if len(chunks_with_stats) < 2:
            return chunks_with_stats
        available = max(200, self.budget - int(reserved))
        paragraphs = [p for chunk, _t, _r in chunks_with_stats for p in chunk]
        weights = [
            0 if getattr(p, 'ignored', False)
            else self.estimate(p.original or '') for p in paragraphs]
        total = sum(weights)
        wanted = len(chunks_with_stats)
        if not total:
            return chunks_with_stats
        share = total / float(wanted)
        chunks = []
        current, current_tokens, current_translatable = [], 0, 0
        for p, tokens in zip(paragraphs, weights):
            if getattr(p, 'ignored', False):
                current.append(p)
                continue
            target = share * (len(chunks) + 1)
            done = sum(t for _c, t, _r in chunks)
            over_cap = (
                current_tokens + tokens > available
                or (self.max_paragraphs
                    and current_translatable >= self.max_paragraphs))
            # Close when adding this paragraph would leave the running
            # total further from the chunk's share than stopping here,
            # as long as there are chunks left to fill.
            past_share = current and len(chunks) < wanted - 1 and abs(
                done + current_tokens + tokens - target) > abs(
                    done + current_tokens - target)
            if current and (over_cap or past_share):
                chunks.append((current, current_tokens, self.REASON_BALANCED))
                current, current_tokens, current_translatable = [], 0, 0
            current.append(p)
            current_tokens += tokens
            current_translatable += 1
        if current:
            chunks.append((current, current_tokens, self.REASON_END))
        return chunks


# ---------------------------------------------------------------------------
# Context / summary / glossary manager
# ---------------------------------------------------------------------------


# Cache info keys (single source of truth).
INFO_NOVEL_MODE = 'novel_mode'
INFO_NOVEL_SUMMARIES = 'novel_summaries'
INFO_NOVEL_GLOSSARY = 'novel_glossary'
INFO_NOVEL_PROGRESS = 'novel_progress'
INFO_NOVEL_CHAPTERS = 'novel_chapters_meta'
INFO_NOVEL_STYLE = 'novel_author_style'
# Why there is no brief, when there is none: shown in the window where
# the brief would be, instead of an empty tab.
INFO_NOVEL_STYLE_NOTE = 'novel_author_style_note'
# The log of the last runs, kept with the book so the window can show
# it again after being closed: what the author brief answered, why a
# chapter's summary was dropped, where a run stopped.
INFO_NOVEL_LOG = 'novel_log'
# What the runs on the book cost so far -- requests, tokens, money, per
# kind of request and per provider -- and the report drawn from it.
INFO_NOVEL_USAGE = 'novel_usage'
INFO_NOVEL_REPORT = 'novel_report'


# Words of a glossary key that are worth matching on their own. Four
# characters keeps out the particles and articles ("of", "the", "van")
# that would match every chapter ever written.
_KEY_WORD_RE = re.compile(r'\w{4,}', re.UNICODE)


def _mentions(haystack, name):
    """Whether ``name`` (a glossary key) turns up in ``haystack``.

    ``haystack`` must already be casefolded. See
    :meth:`ContextManager.glossary_for` for why a multi-word name also
    matches on its longest word alone.
    """
    folded = name.casefold()
    if folded in haystack:
        return True
    words = _KEY_WORD_RE.findall(folded)
    return len(words) > 1 and max(words, key=len) in haystack


class ContextManager:
    """Persist and expose the running context for a novel translation.

    Two pieces of context are maintained:

      * ``summaries``: an ordered list of dicts
        ``{"chapter": int, "title": str, "summary": str}``, one per chapter
        that has already been translated.
      * ``style``: a translator's brief on how the book is written,
        researched once before the first chapter (see
        ``NovelTranslator._ensure_author_style``) and reused for every
        chapter afterwards. Empty when the research is off, impossible
        or came back with nothing.
      * ``glossary``: a dict mapping the original name (source language) to
        a dict ``{"translation": str, "type": str, "notes": str}``. The type
        is a free-form label (character/place/object/other/...); the notes
        are optional.

    Both are serialized as JSON in the SQLite ``info`` key/value table of the
    translation cache. No schema change is required.
    """

    def __init__(self, cache, glossary_max_entries=200):
        """
        :cache: a ``TranslationCache`` instance.
        :glossary_max_entries: hard cap. When new entries would exceed it,
            oldest entries are dropped (FIFO). Set to 0 for no limit.
        """
        self.cache = cache
        self.glossary_max_entries = int(glossary_max_entries or 0)
        self.summaries = []
        self.glossary = {}
        self.style = ''
        self.progress = 0

    # -- persistence -------------------------------------------------------

    def load(self):
        raw = self.cache.get_info(INFO_NOVEL_SUMMARIES)
        try:
            self.summaries = json.loads(raw) if raw else []
            if not isinstance(self.summaries, list):
                self.summaries = []
        except (ValueError, TypeError):
            self.summaries = []

        raw = self.cache.get_info(INFO_NOVEL_GLOSSARY)
        try:
            self.glossary = json.loads(raw) if raw else {}
            if not isinstance(self.glossary, dict):
                self.glossary = {}
        except (ValueError, TypeError):
            self.glossary = {}

        raw = self.cache.get_info(INFO_NOVEL_STYLE)
        self.style = raw if isinstance(raw, str) else ''

        raw = self.cache.get_info(INFO_NOVEL_PROGRESS)
        try:
            self.progress = int(raw) if raw else 0
        except (ValueError, TypeError):
            self.progress = 0

        self.cache.set_info(INFO_NOVEL_MODE, '1')
        return self

    def _persist(self):
        self.cache.set_info(
            INFO_NOVEL_SUMMARIES, json.dumps(
                self.summaries, ensure_ascii=False))
        self.cache.set_info(
            INFO_NOVEL_GLOSSARY, json.dumps(
                self.glossary, ensure_ascii=False))
        self.cache.set_info(INFO_NOVEL_STYLE, self.style or '')
        self.cache.set_info(INFO_NOVEL_PROGRESS, str(self.progress))

    # -- getters -----------------------------------------------------------

    def get_progress(self):
        return self.progress

    def get_summaries(self):
        return list(self.summaries)

    def get_glossary(self):
        return dict(self.glossary)

    def get_style(self):
        return self.style or ''

    def set_style(self, text):
        """Store the author brief. Persisted like the rest of the context,
        so a resumed run reuses it instead of paying for the research
        again. A brief replaces any note on why there was none."""
        self.style = (text or '').strip()
        self._persist()
        if self.style:
            self.cache.set_info(INFO_NOVEL_STYLE_NOTE, '')
        return self.style

    def set_style_note(self, text):
        """Record why there is no brief, for the window to show."""
        self.cache.set_info(INFO_NOVEL_STYLE_NOTE, (text or '').strip())

    def get_style_note(self):
        raw = self.cache.get_info(INFO_NOVEL_STYLE_NOTE)
        return raw if isinstance(raw, str) else ''

    def glossary_for(self, text=None, limit=0, glossary=None):
        """Return the glossary entries that ``text`` actually mentions.

        The glossary grows with every chapter, while a chapter only needs
        the names it contains. Sending all of them costs input tokens on
        every request, and in the extraction call it does worse than
        that: faced with a list of several hundred names to skip, models
        have been observed copying the whole list back as "new" entries
        until they hit their output limit.

        Matching is a case-insensitive substring test, so inflected and
        possessive forms ("Fidelma's") and scripts that do not separate
        words still find their entry. A name of several words also
        matches on its longest word alone, because that is the form the
        prose actually uses: a chapter that never writes "Bishop
        Gelasius" in full still needs the entry when it says "Gelasius".
        The looser test can let an entry through that the chapter does
        not really name, which costs one line of prompt; the strict one
        would drop a name the chapter does use, which costs a
        mistranslation.

        :text: the chapter text -- source, translation, or both. ``None``
            means no filtering, i.e. the whole glossary.
        :limit: keep at most this many entries, the most recently learned
            ones, since the older an entry is the more chapters the model
            has already seen it in. 0 means no limit.
        """
        entries = self.glossary if glossary is None else glossary
        if text:
            haystack = text.casefold()
            entries = {
                source: entry for source, entry in entries.items()
                if source and _mentions(haystack, source)}
        limit = int(limit or 0)
        if limit and len(entries) > limit:
            entries = {
                key: entries[key] for key in list(entries)[-limit:]}
        return dict(entries)

    # -- mutation ----------------------------------------------------------

    def append_chapter(self, chapter_index, title, summary,
                       glossary_updates=None):
        """Record that ``chapter_index`` was completed.

        :summary: the summary text (already in the target language).
        :glossary_updates: iterable of dicts with at least ``source`` and
            ``translation`` keys; ``type`` and ``notes`` are optional.
            Duplicates (by ``source``) update the existing entry.
        """
        summary = (summary or '').strip()
        if summary or title:
            entry = {
                'chapter': int(chapter_index),
                'title': title or '',
                'summary': summary,
            }
            # A chapter translated a second time replaces its own entry:
            # appending would repeat it in every later prompt.
            for i, existing in enumerate(self.summaries):
                if existing.get('chapter') == entry['chapter']:
                    self.summaries[i] = entry
                    break
            else:
                self.summaries.append(entry)
        if glossary_updates:
            self._merge_glossary(glossary_updates)
        # Progress advances only forward.
        if chapter_index > self.progress:
            self.progress = int(chapter_index)
        self._persist()

    def _merged_glossary(self, updates):
        """A copy of the glossary with ``updates`` merged in; the stored
        glossary is left alone."""
        glossary = {key: dict(value) for key, value in self.glossary.items()}
        for item in updates:
            if not isinstance(item, dict):
                continue
            source = (item.get('source') or '').strip()
            translation = (item.get('translation') or '').strip()
            if not source or not translation:
                continue
            entry = glossary.get(source, {})
            entry['translation'] = translation
            if item.get('type'):
                entry['type'] = str(item['type']).strip()
            if item.get('notes'):
                entry['notes'] = str(item['notes']).strip()
            glossary[source] = entry
        return glossary

    def _merge_glossary(self, updates):
        self.glossary = self._merged_glossary(updates)
        # Enforce cap (FIFO on insertion order preserved by dict).
        if self.glossary_max_entries and \
                len(self.glossary) > self.glossary_max_entries:
            overflow = len(self.glossary) - self.glossary_max_entries
            for key in list(self.glossary.keys())[:overflow]:
                del self.glossary[key]

    def replace_glossary(self, new_glossary):
        """Wholesale replacement (used by the UI editor)."""
        self.glossary = {
            k: v for k, v in new_glossary.items()
            if isinstance(v, dict) and v.get('translation')}
        self._persist()

    def reset(self):
        self.summaries = []
        self.glossary = {}
        self.style = ''
        self.progress = 0
        self._persist()
        self.cache.set_info(INFO_NOVEL_STYLE_NOTE, '')

    def reset_progress(self):
        """Forget which chapters are done and keep everything learned
        from them, so a book can be translated again with the summaries,
        the glossary and the author brief it already has."""
        self.progress = 0
        self._persist()

    # -- context composition ----------------------------------------------

    def _format_summaries(self, summaries):
        if not summaries:
            return model_text('(none)')
        lines = []
        for s in summaries:
            title = s.get('title') or ''
            head = model_text('Chapter {n}').format(
                n=s.get('chapter', '?'))
            if title:
                head = '%s - %s' % (head, title)
            body = (s.get('summary') or '').strip()
            if body:
                lines.append('- %s: %s' % (head, body))
            else:
                lines.append('- %s' % head)
        return '\n'.join(lines)

    def _format_glossary(self, glossary):
        if not glossary:
            return model_text('(empty)')
        lines = []
        for source, entry in glossary.items():
            translation = entry.get('translation', '')
            gtype = entry.get('type', '')
            notes = entry.get('notes', '')
            extras = []
            if gtype:
                extras.append(gtype)
            if notes:
                extras.append(notes)
            suffix = ' (%s)' % ', '.join(extras) if extras else ''
            lines.append('- %s -> %s%s' % (source, translation, suffix))
        return '\n'.join(lines)

    def context_text(self, budget_tokens=4000, ratio=4.0,
                     relevant_to=None, glossary_limit=0, pending=None):
        """Return a formatted string containing the recent summaries and the
        glossary, truncated to fit ``budget_tokens`` (approximate).

        Priority when trimming (from most to least important, i.e. dropped
        last): glossary > most recent summaries > older summaries.

        :relevant_to: when given, only the glossary entries this text
            mentions are included (see :meth:`glossary_for`). Trimming by
            budget drops glossary lines from the end, so a book long
            enough to fill the budget would otherwise lose exactly the
            names it learned most recently.
        :glossary_limit: hard cap on the number of glossary entries.
        :pending: ``(summary_entry, glossary_updates)`` of the chapter
            being translated, worked out from its source before its
            translation started: shown with the rest but not stored
            until the chapter is done.
        """
        max_chars = max(200, int(budget_tokens * ratio))

        summaries = list(self.summaries)
        glossary = None
        if pending:
            entry, updates = pending
            if entry and (entry.get('summary') or '').strip():
                summaries.append(entry)
            if updates:
                glossary = self._merged_glossary(updates)

        glossary_text = self._format_glossary(
            self.glossary_for(
                relevant_to, limit=glossary_limit, glossary=glossary))
        summaries_text = self._format_summaries(summaries)

        combined = (
            model_text('Story so far (previous chapters summary):') + '\n'
            + summaries_text + '\n\n'
            + model_text('Glossary (use these exact translations):') + '\n'
            + glossary_text)

        if len(combined) <= max_chars:
            return combined

        # Progressively drop oldest summaries.
        while len(summaries) > 1:
            summaries = summaries[1:]
            summaries_text = self._format_summaries(summaries)
            combined = (
                model_text('Story so far (previous chapters summary):') + '\n'
                + summaries_text + '\n\n'
                + model_text('Glossary (use these exact translations):') + '\n'
                + glossary_text)
            if len(combined) <= max_chars:
                return combined

        # Still too big: truncate glossary lines.
        glossary_lines = glossary_text.split('\n')
        while glossary_lines and len(combined) > max_chars:
            glossary_lines.pop()
            glossary_text = '\n'.join(glossary_lines) \
                or model_text('(truncated)')
            combined = (
                model_text('Story so far (previous chapters summary):') + '\n'
                + summaries_text + '\n\n'
                + model_text('Glossary (use these exact translations):') + '\n'
                + glossary_text)
        return combined


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


# The shipped prompt holds only craft rules that hold for any novel:
# what to do with register, period, voice and tone. Anything specific to
# an author, a series or a single book belongs in the "Translation
# prompt" field of the Novel Mode settings, which replaces this text.
#
# The three placeholders at the end are filled by
# NovelTranslator._translation_system_prompt and are ordered by how often
# they change: the dialogue rules and the author brief are computed once
# for the whole book, the running context changes with every chapter. A
# provider's prefix cache can only reuse what comes before the first byte
# that differs, so anything constant has to sit above {context}.
DEFAULT_NOVEL_TRANSLATION_PROMPT = (
    'You are a professional literary translator. You are translating a '
    'novel from <slang> to <tlang>, chapter by chapter, and the result '
    'has to read like a book published in <tlang>: real prose in the '
    'target language that still says exactly what the author said, no '
    'more and no less.\n\n'

    'Meaning over form. Translate the effect, not the surface. Where the '
    'syntax, idiom, wordplay or sound of the original cannot be '
    'reproduced naturally, choose the expression that best preserves its '
    'meaning, tone, characterisation and narrative rhythm. Prefer '
    'precise, idiomatic phrasing over word-for-word rendering, and let '
    'each sentence flow naturally in <tlang> while keeping the pacing, '
    'the sentence lengths and the paragraph movement of the '
    'original.\n\n'

    'Voice and register. Preserve the author\'s narrative voice and '
    'register exactly as they are. Do not raise or lower the literary '
    'register, do not add ornate vocabulary, melodrama or emphasis the '
    'original does not have, and do not smooth out prose that is '
    'deliberately plain, abrupt, dense or repetitive. When the author '
    'repeats a word, repeat it: a deliberate repetition is not a mistake '
    'to be varied away. Keep the same person, the same tense and the '
    'same distance between narrator and reader, unless <tlang> makes '
    'that impossible.\n\n'

    'Characters. Keep the characters\' voices distinct through '
    'vocabulary, syntax, register and conversational habits rather than '
    'through exaggerated dialect. Do not flatten the difference between '
    'educated and uneducated speech, or between formal and familiar '
    'address. Where <tlang> distinguishes formal from familiar address, '
    'choose the form the relationship between the two characters implies '
    'and keep it stable for that pair until the story itself changes '
    'it.\n\n'

    'Nothing invented, nothing lost. Do not explain, expand, summarise, '
    'annotate, or make explicit what the original leaves implicit. Do '
    'not resolve ambiguity the author chose to leave open, do not add or '
    'remove humour, and do not omit any part of the text however '
    'unimportant it may seem. Invent nothing: not a detail, not a '
    'clarifying subject, not a connective the text does not imply. When '
    'a passage is obscure, elliptical or looks damaged, translate it as '
    'it stands instead of repairing it, and never leave a note, a '
    'bracket or a remark of your own inside the text.\n\n'

    'Period and setting. Match the period and the setting of the '
    'original. Render period terms, titles, occupations, objects, '
    'currencies, institutions and units of measure with the most natural '
    'and historically credible equivalents, without archaising: do not '
    'scatter archaic words merely to signal that the story is set in the '
    'past, and do not modernise an idiom into something that could only '
    'be said today. Leave units, dates and measurements as the author '
    'wrote them; do not convert them.\n\n'

    'Names and terms. Keep names, places and recurring terms exactly as '
    'the running context below renders them, and consistent with '
    'themselves from one chapter to the next. Leave a proper name in its '
    'original form unless the glossary gives another rendering or <tlang> '
    'has a long-established equivalent. Whatever the author left in a '
    'foreign language stays in that language.\n\n'

    'Typography. Reproduce the punctuation and the typography of the '
    'source as closely as <tlang> allows: paragraph breaks, italics, '
    'capitals, ellipses, dashes and the way emphasis is marked. Change '
    'only what would be plainly wrong in <tlang>.\n\n'

    '{dialogue}\n\n'

    '{style}\n\n'

    '{context}')


# Format instructions live in the USER message, not in the system prompt.
# Local models (Gemma, Mistral, LLaMA) tend to "forget" formatting rules
# placed in a long system prompt but respect them when they are the last
# thing they read before the actual task.
# The rules are the same for every request of the whole book, the source
# block changes with every one of them: they are kept apart so a request
# can put what never changes first, where a provider's prompt cache can
# reuse it.
DEFAULT_NOVEL_FORMAT_RULES = (
    'Translate each numbered paragraph below. Rules:\n'
    '1) Keep the exact marker "[N]" on its own line before each translated '
    'paragraph, in the same order and with the same numbers as the source.\n'
    '2) Do NOT drop, add, split, merge or renumber paragraphs. The '
    'number above a translation is the number above its source, whatever '
    'came before it: never number on from a paragraph you skipped.\n'
    '3) Do NOT translate the markers themselves.\n'
    '4) Preserve verbatim any inline placeholder like {id_XXXXX} '
    '(they represent images, line breaks and similar).\n'
    '5) Translate every paragraph in full, however long: never shorten '
    'one or stand in for part of it with an ellipsis such as "[…]".\n'
    '6) Reply with the numbered paragraphs only. No preamble, no '
    'explanation, no closing remarks.')

DEFAULT_NOVEL_FORMAT_SOURCE = 'Source paragraphs:\n\n{text}'


# What the summary call answers, and the combined call flags, when the
# chapter is not part of the story: a copyright page, a list of the
# author's other books, a preface, an afterword, notes. A summary of
# those was carried into every later prompt as if it were plot.
NOT_A_STORY_CHAPTER = 'NOT A STORY CHAPTER'

DEFAULT_NOVEL_SUMMARY_PROMPT = (
    'Summarize the following chapter of a novel in <tlang>. '
    'Write 150 to 350 words. Focus on: plot events, character '
    'introductions and developments, key locations, and any information '
    'that will be useful to translate the following chapters consistently '
    '(e.g. relationships between characters, unresolved threads). '
    'Do not include any preamble or metacommentary; return the summary '
    'text only.\n\n'
    'If the chapter is not part of the story itself -- a copyright page, '
    'a list of other books, a dedication, a table of contents, a '
    'preface, an afterword, notes, a biography of the author -- reply '
    'with exactly: %s\n\n'
    'Chapter {chapter_num}: "{chapter_title}"\n\n'
    '{text}') % NOT_A_STORY_CHAPTER


DEFAULT_NOVEL_GLOSSARY_PROMPT = (
    'You extract named entities from a translated novel chapter. '
    'List NEW entities not already in the existing list: characters, '
    'places, unique objects, organizations.\n\n'
    'Reply with ONLY a JSON object. No preamble, no explanation, no '
    'markdown fences. Follow this exact schema:\n\n'
    '{"entities": [\n'
    '  {"source": "Aslan", "translation": "Aslan", "type": "character", '
    '"notes": "the lion"},\n'
    '  {"source": "Narnia", "translation": "Narnia", "type": "place", '
    '"notes": ""}\n'
    ']}\n\n'
    'If no new entities, reply exactly: {"entities": []}\n\n'
    'Hard limit: {max_entities} entries, the most important first; '
    'count them and stop there. Never list the same name twice, and '
    'never list a name that is already known.\n\n'
    'Already known (skip these): {existing_keys}\n\n'
    'Source:\n{source_text}\n\n'
    'Translation:\n{translated_text}')


# Asks in one request for what DEFAULT_NOVEL_SUMMARY_PROMPT and
# DEFAULT_NOVEL_GLOSSARY_PROMPT ask in two. Both need the chapter that
# was just translated, so keeping them apart means sending it twice: on
# a measured book the summary call carried 7000 to 8000 tokens of
# chapter text that the glossary call was about to send again. The
# summary comes first in the reply on purpose -- it is the field that
# survives if the answer is cut short.
DEFAULT_NOVEL_CONTEXT_PROMPT = (
    'You have just translated a chapter of a novel. Report on it with a '
    'single JSON object holding a summary and the named entities it '
    'introduced.\n\n'
    'Reply with ONLY that JSON object. No preamble, no explanation, no '
    'markdown fences. Follow this exact schema:\n\n'
    '{"narrative": true, "summary": "...", "entities": [\n'
    '  {"source": "Aslan", "translation": "Aslan", "type": "character", '
    '"notes": "the lion"},\n'
    '  {"source": "Narnia", "translation": "Narnia", "type": "place", '
    '"notes": ""}\n'
    ']}\n\n'
    '"narrative" is true when the chapter is part of the story itself '
    'and false for anything around it: a copyright page, a list of '
    'other books, a dedication, a table of contents, a preface, an '
    'afterword, notes, a biography of the author. When it is false, '
    'make "summary" one sentence saying what the chapter is and '
    '"entities" an empty list.\n\n'
    '"summary" is 150 to 350 words in <tlang>. Focus on plot events, '
    'character introductions and developments, key locations, and '
    'anything that will help translate the following chapters '
    'consistently (relationships between characters, unresolved '
    'threads). No preamble, no metacommentary.\n\n'
    '"entities" lists only the NEW characters, places, unique objects '
    'and organizations, each with the translation you used for it, the '
    'most important first. Hard limit: {max_entities} entries. Count '
    'them and stop at {max_entities}, whatever is left out; a longer '
    'list is cut off unread. Never list the same name twice, and never '
    'list a name that is already known. Use an empty list when there '
    'are none.\n\n'
    'Write "narrative" and "summary" before "entities": a reply cut '
    'short must still carry the summary.\n\n'
    'Chapter {chapter_num}: "{chapter_title}"\n\n'
    'Already known (skip these): {existing_keys}\n\n'
    'Source:\n{source_text}\n\n'
    'Translation:\n{translated_text}')


# The same report, asked before the chapter is translated and from its
# source alone (``novel_context_timing`` = 'before'). Half the tokens of
# the report above, which sends the chapter twice, and the glossary it
# yields is already in the prompt when the chapter's own chunks are
# translated: a name is rendered the same way in chunk 1 and chunk 3.
DEFAULT_NOVEL_CONTEXT_SOURCE_PROMPT = (
    'You are about to translate a chapter of a novel into <tlang>. '
    'Before it is translated, report on it with a single JSON object '
    'holding a summary and the named entities it introduces, with the '
    'rendering each of them is to have in <tlang>.\n\n'
    'Reply with ONLY that JSON object. No preamble, no explanation, no '
    'markdown fences. Follow this exact schema:\n\n'
    '{"narrative": true, "summary": "...", "entities": [\n'
    '  {"source": "Aslan", "translation": "Aslan", "type": "character", '
    '"notes": "the lion"},\n'
    '  {"source": "Narnia", "translation": "Narnia", "type": "place", '
    '"notes": ""}\n'
    ']}\n\n'
    '"narrative" is true when the chapter is part of the story itself '
    'and false for anything around it: a copyright page, a list of '
    'other books, a dedication, a table of contents, a preface, an '
    'afterword, notes, a biography of the author. When it is false, '
    'make "summary" one sentence saying what the chapter is and '
    '"entities" an empty list.\n\n'
    '"summary" is 150 to 350 words in <tlang>. Focus on plot events, '
    'character introductions and developments, key locations, and '
    'anything that will help translate this chapter and the following '
    'ones consistently (relationships between characters, unresolved '
    'threads). No preamble, no metacommentary.\n\n'
    '"entities" lists only the NEW characters, places, unique objects '
    'and organizations, each with the rendering to use for it in '
    '<tlang>: keep a proper name in its original form unless <tlang> '
    'has a long-established equivalent, and render descriptive names, '
    'titles, and the names of objects and institutions the way a '
    'published translation would. The most important first. Hard '
    'limit: {max_entities} entries. Count them and stop at '
    '{max_entities}, whatever is left out; a longer list is cut off '
    'unread. Never list the same name twice, and never list a name '
    'that is already known. Use an empty list when there are none.\n\n'
    'Write "narrative" and "summary" before "entities": a reply cut '
    'short must still carry the summary.\n\n'
    'Chapter {chapter_num}: "{chapter_title}"\n\n'
    'Already known (skip these): {existing_keys}\n\n'
    'Source:\n{source_text}')


# Asked once per book, before the first chapter, when
# ``novel_author_style`` allows it. The answer is a translator's brief on
# how the book is written; it is stored with the summaries and the
# glossary and prepended to the system prompt of every chapter.
#
# The escape hatch matters more than the brief: a model that has never
# heard of the author will happily invent a manner for them, and a made
# up brief would steer every paragraph of the book. Hence the exact
# ``NO INFORMATION`` reply, which the pipeline recognises and discards.
NO_AUTHOR_INFORMATION = 'NO INFORMATION'

# Asked before the brief: whether the model knows this author at all.
# Asked for a brief on an invented name, a model wrote a confident one
# about "the author's psychological thrillers"; asked first whether it
# can name real books by that name, the same model says no. The titles
# it names go into the brief request, which anchors it on that author.
DEFAULT_NOVEL_AUTHOR_CHECK_PROMPT = (
    'Is "{author}" a published novelist whose books you know? Answer '
    'with one JSON object and nothing else, in this exact shape:\n'
    '{"recognised": true, "works": ["...", "..."], "shared_name": false, '
    '"note": "..."}\n\n'
    '"recognised" is true only if you can name real, published books by '
    'exactly this author. "works" lists up to five of them, real titles '
    'only, none invented; an empty list when you know none. '
    '"shared_name" is true if more than one published writer has this '
    'name. "note" is one sentence on who this author is, or "unknown".\n\n'
    'Author: {author}\n'
    'Book in hand: {title}')


# The brief is about the author's manner in general, not about the
# book: a model asked about "this author and this book" declined every
# time it did not know the book itself, and one given web results about
# the author's other series placed the book in the wrong century. The
# translator has the text for everything the book alone would tell.
DEFAULT_NOVEL_AUTHOR_STYLE_PROMPT = (
    'You are preparing a brief for the translator of a novel by '
    '{author}. Describe how this author habitually writes, from what '
    'you know of their published work and of what critics, reviewers '
    'and translators have said about it, so that the manner can be '
    'reproduced in <tlang>. Do not search anything.\n\n'
    'The brief is about the author\'s manner in general, not about this '
    'book: the translator has the text and will see for themselves what '
    'it is about, where and when it is set and who is in it. Say '
    'nothing about the plot, the setting, the period, the characters or '
    'the series of this book, and do not guess any of them from the '
    'title. Describe only what holds across the author\'s work.\n\n'
    'Make sure you are describing this author and no other. If the '
    'name belongs to several writers and you cannot tell which one '
    'wrote this book, or if you know the name but not the prose, do not '
    'describe someone else and do not fill the gaps with what novels of '
    'that kind are usually like: give the reply below instead.\n\n'
    'Cover, in this order and only what you can support: the genres the '
    'author works in; the narrative voice and the point of view they '
    'favour; the texture of the sentences (long or short, plain or '
    'ornate, paratactic or heavily subordinated); the level of the '
    'vocabulary; how dialogue is written and how much of a book is '
    'dialogue; the use of humour, irony, dialect, slang or period '
    'language; recurring stylistic habits worth preserving; and '
    'anything translators of this author are known to get wrong.\n\n'
    'Write 150 to 300 words of plain prose in <tlang>. Do not review or '
    'praise, do not give advice that would apply to any novel, and '
    'invent nothing. Only if you know nothing reliable about how this '
    'author writes, reply with exactly: %s\n\n'
    'Reply with the brief itself and nothing else: no preamble, no '
    'headings, no lists, no citations, no closing remarks.\n\n'
    'Author: {author}\n'
    'Known works of this author, for orientation: {works}\n'
    'Book: {title}\n'
    'Original language: <slang>\n'
    'Translation language: <tlang>') % NO_AUTHOR_INFORMATION


# ---------------------------------------------------------------------------
# Dialogue punctuation
# ---------------------------------------------------------------------------
#
# A chapter is translated by a handful of independent requests, and
# nothing in them says how direct speech is punctuated. Left to itself a
# model picks whatever its target language usually does -- and picks it
# again, differently, three chapters later: the same book came back with
# guillemets in one chapter and straight quotes in the next.
#
# The source already answers the question, so it is answered once for the
# whole book by counting marks over every source paragraph, and the
# answer is stated in the system prompt of every single request. It is
# deliberately not persisted: the same book yields the same counts, so
# recomputing on a resumed run gives the same rule.

# Opening mark -> how to describe the pair to the model. Only the opening
# mark is counted: a closing one that is also an apostrophe would count
# every contraction in the book.
DIALOGUE_MARKS = (
    ('«', '«…»', 'guillemets'),
    ('“', '“…”', 'curly double quotation marks'),
    ('„', '„…“', 'low-high double quotation marks'),
    ('"', '"…"', 'straight double quotation marks'),
    ('‘', '‘…’', 'curly single quotation marks'),
    ('‹', '‹…›', 'single guillemets'),
    ('「', '「…」', 'corner brackets'),
    ('『', '『…』', 'white corner brackets'),
)

# Dashes that open a line of dialogue when they open a paragraph. Only
# that position is counted: the same characters in the middle of a
# sentence are parenthetical, not speech.
DIALOGUE_DASHES = ('—', '–', '―', '-')

# The conventions a user can prescribe instead of having the source
# read. Each is what detect_dialogue_style would report for a book that
# follows it: the primary pair, the pair used for a quotation inside a
# line of speech, and whether lines of speech open with a dash. The
# pairs are given outright rather than looked up in DIALOGUE_MARKS,
# which only knows the marks worth counting: a reversed guillemet opens
# speech in German and closes it in French, so it is never counted.
DIALOGUE_CONVENTIONS = {
    'curly_double': {
        'label': '“…”  curly double quotation marks (‘…’ inside)',
        'primary': ('“…”', 'curly double quotation marks'),
        'nested': ('‘…’', 'curly single quotation marks'),
        'dash': False},
    'curly_single': {
        'label': '‘…’  curly single quotation marks (“…” inside)',
        'primary': ('‘…’', 'curly single quotation marks'),
        'nested': ('“…”', 'curly double quotation marks'),
        'dash': False},
    'straight_double': {
        'label': '"…"  straight double quotation marks (\'…\' inside)',
        'primary': ('"…"', 'straight double quotation marks'),
        'nested': ("'…'", 'straight single quotation marks'),
        'dash': False},
    'guillemets': {
        'label': '«…»  guillemets (“…” inside)',
        'primary': ('«…»', 'guillemets'),
        'nested': ('“…”', 'curly double quotation marks'),
        'dash': False},
    'reversed_guillemets': {
        'label': '»…«  reversed guillemets (›…‹ inside)',
        'primary': ('»…«', 'reversed guillemets'),
        'nested': ('›…‹', 'reversed single guillemets'),
        'dash': False},
    'low_high': {
        'label': '„…“  low-high quotation marks (‚…‘ inside)',
        'primary': ('„…“', 'low-high double quotation marks'),
        'nested': ('‚…‘', 'low-high single quotation marks'),
        'dash': False},
    'dash': {
        'label': '—  dash at the start of each line of speech',
        'primary': None,
        'nested': ('“…”', 'curly double quotation marks'),
        'dash': True},
    'corner': {
        'label': '「…」  corner brackets (『…』 inside)',
        'primary': ('「…」', 'corner brackets'),
        'nested': ('『…』', 'white corner brackets'),
        'dash': False},
}

# Below this many occurrences a mark is noise -- a stray quotation in an
# epigraph, a measurement in inches -- rather than the book's convention.
_DIALOGUE_MIN_COUNT = 6
# A second mark is reported as the nested one only when it is common
# enough to be a convention of its own rather than an accident.
_DIALOGUE_NESTED_RATIO = 0.05
# Share of paragraphs that must open with a dash before dash dialogue is
# reported. Real dash dialogue runs through whole conversations.
_DIALOGUE_DASH_RATIO = 0.03


def detect_book_dialogue_style(chapters):
    """Return how the book punctuates direct speech, chapter by chapter.

    :chapters: an iterable of chapters, each an iterable of source
        paragraph strings.

    Every chapter that shows a convention votes for it, and the book
    follows the convention most chapters use; the raw counts then come
    from those chapters alone. A raw count over the whole book let a
    preface or an afterword decide -- an essay quoting at length in
    straight marks outweighed a novel that opens speech with guillemets
    a few times a page -- and a vote is what a preface cannot win.
    The result is that of :func:`detect_dialogue_style`, plus ``votes``
    (chapters that chose the winner) and ``voters`` (chapters that
    showed any convention), both for the log.
    """
    chapters = [list(texts) for texts in chapters]
    votes = {}
    styles = []
    for texts in chapters:
        style = detect_dialogue_style(texts)
        styles.append(style)
        choice = style['primary'] or ('dash' if style['dash'] else None)
        if choice:
            votes[choice] = votes.get(choice, 0) + 1
    if not votes:
        style = detect_dialogue_style(
            text for texts in chapters for text in texts)
        style.update(votes=0, voters=0)
        return style
    winner = max(votes, key=lambda choice: (
        votes[choice],
        sum(s['counts'].get(choice, 0) for s in styles)))
    chosen = [
        texts for texts, style in zip(chapters, styles)
        if (style['primary'] or ('dash' if style['dash'] else None))
        == winner]
    style = detect_dialogue_style(
        text for texts in chosen for text in texts)
    style.update(votes=votes[winner], voters=sum(votes.values()))
    return style


def detect_dialogue_style(texts):
    """Return how the source punctuates direct speech.

    :texts: an iterable of source paragraph strings.

    The result is a dict with ``primary`` and ``nested`` (the opening
    mark of the most and second-most frequent pair, or None), ``dash``
    (whether paragraphs open a line of speech with a dash) and ``counts``
    (the raw tally, for the log). Everything is None/False/empty for a
    text that punctuates nothing, which is a normal answer: a book with
    no dialogue at all needs no rule.
    """
    counts = {}
    dash_paragraphs = 0
    paragraphs = 0
    for text in texts:
        if not text:
            continue
        paragraphs += 1
        for mark, _pair, _name in DIALOGUE_MARKS:
            found = text.count(mark)
            if found:
                counts[mark] = counts.get(mark, 0) + found
        head = text.lstrip()
        if len(head) > 1 and head[0] in DIALOGUE_DASHES \
                and head[1] not in DIALOGUE_DASHES:
            dash_paragraphs += 1
    ranked = [mark for mark, total in sorted(
        counts.items(), key=lambda item: (-item[1], item[0]))
        if total >= _DIALOGUE_MIN_COUNT]
    primary = ranked[0] if ranked else None
    nested = None
    if primary and len(ranked) > 1 \
            and counts[ranked[1]] >= counts[primary] * _DIALOGUE_NESTED_RATIO:
        nested = ranked[1]
    dash = bool(paragraphs) and \
        dash_paragraphs >= max(3, paragraphs * _DIALOGUE_DASH_RATIO)
    return {
        'primary': primary,
        'nested': nested,
        'dash': dash,
        'counts': counts,
        'dash_paragraphs': dash_paragraphs,
        'paragraphs': paragraphs,
    }


# Two placeholders of the shipped prompt are empty on most books --
# there is no author brief, or the source punctuates nothing -- and a
# literal substitution would leave their blank lines behind.
_BLANK_LINES_RE = re.compile(r'\n{3,}')


def collapse_blank_lines(text):
    return _BLANK_LINES_RE.sub('\n\n', text or '').strip()


# ---------------------------------------------------------------------------
# What went wrong with a request
# ---------------------------------------------------------------------------


def _http_error(error):
    """The HTTPRequestError behind ``error``, or None."""
    for candidate in (error, getattr(error, 'cause', None)):
        if isinstance(candidate, HTTPRequestError):
            return candidate
    return None


def _error_payload(http):
    """The ``error`` object of an HTTP error body, or {}."""
    try:
        payload = json.loads(http.body)
    except (TypeError, ValueError):
        return {}
    error = payload.get('error') if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}


def describe_error(error):
    """One line saying what went wrong, for the log and the window.

    The exception an engine raises carries a traceback and the whole
    response body; on a rate limit that was a hundred lines to say
    "try again later". The provider's own message is what a reader
    wants: the status and, for OpenRouter, the upstream reason too.
    """
    http = _http_error(error)
    if http is not None:
        payload = _error_payload(http)
        parts = ['HTTP %d' % http.status]
        message = payload.get('message') or http.reason
        if message:
            parts.append(str(message))
        metadata = payload.get('metadata')
        raw = metadata.get('raw') if isinstance(metadata, dict) else None
        if raw:
            parts.append(str(raw))
        return ': '.join(parts)
    text = str(error)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # The last line that is not part of a traceback is the message.
    for line in reversed(lines):
        if line.startswith(('File ', 'Traceback ', '~', '^')):
            continue
        return line[:300]
    return text[:300]


def retry_after(error):
    """How long the provider asked to wait before asking again, in
    seconds, or None."""
    http = _http_error(error)
    if http is None:
        return None
    if http.retry_after:
        return float(http.retry_after)
    metadata = _error_payload(http).get('metadata')
    if isinstance(metadata, dict):
        try:
            value = float(metadata.get('retry_after_seconds') or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return None


def is_rate_limited(error):
    """Whether the request was refused for the moment, not for good."""
    http = _http_error(error)
    if http is not None:
        if http.status == 429:
            return True
        payload = _error_payload(http)
        metadata = payload.get('metadata')
        code = metadata.get('provider_error_code') \
            if isinstance(metadata, dict) else None
        if code in ('capacity', 'rate_limit', 'rate_limited'):
            return True
    text = str(error).lower()
    return 'rate-limit' in text or 'rate limit' in text \
        or 'too many requests' in text


def is_routing_dead_end(error):
    """Whether OpenRouter found no provider taking every parameter."""
    http = _http_error(error)
    if http is None:
        return False
    message = str(_error_payload(http).get('message') or '')
    return http.status == 404 and 'requested parameters' in message


def _dialogue_pair(mark):
    """The pair and the name of a mark: an opening mark as counted by
    detection, or a ``(pair, name)`` tuple as a convention states it."""
    if isinstance(mark, tuple):
        return mark
    for opening, pair, name in DIALOGUE_MARKS:
        if opening == mark:
            return pair, name
    return mark, mark


def dialogue_convention_style(key):
    """The style dict of a convention in ``DIALOGUE_CONVENTIONS``, in
    the shape :func:`detect_dialogue_style` returns, or None."""
    convention = DIALOGUE_CONVENTIONS.get(key)
    if convention is None:
        return None
    return {
        'primary': convention['primary'],
        'nested': convention['nested'],
        'dash': convention['dash'],
        'counts': {},
    }


def dialogue_instruction(style):
    """Turn :func:`detect_dialogue_style` output -- or a convention's
    style, see :func:`dialogue_convention_style` -- into prompt text.

    Returns '' when there is no convention worth stating, so the
    placeholder simply disappears from the system prompt.
    """
    if not style:
        return ''
    primary = style.get('primary')
    if not (primary or style.get('dash')):
        return ''
    parts = [model_text('Dialogue punctuation.')]
    if primary:
        pair, name = _dialogue_pair(primary)
        parts.append(model_text(
            'The source marks direct speech with {name} ({pair}). Use '
            'exactly those characters in the translation, everywhere, '
            'even where the target language would normally prefer '
            'others.').format(name=name, pair=pair))
    nested = style.get('nested')
    if nested:
        pair, name = _dialogue_pair(nested)
        parts.append(model_text(
            'A quotation inside a line of speech is marked with {name} '
            '({pair}); keep that distinction.').format(
                name=name, pair=pair))
    if style.get('dash'):
        parts.append(model_text(
            'Lines of dialogue that open with a dash in the source open '
            'with the same dash in the translation.'))
    parts.append(model_text(
        'This is the convention of the whole book. Follow it in every '
        'chapter, whatever a particular passage in front of you happens '
        'to use, and never switch to another set of marks partway '
        'through.'))
    return ' '.join(parts)



# ---------------------------------------------------------------------------
# Paragraph tagging / alignment
# ---------------------------------------------------------------------------


# Paragraph markers
# -----------------
#
# We use bracketed numeric markers ``[N]`` on their own line before each
# paragraph rather than XML-style ``<Pn>...</Pn>`` tags. Empirically, local
# LLMs (Gemma, Mistral, LLaMA-based) follow this format much more reliably:
# XML tags require nested balancing which the models tend to skip when the
# context is long, while numeric markers are the natural way these models
# already structure numbered lists in their training data.
#
# Format sent to the LLM:
#     [1]
#     First paragraph text.
#
#     [2]
#     Second paragraph text.
#
# Expected response:
#     [1]
#     Primo paragrafo tradotto.
#
#     [2]
#     Secondo paragrafo tradotto.

# Matches a marker line ``[N]`` capturing everything up to the next marker
# or end of string.  MULTILINE + DOTALL so ``.`` spans newlines inside a
# paragraph, and ``^\s*\[N\]`` anchors on a line by itself.
# The marker is asked for on a line of its own, and most replies put it
# there; some models write "[1] Text" on one line, or "**[1]**", and a
# reply of theirs used to parse as forty-two missing paragraphs.
_MARKER_RE = re.compile(
    r'^[ \t]*(?:\*\*)?\[(\d+)\](?:\*\*)?[ \t]*:?[ \t]*\n?'
    r'(.*?)'
    r'(?=\n[ \t]*(?:\*\*)?\[\d+\](?:\*\*)?[ \t]*:?[ \t]*(?:\n|\S)|\Z)',
    re.MULTILINE | re.DOTALL)


def _renumbered(found, expected_indices):
    """Map a reply that numbered its paragraphs from 1 back onto the
    numbers it was asked for.

    A retry asks for the paragraphs that are still missing under their
    original numbers -- 34 to 75, say -- and some models number what
    they are given from 1 regardless. When nothing carries an expected
    number and the reply's numbers are exactly 1..k for the k paragraphs
    asked, the order is the mapping.

    A reply that carries fewer than k is not mapped. Which of the
    paragraphs asked for those few are is anybody's guess, and a wrong
    guess files a translation under another paragraph, where nothing
    shows it is wrong. They are asked for again instead.
    """
    expected = list(expected_indices)
    if not found or not expected or expected[0] == 1:
        return None
    if any(n in expected for n in found):
        return None
    if set(found) != set(range(1, len(expected) + 1)):
        return None
    return {expected[n - 1]: value for n, value in found.items()}


# What a model writes in place of the text it did not care to translate:
# "[…]", "[...]", "(…)", "[. . .]".
_ABBREVIATION_RE = re.compile(r'[\[(]\s*(?:…|\.(?:\s*\.){2,})\s*[\])]')
# The inline placeholders the source carries for images and line breaks
# (see ``Base.placeholder``), with the spaces and single braces a model
# may put around them.
_PLACEHOLDER_RE = re.compile(r'\{\{?\s*id\s*_\s*(\d+)\s*\}\}?')
_DIALOGUE_OPENERS = frozenset(
    [opening for opening, _pair, _name in DIALOGUE_MARKS]
    + ['»', '›', '‚', "'"])
# Below this many characters of source the length of a translation says
# nothing: a title, a name, a "Yes." come back any size.
_RATIO_MIN_CHARS = 40
# A translation this short is duplicated legitimately -- two "‘No.’"
# lines in a row -- and its duplicate is a doubt, not a certainty.
_DUPLICATE_MIN_CHARS = 30

VERIFICATION_REASONS = {
    'abbreviated': _('shortened with an ellipsis'),
    'placeholders': _('placeholders unlike the source'),
    'duplicate': _('same text as another paragraph'),
    'dialogue': _('opens as dialogue where the source does not, or '
                  'the reverse'),
    'length': _('length out of proportion with the source'),
}


# Titles of the pages around the story, as a table of contents usually
# names them. A chapter whose title carries one of them is translated
# but not summarised, and its names do not go into the glossary: there
# is no story in it to keep track of, and the call that says so costs
# as much as a real summary.
FRONT_MATTER_TITLES = (
    'copyright, also by, other books by, by the same author, books by, '
    'praise for, about the author, dedication, acknowledgements, '
    'acknowledgments, contents, title page, half title, colophon')
# Titles of the pages that stay in the language they are written in: a
# list of the author's other books is a list of titles the reader will
# look for as they were published.
UNTRANSLATED_TITLES = 'also by, other books by, by the same author, books by'


def title_matches(title, patterns):
    """Whether ``title`` carries one of the comma-separated ``patterns``
    as whole words, case-insensitively: "Also by Paul Doherty" matches
    "also by", "Discontents" does not match "contents"."""
    haystack = ' '.join((title or '').split()).casefold()
    if not haystack:
        return False
    for pattern in str(patterns or '').split(','):
        pattern = ' '.join(pattern.split()).casefold()
        if pattern and re.search(
                r'(?<!\w)%s(?!\w)' % re.escape(pattern), haystack):
            return True
    return False


def _opens_dialogue(text):
    """Whether ``text`` starts the way a line of speech does: with an
    opening quotation mark, or with a dash and then the words."""
    head = (text or '').lstrip()
    if not head:
        return False
    if head[0] in _DIALOGUE_OPENERS:
        return True
    return head[0] in DIALOGUE_DASHES and len(head) > 1 and (
        head[1].isspace() or head[1].isalpha()
        or head[1] in _DIALOGUE_OPENERS)


def _normalized(text):
    return ' '.join((text or '').split()).casefold()


def _length_reference(sources, translations):
    """The proportion between translation and source this reply keeps,
    measured on its long paragraphs: the median of their ratios, or 1.0
    with a wider tolerance when there are too few to measure. It is a
    property of the pair of languages, and a chunk of the book is the
    right place to read it."""
    ratios = []
    for number, text in translations.items():
        source = (sources.get(number, '') or '').strip()
        if len(source) >= _RATIO_MIN_CHARS:
            ratios.append(len(text.strip()) / len(source))
    if len(ratios) >= 3:
        return sorted(ratios)[len(ratios) // 2], 2.5
    return 1.0, 3.0


def _paragraph_flags(source, text, reference=1.0, spread=3.0):
    """The signs, on one paragraph, that ``text`` is not its translation:
    ``[(reason, hard), ...]``, empty when nothing is wrong. The reply-wide
    signs -- the same text under two numbers -- are not here."""
    flags = []
    if _ABBREVIATION_RE.search(text) and not _ABBREVIATION_RE.search(source):
        flags.append(('abbreviated', True))
    if sorted(_PLACEHOLDER_RE.findall(text)) \
            != sorted(_PLACEHOLDER_RE.findall(source)):
        flags.append(('placeholders', True))
    if _opens_dialogue(source) != _opens_dialogue(text):
        flags.append(('dialogue', False))
    stripped = source.strip()
    if len(stripped) >= _RATIO_MIN_CHARS:
        ratio = len(text.strip()) / len(stripped)
        if ratio > reference * spread or ratio < reference / spread:
            flags.append(('length', False))
    return flags


def realign_shifted(sources, translations, max_shift=3, confirm=3):
    """Read a reply whose numbers slipped.

    A model that skips a paragraph and numbers on from there labels every
    later translation one short: the text under 25 is the translation of
    26, and so on until the next skip. Returns ``(realigned, moves)``:
    the translations under the numbers of the paragraphs they actually
    translate, and one ``(from, to)`` pair for each that moved. What
    fits no paragraph at any offset is left out, to be asked for again.

    The reading is deliberately hard to trigger. A translation is moved
    only when it fails the checks where it stands, passes them ``d``
    places further on, the next ``confirm`` translations pass them at
    the same offset too, and at least one of those also fails where it
    stands: one odd paragraph does not move anything, a run of them
    that all make sense one place further on does. The offset only ever
    grows, because a skip is what a model does; a paragraph translated
    twice is not.
    """
    numbers = sorted(translations)
    reference, spread = _length_reference(sources, translations)

    def fits(number, text):
        source = sources.get(number)
        return source is not None and not _paragraph_flags(
            source, text, reference, spread)

    realigned = {}
    moves = []
    offset = 0
    i = 0
    while i < len(numbers):
        number = numbers[i]
        text = translations[number]
        if fits(number + offset, text):
            realigned[number + offset] = text
            if offset:
                moves.append((number, number + offset))
            i += 1
            continue
        window = numbers[i:i + confirm + 1]
        shifted = None
        if len(window) >= 2:
            for delta in range(offset + 1, offset + max_shift + 1):
                if all(fits(m + delta, translations[m]) for m in window) \
                        and any(not fits(m + offset, translations[m])
                                for m in window[1:]):
                    shifted = delta
                    break
        if shifted is None:
            i += 1
            continue
        offset = shifted
    return realigned, moves


def suspicious_translations(sources, translations, accepted=None):
    """Find the translations of a reply that cannot be of the paragraph
    they are filed under.

    ``sources`` maps every paragraph number of the chunk to its text,
    ``translations`` the numbers of one reply to what came back for
    them, and ``accepted`` the translations already taken from earlier
    replies of the same chunk. Returns ``{number: (reason, hard)}``,
    ``reason`` being a key of ``VERIFICATION_REASONS``.

    The number is the only thing that pairs a translation with its
    paragraph, and a model that skips one paragraph and numbers on from
    there files every translation after it under the wrong number, with
    nothing in the reply to show for it. A retry that receives fewer
    paragraphs than it asked for does the same when the model numbers
    them from 1. Having the model echo the source would show it, at
    twice the output cost; these checks cost nothing and catch the
    shapes that kind of slip takes:

      * the translation stands in for part of its text with an ellipsis
        in brackets (hard);
      * the inline placeholders of the source are not those of the
        translation (hard);
      * two paragraphs with different sources got the same translation
        (hard when the text is long enough to make a coincidence
        unlikely, soft otherwise);
      * one opens as a line of speech and the other does not (soft);
      * the translation is out of proportion with its source, against
        the proportion the rest of the reply keeps (soft).

    A hard sign is a wrong translation whatever the paragraph. A soft
    one is what a shift leaves behind but also what an unusual paragraph
    can look like, so the caller gives it a second chance.
    """
    flagged = {}

    def flag(number, reason, hard):
        current = flagged.get(number)
        if current is None or (hard and not current[1]):
            flagged[number] = (reason, hard)

    by_text = {}
    for number, text in list((accepted or {}).items()) \
            + list(translations.items()):
        by_text.setdefault(_normalized(text), set()).add(number)
    # Length is judged against the proportion this reply keeps between
    # source and translation rather than a figure of our own.
    reference, spread = _length_reference(sources, translations)
    for number, text in translations.items():
        source = sources.get(number, '') or ''
        others = by_text.get(_normalized(text), set()) - {number}
        if any(_normalized(sources.get(other, '')) != _normalized(source)
               for other in others):
            flag(number, 'duplicate',
                 len(text.strip()) >= _DUPLICATE_MIN_CHARS)
        for reason, hard in _paragraph_flags(source, text, reference, spread):
            flag(number, reason, hard)
    return flagged


def _money(value):
    """A cost in dollars for the log: whole cents and below, without
    the noise of floating point."""
    try:
        value = float(value or 0.0)
    except (TypeError, ValueError):
        return '0'
    text = '%.6f' % value
    text = text.rstrip('0').rstrip('.')
    return text or '0'


def _ranges(numbers):
    """``[1, 2, 3, 7, 9, 10]`` as ``"1-3, 7, 9-10"``, for the log."""
    parts = []
    start = previous = None
    for number in sorted(numbers):
        if start is None:
            start = previous = number
        elif number == previous + 1:
            previous = number
        else:
            parts.append(
                str(start) if start == previous else '%d-%d' % (
                    start, previous))
            start = previous = number
    if start is not None:
        parts.append(
            str(start) if start == previous else '%d-%d' % (start, previous))
    return ', '.join(parts)


def tag_paragraphs(paragraphs):
    """Return the ``[N]\\ntext`` block that will be sent to the LLM plus
    the list of indices used (in order).

    Ignored paragraphs are skipped (they carry no translatable content).
    """
    parts = []
    indices = []
    for i, p in enumerate(paragraphs, start=1):
        if getattr(p, 'ignored', False):
            continue
        text = (p.original or '').strip()
        parts.append('[%d]\n%s' % (i, text))
        indices.append(i)
    # Blank line between paragraphs helps the LLM keep them separated and
    # makes the parser boundary regex simpler / more robust.
    return '\n\n'.join(parts), indices


def parse_tagged_response(response, expected_indices):
    """Parse an LLM response containing ``[N]`` numeric markers.

    Returns a dict ``{index: translation}``. Missing indices are absent
    from the dict; callers can then decide how to handle the mismatch.

    The parser is tolerant of the common LLM habits:
      * leading/trailing prose (e.g. "Here is the translation:")
      * extra whitespace between markers
      * duplicate markers (last one wins)
      * hallucinated marker numbers (filtered by ``expected_indices``)
    """
    if not response:
        return {}
    found = {}
    for m in _MARKER_RE.finditer(response):
        try:
            idx = int(m.group(1))
        except (ValueError, TypeError):
            continue
        body = m.group(2)
        # Trim trailing prose after the last marker: if the body ends
        # with a blank line followed by additional text, that text is
        # commentary (e.g. "End of translation.") and must be dropped.
        # A blank line is ``\n\n`` with optional whitespace.
        blank_break = re.search(r'\n[ \t]*\n', body)
        if blank_break:
            # Everything after the blank line is discarded UNLESS it
            # itself contains a marker — but that case is already
            # handled by the outer finditer, so this cut is safe.
            body = body[:blank_break.start()]
        body = body.strip()
        if not body:
            # A marker with nothing under it is a paragraph the model
            # skipped, not a translation. Left out, it is asked for again
            # by the alignment retry; stored, it blanked the paragraph in
            # the output. The structured path treats an empty
            # "translation" the same way.
            continue
        # Latest occurrence wins if duplicated.
        found[idx] = body
    # Filter to only expected indices to avoid pollution from
    # hallucinated tags.
    result = {i: found[i] for i in expected_indices if i in found}
    return result or _renumbered(found, expected_indices) or {}


# ---------------------------------------------------------------------------
# Novel translator (sequential orchestrator)
# ---------------------------------------------------------------------------


def _extract_json_object(text):
    """Best-effort extraction of the first top-level JSON object in ``text``.

    LLMs (especially small ones) sometimes wrap JSON output in prose or a
    Markdown code fence. We scan for the first ``{`` and take the balanced
    substring up to its matching ``}``.
    """
    if not text:
        return None
    # Strip common markdown fences first (```json ... ```).
    fence_stripped = re.sub(
        r'^```(?:json)?\s*\n?|\n?```\s*$', '', text.strip(), flags=re.M)
    for candidate_text in (fence_stripped, text):
        start = candidate_text.find('{')
        if start < 0:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(candidate_text)):
            ch = candidate_text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = candidate_text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except ValueError:
                        break  # try next candidate_text
    return None


def _extract_json_string(text, key):
    """Return the value of the string field ``key`` in a broken reply.

    Used when the answer stopped before its closing braces: the object
    cannot be decoded as a whole, but a field that was written in full
    still decodes on its own.
    """
    if not text:
        return ''
    match = re.search(r'"%s"\s*:\s*"' % re.escape(key), text)
    if not match:
        return ''
    try:
        value, _end = json.JSONDecoder().raw_decode(text, match.end() - 1)
    except ValueError:
        return ''
    return value.strip() if isinstance(value, str) else ''


def _iter_json_objects(text):
    """Yield every complete JSON object found anywhere in ``text``.

    Salvage path for a reply the model did not finish: when the answer is
    cut off mid-object the outermost braces never balance, so
    :func:`_extract_json_object` returns nothing and a whole chunk is
    thrown away even though most of its paragraphs arrived intact. Here we
    walk the text and let the decoder consume whatever parses from each
    ``{``, so the paragraphs that did complete are kept and only the tail
    has to be asked for again.
    """
    if not text:
        return
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        index = text.find('{', index)
        if index < 0:
            return
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(obj, dict):
            # A wrapper that did close (``{"paragraphs": [...]}`` under
            # an unexpected key, say) is yielded together with the entries
            # it holds, so the caller finds them either way.
            yield obj
            for value in obj.values():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            yield item
        index = max(end, index + 1)


# Fallback pattern for line-based entity extraction when JSON parsing fails.
# The separator between source and translation is restricted to arrow-like
# symbols (``->``, ``=>``, ``→``) which are unambiguous entity mappings;
# colon and pipe alone would match prose lines like "Here are the
# entities: ...".
_ENTITY_LINE_PATTERNS = [
    re.compile(
        # source: run of non-quote, non-arrow-marker chars.
        r'["\']?(?P<source>[^"\'\n\->=→|(]{1,80}?)["\']?\s*'
        # separator: any arrow form.
        r'(?:->|→|=>)\s*'
        # translation: run stopping before optional (type) or notes.
        r'["\']?(?P<translation>[^"\'\n(]{1,120}?)["\']?\s*'
        # optional (type)
        r'(?:\((?P<type>[^)\n]+)\))?\s*'
        # optional notes prefixed by : or - or |
        r'(?:[:\-\|]\s*(?P<notes>[^\n]{1,200}))?',
        re.MULTILINE),
]


def _extract_entities_fallback(text):
    """Extract entities using line-based regex when JSON parsing fails.

    Returns a list of dicts compatible with ``_extract_glossary_updates``.
    Only returns entries that look plausible (both source and translation
    non-empty, source shorter than 80 chars).
    """
    if not text:
        return []
    # Try to find lines that look like key/value entity mappings. We are
    # deliberately conservative: prefer no entries over noisy ones.
    entities = []
    seen_sources = set()
    for pattern in _ENTITY_LINE_PATTERNS:
        for m in pattern.finditer(text):
            source = (m.group('source') or '').strip(' "\',.;')
            translation = (m.group('translation') or '').strip(' "\',.;')
            # Filter obvious noise: skip lines that don't look like entities.
            if not source or not translation:
                continue
            if source == translation and len(source) < 3:
                continue
            if len(source) > 80 or len(translation) > 120:
                continue
            # Skip lines that are clearly prose (contain verbs / long text).
            if source.count(' ') > 5:
                continue
            if source.lower() in seen_sources:
                continue
            seen_sources.add(source.lower())
            entities.append({
                'source': source,
                'translation': translation,
                'type': (m.group('type') or '').strip() or 'other',
                'notes': (m.group('notes') or '').strip(' "\',.;'),
            })
    return entities


class NovelTranslator:
    """Sequential chapter-by-chapter translator with running context.

    Design constraints (contrasted with ``lib.translation.Translation``):

      * strictly sequential: chapter N+1 depends on the summary of chapter N.
      * per-chunk retry with alignment-aware re-request.
      * writes each translated paragraph into the cache as soon as it is
        available, so an interruption leaves a partial-but-consistent state.
      * bumps ``ContextManager.progress`` only when a full chapter is done.

    The translator engine is expected to expose the same interface as
    ``engines.base.Base.translate(text)`` and, if it is a ``GenAI`` subclass,
    the helper ``override_prompt(prompt)`` -- but we fall back gracefully
    if the helper is not available by assigning to ``translator.prompt``
    directly.
    """

    def __init__(self, translator, chapters, context_manager, cache,
                 config=None, aux_paragraphs=None):
        """
        :aux_paragraphs: the paragraphs that belong to no chapter -- the
            metadata, the table of contents, the pages the front-matter
            filter set aside (see ``ChapterBuilder.auxiliary_paragraphs``).
            Translated before the chapters, apart from the narrative.
        """
        self.translator = translator
        self.chapters = list(chapters)
        self.aux_paragraphs = list(aux_paragraphs or [])
        self.ctx = context_manager
        self.cache = cache
        self.config = dict(config or {})

        # Callbacks (all optional; safe defaults).
        self.progress = dummy       # (fraction: float, message: str)
        self.log = dummy            # (message: str, is_error: bool=False)
        self.chapter_started = dummy   # (chapter: Chapter)
        self.chapter_done = dummy      # (chapter: Chapter, summary: str,
                                       #  glossary_delta: list[dict])
        self.cancel_request = lambda: False

        # Runtime state.
        self.total_chapters = 0
        self.completed_chapters = 0
        # One-shot log flags: set to True after the first call that logs
        # the chosen output format, and the first one that lowers the
        # chunk budget. Reset per instance.
        self._structured_choice_logged = False
        self._chunk_budget_logged = False
        self._reply_room_logged = False
        # Computed once per book, lazily: the dialogue rule read off the
        # source, and the author brief (researched or read back from the
        # context). None means "not worked out yet", '' means "worked
        # out, and there is nothing to say".
        self._dialogue_rules = None
        self._author_style = None
        # Whether the chapter being worked on belongs to the story, as
        # the model reported when asked for its summary.
        self._chapter_is_narrative = True
        # What the request being sent is for -- 'translation', 'retry',
        # 'context', 'author' -- so the totals can tell them apart.
        self._request_kind = 'other'
        # Requests, tokens and cost per kind of request; replies per
        # provider and how many of them were unreliable; how many
        # translations were moved back under their paragraph. Loaded
        # from the cache when the run starts, so they cover the book
        # and not the run, and written back after every chapter.
        self.usage = {}
        self.provider_stats = {}
        self.realigned = 0
        # Source characters sent for translation and, of those, the
        # ones whose translation had to be set aside, moved or asked
        # for again: the share of the text that gave trouble.
        self.text_stats = {'chars': 0, 'bad_chars': 0}
        # Wall-clock seconds of the runs on this book, this one included.
        self.run_seconds = 0.0
        self.report = dummy         # (html: str)
        # When the chunks of a chapter are in flight at once, each runs
        # on a shallow copy of this object with its own engine (see
        # :meth:`_clone`); the copies share the totals above through
        # ``_root`` and take this lock to change them.
        self._root = self
        self._lock = threading.RLock()

    # -- setters (mirroring lib.translation.Translation) -------------------

    def set_report(self, cb):
        """Called with the report text whenever it is rewritten: after
        every chapter and at the end of the run."""
        self.report = cb or dummy

    def set_progress(self, cb):
        self.progress = cb or dummy

    def set_logging(self, cb):
        self.log = cb or dummy

    def set_chapter_started(self, cb):
        self.chapter_started = cb or dummy

    def set_chapter_done(self, cb):
        self.chapter_done = cb or dummy

    def set_cancel_request(self, cb):
        self.cancel_request = cb or (lambda: False)

    # -- configuration accessors ------------------------------------------

    def _cfg(self, key, default):
        return self.config.get(key, default) if self.config else default

    @property
    def chunk_tokens(self):
        return int(self._cfg('novel_chunk_tokens', 16000))

    @property
    def max_paragraphs_per_chunk(self):
        """Maximum number of translatable paragraphs per chunk.

        The chunk is closed as soon as either the token budget or this
        paragraph cap is reached -- whichever comes first. This cap is
        what keeps the *reply* within reach: a chunk is budgeted on the
        tokens it costs to read, but the translation costs about as much
        again to write, and output limits (8k-32k tokens) are an order of
        magnitude below the context windows the token budget targets.
        It also prevents the LLM from losing track of the ``[N]``
        alignment markers when paragraphs are short (dialogue, TOC
        lists, one-line stanzas).

        Set to 0 to disable the cap and use only the token budget.
        """
        return int(self._cfg('novel_max_paragraphs_per_chunk', 50))

    @property
    def overlap_paragraphs(self):
        """Number of already-translated paragraphs from the previous chunk
        to replay as narrative context for the next chunk.

        Analogous to the sliding-window overlap used in RAG chunking, but
        here the overlap serves a different purpose: it preserves
        dialogue threads, pronoun referents and stylistic continuity
        across chunk boundaries. The overlap is:

          * shown to the LLM as *already-translated* text so it needs no
            further translation (avoids re-cost + preserves consistency);
          * not re-aligned into the DOM (the ``paragraph.translation``
            for those items was already written by the previous chunk);
          * subtracted from the per-chunk token budget so the total
            request size stays within the model window.

        Set to 0 to disable overlap entirely.
        """
        return max(0, int(self._cfg('novel_overlap_paragraphs', 5)))

    @property
    def structured_output_setting(self):
        """User preference for the structured (JSON) output path.

        Values:
          ``'auto'``  -- use structured output when the current engine
                         advertises support via ``structured_output_mode``.
                         Otherwise fall back to text markers ``[N]``.
                         This is the default and the safest choice.
          ``'off'``   -- always use text markers, even if the engine
                         supports structured output. Useful for debugging
                         or when a specific model produces low-quality
                         translations in JSON mode.
          ``'force'`` -- use structured output regardless of what the
                         engine advertises. Useful for OpenAI-compatible
                         custom endpoints (Ollama exotics, LM Studio,
                         vLLM, ...) that accept ``response_format`` but
                         do not declare the capability in the plugin.
        """
        value = self._cfg('novel_structured_output', 'auto')
        if value not in ('auto', 'off', 'force'):
            value = 'auto'
        return value

    def _engine_supports_structured(self):
        """Return True if the current translator declares native support
        for structured output (JSON mode or JSON schema)."""
        return getattr(
            self.translator, 'structured_output_mode', None) in (
                'json', 'schema')

    def _structured_active(self):
        """Combine the user setting and the engine capability to decide
        whether the current chunk should be translated via the structured
        path or the marker path.

        Emits a one-shot log line the first time it is called so the
        user sees which path is active without needing to inspect chunk
        payloads. The choice is stable per translation session (the
        engine and the config do not change mid-run).
        """
        setting = self.structured_output_setting
        supports = self._engine_supports_structured()

        if setting == 'off':
            active = False
        elif setting == 'force':
            active = True
        else:  # 'auto'
            active = supports

        # Verbose one-shot log: informs the user which output format the
        # LLM will be asked to produce.
        if not getattr(self, '_structured_choice_logged', False):
            self._structured_choice_logged = True
            engine_name = getattr(
                self.translator, 'name', self.translator.__class__.__name__)
            capability_desc = getattr(
                self.translator, 'structured_output_mode', None) or 'none'
            if active:
                self.log(_(
                    'Output format: structured JSON '
                    '(engine={eng}, capability={cap}, setting={s}). '
                    'Chunks can safely hold more paragraphs than with '
                    'text markers.').format(
                        eng=engine_name, cap=capability_desc, s=setting))
            else:
                if setting == 'off':
                    reason = _('disabled by user setting')
                elif not supports:
                    reason = _('engine does not advertise support')
                else:
                    reason = _('unknown')
                self.log(_(
                    'Output format: text markers [N] '
                    '(engine={eng}, capability={cap}, setting={s}, '
                    'reason={reason}).').format(
                        eng=engine_name, cap=capability_desc,
                        s=setting, reason=reason))
        return active

    @property
    def context_tokens(self):
        return int(self._cfg('novel_context_tokens', 4000))

    @property
    def summary_tokens(self):
        return int(self._cfg('novel_summary_tokens', 600))

    @property
    def min_chars_for_context(self):
        """Minimum translated-chapter length below which summary+glossary
        LLM calls are skipped. Guards against wasting time on front/back
        matter (Copyright, Table of Contents, About the Author, ...).
        """
        return int(self._cfg('novel_min_chars_for_context', 300))

    @property
    def summary_max_chars(self):
        """Length above which a chapter summary is truncated before it is
        stored.

        A summary is written once and then re-read in the prompt of every
        chapter that follows, so a bad one is not a one-off cost: asked
        for 150 to 350 words, a model was measured answering with the
        whole translated chapter, 30960 characters, which alone fills the
        entire context budget and pushes the glossary and the older
        summaries out of it for the rest of the book.

        Derived from ``novel_summary_tokens`` -- twice the target size,
        so an ordinary summary is never touched -- unless
        ``novel_summary_max_chars`` overrides it.
        """
        override = int(self._cfg('novel_summary_max_chars', 0) or 0)
        if override > 0:
            return override
        return max(500, self.summary_tokens * 4 * 2)

    @property
    def context_max_tokens(self):
        """Hard cap on what the summary and the glossary calls may write.

        Both answers are short by nature -- a few hundred words, a list
        of proper nouns -- but nothing in the request said so, and a
        model that starts repeating itself keeps going until its own
        output limit stops it: one glossary call was measured writing
        131072 tokens over eight minutes, re-listing names it had been
        told to skip. The cap costs nothing when the model behaves and
        bounds the damage when it does not. Set to 0 to leave the
        engine's own limit alone.
        """
        return int(self._cfg('novel_context_max_tokens', 8000))

    @property
    def glossary_chapter_max_entries(self):
        """How many new glossary entries one chapter may add: the
        request says so, the JSON schema enforces it where the server
        honours schemas, and the parser cuts a longer list anyway. A
        model told "at most forty" listed ninety-eight on a crowded
        chapter and ran into the reply cap."""
        try:
            return max(1, int(self._cfg(
                'novel_glossary_chapter_max_entries', 50)))
        except (TypeError, ValueError):
            return 50

    def _entities_schema(self):
        """The entity list of the schemas, capped at
        :attr:`glossary_chapter_max_entries`."""
        schema = dict(self._GLOSSARY_RESPONSE_SCHEMA['properties']['entities'])
        schema['maxItems'] = self.glossary_chapter_max_entries
        return schema

    def _glossary_schema(self):
        schema = dict(self._GLOSSARY_RESPONSE_SCHEMA)
        schema['properties'] = dict(schema['properties'])
        schema['properties']['entities'] = self._entities_schema()
        return schema

    def _context_schema(self):
        schema = dict(self._CONTEXT_RESPONSE_SCHEMA)
        schema['properties'] = dict(schema['properties'])
        schema['properties']['entities'] = self._entities_schema()
        return schema

    @property
    def glossary_relevant_only(self):
        """Whether prompts carry only the glossary entries of the chapter
        at hand, rather than the whole glossary.
        """
        return bool(self._cfg('novel_glossary_relevant_only', True))

    @property
    def glossary_prompt_max_entries(self):
        """Hard cap on the glossary entries any single prompt carries.

        Applied after the relevance filter, keeping the most recently
        learned entries. 0 means no cap.
        """
        return int(self._cfg('novel_glossary_prompt_max_entries', 150))

    @property
    def context_reasoning(self):
        """Whether the summary and the glossary calls may spend reasoning
        tokens.

        Off by default. Neither task is a reasoning task -- one condenses
        a chapter that has just been translated, the other lists the
        proper nouns in it -- and measured against a real chapter the
        glossary call spent three quarters of its output on deliberation
        before writing a short JSON list.
        """
        return bool(self._cfg('novel_context_reasoning', False))

    @property
    def translation_prompt(self):
        value = self._cfg('novel_translation_prompt', None)
        return value or DEFAULT_NOVEL_TRANSLATION_PROMPT

    # The summary, glossary, combined-context and author-brief prompts
    # are the plugin's own and not settings: they ask for a shape the
    # code parses (a JSON object with given fields, a summary of a given
    # size) and a prompt typed by a user broke that shape more often
    # than it improved the answer. The translation prompt stays a
    # setting, because its output is prose.
    summary_prompt = DEFAULT_NOVEL_SUMMARY_PROMPT
    glossary_prompt = DEFAULT_NOVEL_GLOSSARY_PROMPT
    context_prompt = DEFAULT_NOVEL_CONTEXT_PROMPT
    context_source_prompt = DEFAULT_NOVEL_CONTEXT_SOURCE_PROMPT
    author_style_prompt = DEFAULT_NOVEL_AUTHOR_STYLE_PROMPT
    author_check_prompt = DEFAULT_NOVEL_AUTHOR_CHECK_PROMPT

    @property
    def author_style_setting(self):
        """Whether the book gets a brief on how its author writes.

        Values:
          ``'model'`` -- ask the model once, before the first chapter,
                         what it knows of the author's prose. The
                         default.
          ``'off'``   -- do not ask at all.

        A web search used to be an option; it is gone. The pages a
        search finds are about the plot of the author's other books, and
        a model given them declined to answer or placed the book in the
        wrong century. Anything stored as 'auto' from then reads as
        'model'.
        """
        value = self._cfg('novel_author_style', 'model')
        return 'off' if value == 'off' else 'model'

    @property
    def author_check(self):
        """Whether the model is asked, before the brief, if it knows the
        author at all (see ``DEFAULT_NOVEL_AUTHOR_CHECK_PROMPT``). One
        small request; without it a model asked about an invented name
        wrote a confident brief about nobody."""
        return bool(self._cfg('novel_author_check', True))

    @property
    def dialogue_convention(self):
        """How the system prompt states the punctuation of direct
        speech. ``'auto'`` (the default) reads it off the source text,
        chapter by chapter; a key of ``DIALOGUE_CONVENTIONS`` prescribes
        that convention; ``'off'`` says nothing and lets the model
        choose, which is what produced guillemets in one chapter and
        straight quotes in the next.
        """
        value = self._cfg('novel_dialogue_convention', 'auto')
        if value not in ('auto', 'off') and value not in DIALOGUE_CONVENTIONS:
            value = 'auto'
        return value

    def _cache_info(self, key):
        """Read one field of the cache's info table, '' when absent.

        Defensive about the type: the tests hand the translator a mock
        cache, and a mock returns a mock rather than a string.
        """
        try:
            value = self.cache.get_info(key)
        except Exception:
            return ''
        return value if isinstance(value, str) else ''

    @property
    def book_author(self):
        """Who wrote the book, for the author brief.

        Supplied by the caller (the interactive worker reads it from the
        calibre metadata); the cache is the fallback, so a run started
        from the background job still knows.
        """
        value = self._cfg('novel_book_author', None)
        if not value:
            value = self._cache_info('author')
        return (value or '').strip()

    @property
    def book_title(self):
        value = self._cfg('novel_book_title', None)
        if not value:
            value = self._cache_info('title')
        return (value or '').strip()

    @property
    def combined_context_call(self):
        """Whether the summary and the glossary are asked for at once.

        On by default: the two tasks read the same chapter, so keeping
        them apart sends it twice.
        """
        return bool(self._cfg('novel_combined_context_call', True))

    @property
    def context_narrative_only(self):
        """Whether the summary and the glossary are kept only for the
        chapters that belong to the story.

        On by default. The model that summarises a chapter is asked
        whether the chapter is part of the story at all; a copyright
        page, a list of the author's other books, a preface or a note
        gets no summary and no glossary entries, which would otherwise
        be carried into every later prompt as if they were plot.
        """
        return bool(self._cfg('novel_context_narrative_only', True))

    @property
    def skip_context_last_chapter(self):
        """Whether the last chapter skips the summary and glossary call.

        On by default: nothing ever reads them. The context of a chapter
        is built for the chapters that follow it, and after the last one
        there are none -- the call is one full copy of the chapter sent
        for an answer that is stored and never looked at again.
        """
        return bool(self._cfg('novel_skip_context_last_chapter', True))

    # A chunk is answered with the same text in another language, plus
    # the JSON scaffolding around it: the reply is about as long as what
    # was sent, longer when the target language is wordier than the
    # source. Two thirds of the model's output limit leaves room for
    # both without wasting most of the window.
    # How long a reply is for a chunk of a given size: the translation
    # runs to 1.2-1.5 times the source in most language pairs, and the
    # JSON or the markers around it add a fifth. And what a reply needs
    # over that: the object's own scaffolding, a few entries repeated.
    # One pair of numbers sizes both the chunk (how much source fits
    # under the reply room) and the room asked for a chunk (how much
    # the reply of that source needs), so the two cannot disagree; they
    # used to, and a chunk sized for 11000 source tokens was given room
    # for 16384 of reply, a third short.
    REPLY_RATIO = 2.2
    REPLY_MARGIN = 1500

    @property
    def output_aware_chunking(self):
        """Whether the chunk size is capped by what the model can write.

        On by default. ``novel_chunk_tokens`` says how much to send;
        nothing in it says how much the model is able to answer, and the
        two have nothing to do with each other -- context windows are
        measured in hundreds of thousands of tokens while reply limits
        run from 4096 up. A chunk the model cannot finish is cut
        mid-answer, and the paragraphs that were lost are asked for
        again: the request is paid twice and was never going to fit.
        """
        return bool(self._cfg('novel_output_aware_chunking', True))

    # No model writes more than this in one reply; a listing that says
    # so is quoting the context window (OpenRouter has reported 943718
    # for a model with a million-token context), and is not believed.
    MODEL_OUTPUT_LIMIT_CEILING = 262144

    @property
    def model_output_limit(self):
        """The longest reply the configured model will write, in tokens,
        as its provider reported it when the model was chosen in the
        setting dialog. 0 when nobody ever said, or when the figure is
        not a reply limit at all."""
        try:
            limit = max(0, int(getattr(
                self.translator, 'model_max_output_tokens', 0) or 0))
        except (TypeError, ValueError):
            return 0
        return limit if limit <= self.MODEL_OUTPUT_LIMIT_CEILING else 0

    def _effective_chunk_tokens(self, reserved=0):
        """The tokens of source text one chunk may carry.

        Two ceilings, and the lower one wins. The configured budget minus
        ``reserved`` is what the context window leaves once the system
        prompt, the running summary, the glossary and the overlap are in;
        the reply limit of the model, when the provider published one,
        is how much of a translation it can write back. The two are
        unrelated -- the reserve is input, the limit is output -- which
        is why the reserve is subtracted from the budget and never from
        the limit: taken off both, a 4096-token limit left 200 tokens per
        chunk and a chapter went out one paragraph at a time.

        Never raises the configured budget, only lowers it, and says so
        once when the model limit is what lowers it.
        """
        wanted = max(200, self.chunk_tokens - int(reserved or 0))
        room = self.reply_room_limit
        if not (self.output_aware_chunking and room):
            return wanted
        allowed = self.source_for_reply(room)
        if allowed >= wanted:
            return wanted
        if not self._chunk_budget_logged:
            self._chunk_budget_logged = True
            self.log(_(
                'Chunk budget lowered from {} to {} source tokens: a reply '
                'may run to {} tokens ({}), and a translation is about '
                '{} times its source with the JSON around it.').format(
                    wanted, allowed, room, self._reply_room_reason(),
                    self.REPLY_RATIO))
        return allowed

    @property
    def reply_room_limit(self):
        """The most a reply may run to, in tokens: the lower of what the
        model can write and the cap in the settings, 0 when neither is
        known."""
        limits = [n for n in (self.model_output_limit, self.reply_max_tokens)
                  if n]
        return min(limits) if limits else 0

    def _reply_room_reason(self):
        limit, cap = self.model_output_limit, self.reply_max_tokens
        if limit and (not cap or limit <= cap):
            return _('the model\'s limit')
        return _('the cap in the settings')

    def source_for_reply(self, room):
        """How much source fits in a chunk whose reply may run to
        ``room`` tokens."""
        return max(300, int((room - self.REPLY_MARGIN) / self.REPLY_RATIO))

    def reply_for_source(self, source_tokens):
        """How much room the reply of ``source_tokens`` needs."""
        return int(self.REPLY_RATIO * source_tokens) + self.REPLY_MARGIN

    def _log_sizing(self):
        """Say once, at the start, how the requests are sized and why."""
        reserved = (self.context_tokens + self.summary_tokens
                    + self.overlap_paragraphs * 80)
        room = self.reply_room_limit
        source = self._effective_chunk_tokens(reserved)
        parts = [_('Sizing: {} tokens of budget less {} reserved for the '
                   'running context').format(self.chunk_tokens, reserved)]
        if room and self.output_aware_chunking:
            parts.append(_(
                'a reply of at most {} tokens ({}) holds about {} of '
                'source').format(room, self._reply_room_reason(),
                                 self.source_for_reply(room)))
        parts.append(_('so a chunk carries up to {} source tokens').format(
            source))
        if self.max_paragraphs_per_chunk:
            parts.append(_('and {} paragraphs').format(
                self.max_paragraphs_per_chunk))
        self.log('; '.join(parts) + '.')

    @property
    def reuse_translated_paragraphs(self):
        """Whether paragraphs the cache already holds are kept instead of
        being sent to the model again.

        On by default. Translations are written after every chunk while
        the progress counter only moves once a chapter is finished, so a
        run cancelled at chunk 7 of 9 would otherwise pay for those seven
        chunks a second time. Turn it off to force a fresh translation of
        every paragraph of the chapters that are still pending.
        """
        return bool(self._cfg('novel_reuse_translated_paragraphs', True))

    @property
    def verify_alignment(self):
        """Whether the translations of a reply are checked against their
        paragraphs before they are kept (see
        :func:`suspicious_translations`); the ones that fail are asked
        for again with the missing ones. On by default: the check costs
        nothing, and without it a reply whose numbers slipped goes into
        the book as it is."""
        return bool(self._cfg('novel_verify_alignment', True))

    @property
    def log_reply_excerpt(self):
        """How many characters of a reply that covered fewer paragraphs
        than asked are put in the log, so that what the model did
        instead can be seen. 0 logs the count only."""
        try:
            return max(0, int(self._cfg('novel_log_reply_excerpt', 300)))
        except (TypeError, ValueError):
            return 300

    @property
    def retry_split(self):
        """Whether a retry asks for the missing paragraphs in two halves
        rather than all at once. A model that could not manage a chunk
        seldom manages the same paragraphs again at the same size; what
        it missed was asked for again three times at that size before
        the chapter-level pass in smaller chunks."""
        return bool(self._cfg('novel_retry_split', True))

    @property
    def provider_failures_before_exclusion(self):
        """How many unreliable replies -- shifted numbers, a reply
        covering less than half of what was asked -- a provider may
        give before the engine is told not to route to it for the rest
        of the run. 0 never excludes anyone. Only engines that route
        between providers (OpenRouter) act on it."""
        try:
            return max(0, int(self._cfg(
                'novel_provider_failures_before_exclusion', 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def realign_shifted_replies(self):
        """Whether a reply whose numbers slipped is read as it was meant
        (see :func:`realign_shifted`) instead of asked for again."""
        return bool(self._cfg('novel_realign_shifted_replies', True))

    @property
    def front_matter_titles(self):
        """Comma-separated words a chapter title carries when the
        chapter is not part of the story (see ``FRONT_MATTER_TITLES``):
        it is translated, but no summary or glossary is asked for it."""
        value = self._cfg('novel_front_matter_titles', None)
        return FRONT_MATTER_TITLES if value is None else str(value)

    @property
    def untranslated_titles(self):
        """Comma-separated words a chapter title carries when the
        chapter stays in its original language (see
        ``UNTRANSLATED_TITLES``)."""
        value = self._cfg('novel_untranslated_titles', None)
        return UNTRANSLATED_TITLES if value is None else str(value)

    @property
    def context_timing(self):
        """'before': the summary and the glossary of a chapter are asked
        from its source before it is translated, at half the tokens,
        and the glossary guides every chunk of the chapter itself.
        'after': from source and translation together once the chapter
        is done, as before."""
        value = str(self._cfg('novel_context_timing', 'before') or 'before')
        return 'after' if value == 'after' else 'before'

    @property
    def parallel_chunks(self):
        """How many chunks of one chapter may be in flight at once. 1,
        the default, is the sequential pipeline. More than 1 sends the
        chunks of a chapter together, each on its own copy of the engine,
        and the context around a chunk is then the source text (see
        :attr:`chunk_context`), because the translated overlap needs the
        previous chunk to be done. Chapters stay sequential whatever the
        value: each depends on the summary of the one before."""
        try:
            return max(1, min(16, int(self._cfg('novel_parallel_chunks', 1))))
        except (TypeError, ValueError):
            return 1

    @property
    def balanced_chunks(self):
        """Whether chunks in flight together are cut to about the same
        size (see :meth:`TokenBudget.balance`): a chapter then takes as
        long as its longest chunk, and the longest one is as short as
        it can be."""
        return bool(self._cfg('novel_balanced_chunks', True))

    @property
    def chunk_context(self):
        """What a chunk is shown of its surroundings: 'translated', the
        last paragraphs of the previous chunk as the model rendered them
        (the overlap; sequential only), or 'source', the source text of
        the paragraphs before and after it, which needs nothing to have
        been translated yet and shows what comes next as well."""
        value = str(self._cfg('novel_chunk_context', 'translated')
                    or 'translated')
        return 'source' if value == 'source' else 'translated'

    @property
    def source_context_paragraphs(self):
        """How many source paragraphs before and after a chunk are shown
        as context when :attr:`chunk_context` is 'source'."""
        try:
            return max(0, int(self._cfg('novel_source_context_paragraphs', 5)))
        except (TypeError, ValueError):
            return 5

    @property
    def reply_max_tokens(self):
        """The most a translation request may ask the model to write,
        in tokens, when the engine leaves the figure to the provider.

        Left out, a provider applies a default of its own -- 4096 on
        many -- and a chunk of seventy-five paragraphs came back cut at
        a third, three times over, before the paragraphs were asked for
        in smaller pieces. Each chunk asks for what it needs, twice its
        own size plus room for the JSON, up to this cap and to what the
        model can write. 0 sends nothing and leaves it to the provider.
        """
        return max(0, int(self._cfg('novel_reply_max_tokens', 16384) or 0))

    @contextmanager
    def _reply_room(self, chunk_paragraphs):
        """Set the engine's reply limit for one chunk, when the engine
        leaves it to the provider, and put it back afterwards."""
        translator = self.translator
        cap = self.reply_max_tokens
        current = 0
        if cap and hasattr(translator, 'max_tokens'):
            try:
                current = int(getattr(translator, 'max_tokens', 0) or 0)
            except (TypeError, ValueError):
                current = 0
        if not cap or current > 0 or not hasattr(translator, 'max_tokens'):
            yield
            return
        source = sum(self._estimate_tokens(p.original or '')
                     for p in chunk_paragraphs
                     if not getattr(p, 'ignored', False))
        wanted = min(cap, self.reply_for_source(source))
        if self.model_output_limit:
            wanted = min(wanted, self.model_output_limit)
        if not self._reply_room_logged:
            self._reply_room_logged = True
            self.log(_(
                'Reply room: each chunk asks the model for {} times its '
                'source plus {} tokens, capped at {} tokens '
                '(novel_reply_max_tokens).').format(
                    self.REPLY_RATIO, self.REPLY_MARGIN, cap))
        translator.max_tokens = wanted
        try:
            yield
        finally:
            translator.max_tokens = 0

    @property
    def rate_limit_max_wait(self):
        """How long, in seconds, a rate-limited request may be waited
        out before it counts as a failure. A 429 says "not now", not
        "never": the three attempts used to be spent in a second on a
        provider that asked for one second of patience. 0 treats a rate
        limit like any other error."""
        return max(0, int(self._cfg('novel_rate_limit_max_wait', 600) or 0))

    @property
    def on_missing_paragraphs(self):
        """What to do with paragraphs the model never returned, once the
        retries inside a chunk and one more pass in smaller chunks have
        all been tried.

        'stop', the default, ends the run with the chapter unfinished:
        its progress is not recorded, so a resume asks for exactly those
        paragraphs again and nothing else. 'continue' logs them and goes
        on to the next chapter; they keep their source text in the
        output, and nothing comes back for them later, because a resume
        starts after the last finished chapter.
        """
        value = str(self._cfg(
            'novel_on_missing_paragraphs', 'stop') or 'stop').lower()
        return 'continue' if value == 'continue' else 'stop'

    @property
    def prompt_cache(self):
        """Whether the engine is asked to cache the prompt prefix.

        Every chunk of a chapter is sent with the same system prompt --
        role, languages, running summary and glossary, a few thousand
        tokens of it -- and providers charge a fraction of the price for
        a prefix they already hold. Engines that cache on their own
        (OpenAI, Gemini, DeepSeek) need nothing from us and ignore this;
        it is the engines with explicit cache breakpoints, Claude today,
        that read it.
        """
        return bool(self._cfg('novel_prompt_cache', True))

    # -- engine plumbing ---------------------------------------------------

    def _apply_prompt(self, prompt_text):
        """Swap the engine's system prompt for the duration of one request.

        Uses ``override_prompt`` if defined by the engine (GenAI helper),
        else falls back to assigning to ``.prompt`` directly.
        """
        # Engines that support explicit prompt-cache breakpoints read this
        # attribute; the others never look at it.
        self.translator.prompt_cache = self.prompt_cache
        if hasattr(self.translator, 'override_prompt'):
            self.translator.override_prompt(prompt_text)
        else:
            self.translator.prompt = prompt_text

    def _restore_prompt(self):
        if hasattr(self.translator, 'restore_prompt'):
            self.translator.restore_prompt()

    def _fill_placeholders(self, template, extra=None):
        source_lang = getattr(self.translator, 'source_lang', '') or ''
        target_lang = getattr(self.translator, 'target_lang', '') or ''
        replacements = {
            '<slang>': source_lang or 'source language',
            '<tlang>': target_lang or 'target language',
        }
        if extra:
            replacements.update(extra)
        for k, v in replacements.items():
            template = template.replace(k, v)
        return template

    def _compose_prompt(self, template, values, required=()):
        """Fill a prompt template without demanding any placeholder.

        Substitution is literal, never through ``str.format``: a prompt
        typed by hand is very likely to contain a stray brace, and
        ``format`` would raise on it instead of translating the chapter.
        Every entry named in ``required`` that the template does not
        mention is appended at the end under its label, so a prompt whose
        author never heard of the placeholders still receives the chapter
        text, the running context and the rest.

        :values: ``{placeholder: (label, value)}``. The label is only used
            when the value has to be appended.
        """
        filled = self._fill_placeholders(template, extra={
            placeholder: value
            for placeholder, (_label, value) in values.items()})
        appended = []
        for placeholder in required:
            label, value = values.get(placeholder, (None, ''))
            if placeholder in template or not value:
                continue
            appended.append('%s\n%s' % (label, value) if label else value)
        if appended:
            filled = '%s\n\n%s' % (filled.rstrip(), '\n\n'.join(appended))
        return filled

    # -- book-wide directives ---------------------------------------------

    @property
    def dialogue_rules(self):
        """How direct speech is punctuated, stated once for the book.

        Worked out on first use and kept: the source does not change
        between chapters, so neither does the answer.
        """
        if self._dialogue_rules is None:
            self._dialogue_rules = self._build_dialogue_rules()
        return self._dialogue_rules

    def _ensure_dialogue_rules(self):
        """Work the rule out now, so that its log line lands with the
        other book-wide decisions instead of inside the first chunk."""
        return self.dialogue_rules

    def _build_dialogue_rules(self):
        if self.dialogue_convention == 'off':
            return ''
        custom = (self._cfg('novel_dialogue_rules', '') or '').strip()
        if custom:
            self.log(_('Dialogue punctuation: using the rule from the '
                       'settings.'))
            return custom
        convention = self.dialogue_convention
        if convention != 'auto':
            rules = dialogue_instruction(dialogue_convention_style(convention))
            self.log(_(
                'Dialogue punctuation as set in the settings: {}. Every '
                'chapter will be asked to use it.').format(
                    DIALOGUE_CONVENTIONS[convention]['label'].strip()))
            return rules
        style = detect_book_dialogue_style(
            [paragraph.original
             for paragraph in chapter.paragraphs
             if not getattr(paragraph, 'ignored', False)]
            for chapter in self.chapters)
        rules = dialogue_instruction(style)
        if not rules:
            self.log(_('Dialogue punctuation: the source shows no '
                       'convention to follow.'))
            return ''
        marks = ' '.join(
            '%s=%d' % (mark, count)
            for mark, count in sorted(
                style['counts'].items(), key=lambda i: -i[1])[:4])
        self.log(_(
            'Dialogue punctuation read off the source: {primary}, the '
            'choice of {votes} of the {voters} chapter(s) that show one '
            '(counts: {counts}; paragraphs opening with a dash: {dash}). '
            'Every chapter will be asked to use it.').format(
                primary=style.get('primary') or '—',
                votes=style.get('votes', 0), voters=style.get('voters', 0),
                counts=marks or _('none'),
                dash=style.get('dash_paragraphs', 0)))
        return rules

    @property
    def author_style(self):
        """The brief on how this book is written, '' when there is none.

        Read back from the stored context when the research has not run
        in this session, so a translation resumed in a new process keeps
        the brief it was started with.
        """
        if self._author_style is None:
            self._author_style = self.ctx.get_style()
        return self._author_style

    def _style_block(self):
        style = self.author_style
        if not style:
            return ''
        return '%s\n%s' % (model_text(
            'How this book is written. The brief below describes the '
            'author and this novel. Reproduce the manner it describes, '
            'except where the text in front of you plainly contradicts '
            'it: the text always wins.'), style)

    def _ensure_author_style(self):
        """Research once, before the first chapter, how the book is written.

        The answer is stored with the summaries and the glossary, so it
        is paid for once per book and survives a resume. Every failure
        along the way -- no author in the metadata, a request that
        errors, a model that admits it knows nothing -- ends with an
        empty brief and a translation that proceeds without one: the
        alternative, a brief the model made up, would misdirect every
        paragraph of the book.
        """
        if self.author_style_setting == 'off':
            return self._no_author_style(_(
                'The author brief is turned off in the settings (Novel '
                'Mode, "How the author writes").'))
        existing = self.ctx.get_style()
        if existing:
            self._author_style = existing
            self.log(_('Author brief: reusing the one on file '
                       '({} characters).').format(len(existing)))
            return existing
        author = self.book_author
        if not author:
            self.log(_('Author brief: skipped, the book carries no author '
                       'in its metadata.'))
            return self._no_author_style(_(
                'No brief: the book carries no author in its calibre '
                'metadata, so there was nobody to ask about. Set the '
                'author in calibre and use "Reset context" to ask again.'))
        works = ''
        if self.author_check:
            known = self._recognise_author(author)
            if known is not None and not known.get('recognised'):
                self.log(_(
                    'Author brief: the model does not know "{author}" '
                    '(it says: {note}); the translation continues without '
                    'a brief rather than with one made up.').format(
                        author=author, note=known.get('note') or '-'))
                return self._no_author_style(_(
                    'No brief: asked whether it knows {author}, the model '
                    'answered that it cannot name any book by this author '
                    '({note}). A brief from it would be invented, so the '
                    'translation goes on without one. Pick a model that '
                    'knows the author, then use "Reset context" so the '
                    'question is asked again.').format(
                        author=author, note=known.get('note') or 'unknown'))
            if known is not None:
                works = ', '.join(
                    str(w).strip() for w in known.get('works') or []
                    if str(w).strip())
                self.log(_(
                    'Author brief: the model knows "{author}" ({note}){works}'
                    '{shared}.').format(
                        author=author, note=known.get('note') or '-',
                        works=(_('; works it names: {}').format(works)
                               if works else ''),
                        shared=(_('; it warns that more than one writer '
                                  'has this name') if known.get(
                                      'shared_name') else '')))
        self.log(_('Author brief: asking the model how "{author}" '
                   'writes.').format(author=author))
        user_prompt = self._compose_prompt(
            self.author_style_prompt,
            {'{author}': (model_text('Author:'), author),
             '{works}': (model_text('Known works:'), works),
             '{title}': (model_text('Book:'), self.book_title)},
            required=('{author}', '{title}'))
        system_prompt = self._fill_placeholders(model_text(
            'You research how books are written and answer in plain '
            'prose, saying only what you can support.'))
        try:
            response = self._author_style_call(system_prompt, user_prompt)
            brief = collapse_blank_lines(response)
        except TranslationCanceled:
            raise
        except Exception as e:
            self.log(_('Author brief: the request failed ({}). The '
                       'translation continues without it.').format(
                           describe_error(e)), True)
            return self._no_author_style(_(
                'No brief: the request for it failed ({}). The translation '
                'went on without one; "Reset context" asks again.')
                .format(describe_error(e)))
        if self._is_no_information(brief):
            self.log(_('Author brief: the model has no reliable information '
                       'about this author and said so; the translation '
                       'continues without a brief.'))
            return self._no_author_style(_(
                'No brief: the model was asked how {author} writes and '
                'answered that it has no reliable information about this '
                'author and this book, which is the honest answer and '
                'better than an invented one. The translation goes on '
                'without a brief.\n\n'
                'To get one, pick a model that knows the author, then '
                'use "Reset context" so the question is asked '
                'again.').format(author=author))
        if len(brief) < 80:
            self.log(_('Author brief: nothing usable came back, the '
                       'translation continues without one.'))
            return self._no_author_style(_(
                'No brief: the model answered with too little to be one '
                '({} characters). The translation goes on without a '
                'brief; "Reset context" asks again.').format(len(brief)))
        self._author_style = self.ctx.set_style(brief)
        self.log(_('Author brief ({} characters):').format(len(brief)))
        self.log(brief)
        return self._author_style

    @staticmethod
    def _is_no_information(brief):
        """Whether a reply is the model declining, however it dressed
        the words up."""
        condensed = re.sub(r'[^A-Z ]', '', (brief or '').upper()).strip()
        return condensed.startswith(NO_AUTHOR_INFORMATION)

    def _no_author_style(self, note):
        """Go on without a brief, leaving ``note`` for the window."""
        self._author_style = ''
        self.ctx.set_style_note(note)
        return ''

    def _recognise_author(self, author):
        """Ask whether the model knows ``author``: the parsed JSON reply
        (``recognised``, ``works``, ``shared_name``, ``note``), or None
        when the request failed or the reply was not readable, in which
        case the brief is asked for anyway: a parsing problem is no
        reason to lose it."""
        user_prompt = self._compose_prompt(
            self.author_check_prompt,
            {'{author}': (model_text('Author:'), author),
             '{title}': (model_text('Book in hand:'), self.book_title)},
            required=('{author}', '{title}'))
        system_prompt = model_text(
            'You answer with strict JSON only, and only from what you '
            'actually know. Never invent a book or an author.')
        self._request_kind = 'author'
        try:
            response = self._translate_context_call(
                system_prompt, user_prompt, _('the author check'),
                schema=self._AUTHOR_CHECK_SCHEMA)
        except TranslationCanceled:
            raise
        except Exception as e:
            self.log(_('Author check: the request failed ({}); asking for '
                       'the brief anyway.').format(describe_error(e)), True)
            return None
        obj = _extract_json_object(response)
        if not isinstance(obj, dict) or 'recognised' not in obj:
            self.log(_('Author check: the reply was not readable; asking '
                       'for the brief anyway.'))
            return None
        return {
            'recognised': bool(obj.get('recognised')),
            'works': [w for w in (obj.get('works') or [])
                      if isinstance(w, str)][:5],
            'shared_name': bool(obj.get('shared_name')),
            'note': str(obj.get('note') or '').strip(),
        }

    def _author_style_call(self, system_prompt, user_prompt):
        """Run the request for the brief: one context call, counted
        under its own kind."""
        self._request_kind = 'author'
        return self._translate_context_call(
            system_prompt, user_prompt, _('the author brief'))

    def _translation_system_prompt(self, context_text):
        """Build the system prompt of one translation request.

        Shared by the marker and the structured path. The language
        directive is supplied here when the template omits it, so not
        even ``<slang>``/``<tlang>`` are mandatory in the settings dialog.
        """
        template = self.translation_prompt
        prompt = self._compose_prompt(
            template, {
                '{dialogue}': (None, self.dialogue_rules),
                '{style}': (None, self._style_block()),
                '{context}': (None, context_text),
            },
            # Ordered by how often the value changes, because a template
            # that names none of them gets them appended in this order
            # and the provider's prefix cache reuses everything up to the
            # first byte that differs.
            required=('{dialogue}', '{style}', '{context}'))
        if '<tlang>' not in template and '<slang>' not in template:
            prompt = '%s\n\n%s' % (
                self._fill_placeholders(
                    model_text('Translate from <slang> to <tlang>.')),
                prompt)
        # A book with no author brief, or a source that punctuates
        # nothing, leaves its placeholder empty and its blank lines
        # behind.
        return collapse_blank_lines(prompt)

    def _run_translation_call(self, user_text):
        """Invoke ``translator.translate`` handling both plain-string and
        generator (streaming) return values.
        """
        result = self.translator.translate(user_text)
        # Streaming: collect the generator.
        if hasattr(result, '__iter__') and not isinstance(result, str):
            try:
                result = ''.join(chunk for chunk in result)
            except TypeError:
                # Not actually a generator (e.g. bytes), let the engine
                # deal with it.
                pass
        return result or ''

    def _estimate_tokens(self, text):
        """Approximate token count, using the chunker's own estimator so
        the sizes in the log are comparable to the caps in the settings.
        """
        if getattr(self, '_token_estimator', None) is None:
            self._token_estimator = TokenBudget()
        return self._token_estimator.estimate(text or '')

    def _translate_with_retry(self, system_prompt, user_text, attempts=None,
                              label=None):
        """Send ``system_prompt`` + ``user_text`` to the LLM with retries.

        Retries here are for total failures of the request (network,
        parsing, ...). Alignment-level retries are handled separately by
        ``_translate_chunk``.

        Every attempt is bracketed by a log line naming ``label`` and the
        size of what is being sent and received, and the answer's line
        carries how long it took. Without them a slow request and a hung
        one look identical from the outside, which is a real risk here: a
        reasoning model whose chain of thought is excluded from the
        stream sends no bytes at all while it thinks, and a single chunk
        has been observed taking a quarter of an hour with nothing at all
        printed in between.
        """
        attempts = attempts or getattr(
            self.translator, 'request_attempt', 3) or 3
        label = label or _('request')
        last_error = None
        attempt = 0
        # Seconds spent waiting out rate limits so far, and how many
        # times in a row; both bound the patience.
        waited = 0.0
        limited = 0
        relaxed = False
        while attempt < attempts:
            if self.cancel_request():
                raise TranslationCanceled(_('Translation canceled.'))
            started = time.time()
            self.log(_(
                '  -> {}: sent {} chars (~{} tokens), waiting for the '
                'model...').format(
                    label, len(user_text),
                    self._estimate_tokens(user_text)))
            try:
                self._apply_prompt(system_prompt)
                result = self._run_translation_call(user_text)
                if self.cancel_request():
                    # The request was cut short from outside (see
                    # ``Base.abort``): whatever came back is not a reply.
                    raise TranslationCanceled(_('Translation canceled.'))
                self._account(user_text, result, time.time() - started)
                self.log(_(
                    '  <- {}: {} chars (~{} tokens) in {}s{}.').format(
                        label, len(result), self._estimate_tokens(result),
                        round(time.time() - started, 1),
                        self._reply_notes()))
                return result
            except TranslationCanceled:
                raise
            except Exception as e:
                if self.cancel_request():
                    raise TranslationCanceled(_('Translation canceled.'))
                last_error = e
                elapsed = round(time.time() - started, 1)
                # A provider list narrowed to one that does not take
                # every parameter leaves OpenRouter nothing to route to.
                # Asking without insisting on the parameters is the way
                # through; done once, and said.
                relax = getattr(self.translator, 'relax_parameters', None)
                if is_routing_dead_end(e) and not relaxed and relax \
                        and relax():
                    relaxed = True
                    self.log(_(
                        '{label}: no provider takes every parameter of '
                        'the request ({error}). Asking again without '
                        'requiring them.').format(
                            label=label, error=describe_error(e)), True)
                    continue
                # A rate limit says "not now", not "never": wait what
                # the provider asks, a little longer each time, and do
                # not spend an attempt on it -- within reason.
                delay = retry_after(e) or 0
                delay = max(delay, min(60, 2.0 * 2 ** limited))
                if is_rate_limited(e) and waited + delay \
                        <= self.rate_limit_max_wait:
                    limited += 1
                    waited += delay
                    self.log(_(
                        '{label}: rate limited ({error}); trying again '
                        'in {delay}s.').format(
                            label=label, error=describe_error(e),
                            delay=int(delay)), True)
                    self._wait(delay)
                    continue
                attempt += 1
                self.log(
                    _('Novel mode request failed after {}s '
                      '(attempt {}/{}): {}').format(
                          elapsed, attempt, attempts,
                          describe_error(e)), True)
                if attempt < attempts:
                    self._wait(min(30, 5 * attempt))
            finally:
                self._restore_prompt()
        raise TranslationFailed(
            _('Novel mode: giving up after {} attempts. Last error: {}')
            .format(attempts, describe_error(last_error)))

    def _reply_fact(self, name, kind=str):
        """What the engine recorded about its last reply under ``name``
        (``last_provider``, ``last_usage``, ...), or None when it
        recorded nothing of that ``kind``: engines that do not report
        have no such attribute, and nothing else passes for one."""
        value = getattr(self.translator, name, None)
        return value if isinstance(value, kind) and value else None

    def _reply_notes(self):
        """What the engine learnt about the last reply besides its text:
        the provider that served it, when a gateway names one, what it
        cost when the provider says, and why the model stopped when it
        was not because it had finished. A reply cut at the output limit
        and a model that left early look the same from the outside -- a
        short answer -- and only this tells them apart."""
        notes = ''
        served = self._reply_fact('last_provider')
        if served:
            notes += _(' via {}').format(served)
        usage = self._reply_fact('last_usage', dict) or {}
        if usage.get('prompt_tokens') is not None:
            notes += _(', {} in + {} out tokens').format(
                usage.get('prompt_tokens'), usage.get('completion_tokens'))
            if usage.get('cost') is not None:
                notes += ', $%s' % _money(usage.get('cost'))
        reason = self._reply_fact('last_finish_reason')
        if reason and reason.lower() not in ('stop', 'end_turn'):
            notes += _(' -- stopped early: {}').format(reason)
        generation = self._reply_fact('last_generation_id')
        if generation:
            # What the gateway's own records list the reply under.
            notes += ' [%s]' % generation
        return notes

    # -- what the book costs ---------------------------------------------

    USAGE_KINDS = ('translation', 'retry', 'context', 'author', 'other')

    def _account(self, sent, received, seconds=0.0):
        """Add the reply just received to the running totals, under the
        kind of request it answered and the provider that served it,
        with the time it took. Tokens the provider does not report are
        estimated and counted as such."""
        kind = self._request_kind if self._request_kind in self.USAGE_KINDS \
            else 'other'
        with self._lock:
            self._account_locked(kind, sent, received, seconds)

    def _account_locked(self, kind, sent, received, seconds):
        entry = self.usage.setdefault(kind, {
            'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
            'cost': 0.0, 'estimated': 0, 'seconds': 0.0})
        entry['seconds'] = entry.get('seconds', 0.0) + float(seconds or 0.0)
        usage = self._reply_fact('last_usage', dict) or {}
        try:
            prompt = int(usage.get('prompt_tokens'))
            completion = int(usage.get('completion_tokens') or 0)
        except (TypeError, ValueError):
            prompt = self._estimate_tokens(sent)
            completion = self._estimate_tokens(received)
            entry['estimated'] += 1
        entry['requests'] += 1
        entry['prompt_tokens'] += prompt
        entry['completion_tokens'] += completion
        try:
            entry['cost'] += float(usage.get('cost') or 0.0)
        except (TypeError, ValueError):
            pass
        provider = self._reply_fact('last_provider')
        if provider:
            self._provider(provider)['requests'] += 1

    def _provider(self, name):
        stats = self.provider_stats.setdefault(
            name or _('(provider not named)'),
            {'requests': 0, 'failures': 0, 'excluded': False})
        stats.setdefault('chars', 0)
        stats.setdefault('bad_chars', 0)
        return stats

    def _audit_reply(self, sources, expected, parsed, moved):
        """Add a reply to the share of the text that gave trouble: the
        source characters asked for, and among them the ones whose
        translation is missing, was set aside, or had to be moved."""
        if not expected:
            return
        asked = sum(len(sources.get(n, '') or '') for n in expected)
        bad = sum(
            len(sources.get(n, '') or '') for n in expected
            if n not in parsed or n in moved)
        with self._lock:
            self.text_stats['chars'] = \
                self.text_stats.get('chars', 0) + asked
            self.text_stats['bad_chars'] = \
                self.text_stats.get('bad_chars', 0) + bad
            provider = self._reply_fact('last_provider')
            if provider:
                stats = self._provider(provider)
                stats['chars'] += asked
                stats['bad_chars'] += bad

    def _note_provider_failure(self, what):
        """One more unreliable reply from the provider that served the
        last one. Past the configured number, an engine that routes
        between providers is told to leave it out for the rest of the
        run."""
        provider = self._reply_fact('last_provider') or ''
        with self._lock:
            stats = self._provider(provider)
            stats['failures'] += 1
            threshold = self.provider_failures_before_exclusion
            if not provider or not threshold or stats['excluded'] \
                    or stats['failures'] < threshold:
                return
            # Every engine in play: this one, the root one the next
            # chunks are cloned from, and the copies in flight.
            engines = []
            for engine in [self.translator, self._root.translator] + list(
                    getattr(self._root.translator, 'clones', None) or []):
                if not any(engine is known for known in engines):
                    engines.append(engine)
            excluded = False
            for engine in engines:
                exclude = getattr(engine, 'exclude_provider', None)
                if not callable(exclude):
                    continue
                try:
                    excluded = bool(exclude(provider)) or excluded
                except Exception as e:
                    self.log(_('Could not exclude {}: {}').format(
                        provider, describe_error(e)), True)
                    return
        if excluded:
            stats['excluded'] = True
            self.log(_(
                '{provider} gave {count} unreliable replies (the last: '
                '{what}): excluded for the rest of this run '
                '(novel_provider_failures_before_exclusion).').format(
                    provider=provider, count=stats['failures'],
                    what=what), True)

    def _load_usage(self):
        """The totals of the earlier runs on this book, if any."""
        try:
            stored = json.loads(self.cache.get_info(INFO_NOVEL_USAGE) or '')
        except (TypeError, ValueError):
            stored = None
        if not isinstance(stored, dict):
            return
        usage = stored.get('usage')
        if isinstance(usage, dict):
            self.usage = {
                kind: dict(entry) for kind, entry in usage.items()
                if isinstance(entry, dict)}
        providers = stored.get('providers')
        if isinstance(providers, dict):
            self.provider_stats = {
                name: dict(entry) for name, entry in providers.items()
                if isinstance(entry, dict)}
            # An exclusion lasts one run: the next one starts afresh.
            for entry in self.provider_stats.values():
                entry['excluded'] = False
        try:
            self.realigned = int(stored.get('realigned') or 0)
        except (TypeError, ValueError):
            self.realigned = 0
        text = stored.get('text')
        if isinstance(text, dict):
            self.text_stats = {
                'chars': int(text.get('chars') or 0),
                'bad_chars': int(text.get('bad_chars') or 0)}
        try:
            self.run_seconds = float(stored.get('run_seconds') or 0.0)
        except (TypeError, ValueError):
            self.run_seconds = 0.0

    def _publish_report(self):
        """Write the totals and the report to the cache, and hand the
        report to whoever asked for it."""
        try:
            self.cache.set_info(INFO_NOVEL_USAGE, json.dumps({
                'usage': self.usage, 'providers': self.provider_stats,
                'realigned': self.realigned, 'text': self.text_stats,
                'run_seconds': self.run_seconds + self._run_elapsed()}))
            html = self.build_report_html()
            self.cache.set_info(INFO_NOVEL_REPORT, html)
        except Exception as e:
            self.log(_('Could not save the report: {}').format(
                describe_error(e)), True)
            return
        self.report(html)

    def _run_elapsed(self):
        """Seconds since this run started, 0 before it did."""
        started = getattr(self, '_run_started', None)
        return time.time() - started if started else 0.0

    # Below this share of the text a provider's trouble is noted, not
    # advised against: one bad reply in a long book happens to anyone.
    ADVICE_BAD_SHARE = 0.05

    @staticmethod
    def _share(part, whole):
        """``part`` over ``whole`` as a percentage string, "0%" when
        there is nothing to measure."""
        if not whole:
            return '0%'
        value = 100.0 * part / whole
        return ('%.1f%%' if value < 10 else '%.0f%%') % value

    @staticmethod
    def _duration(seconds):
        """Seconds as "1h 02m 03s", "4m 05s" or "12s"."""
        seconds = int(round(seconds or 0))
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return '%dh %02dm %02ds' % (hours, minutes, secs)
        if minutes:
            return '%dm %02ds' % (minutes, secs)
        return '%ds' % secs

    def report_data(self):
        """What the report is drawn from: the rows of the usage table
        with their total, the providers and the advice, as plain values.
        The text and the HTML renderings both read this."""
        kinds = {
            'translation': _('translation'),
            'retry': _('retries'),
            'context': _('summaries and glossary'),
            'author': _('author brief'),
            'other': _('other'),
        }
        rows = []
        total = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                 'cost': 0.0, 'estimated': 0, 'seconds': 0.0}
        for kind in self.USAGE_KINDS:
            entry = self.usage.get(kind)
            if not entry or not entry.get('requests'):
                continue
            rows.append((kinds[kind], entry))
            for key in total:
                total[key] += entry.get(key, 0) or 0
        chars = self.text_stats.get('chars', 0)
        providers = []
        advice = []
        for name, stats in sorted(
                self.provider_stats.items(),
                key=lambda item: -item[1].get('requests', 0)):
            failures = stats.get('failures', 0)
            requests = max(1, stats.get('requests', 0))
            bad = stats.get('bad_chars', 0)
            own = stats.get('chars', 0)
            providers.append({
                'name': name,
                'requests': stats.get('requests', 0),
                'failures': failures,
                'share': self._share(bad, own),
                'excluded': bool(stats.get('excluded')),
            })
            if failures and (
                    failures >= 3
                    or (own and bad >= own * self.ADVICE_BAD_SHARE)):
                advice.append(_(
                    '{name} gave {failures} unreliable replies out of '
                    '{requests} with this model, and {share} of the text '
                    'it was sent had to be set aside, moved or asked for '
                    'again. Exclude it in the engine settings '
                    '(OpenRouter: provider ignore) or pick another '
                    'model.').format(
                        name=name, failures=failures, requests=requests,
                        share=self._share(bad, own)))
        if self.realigned:
            advice.append(_(
                '{count} translation(s) came back under the wrong number '
                'and were moved back under their paragraph. The model '
                'loses count at this chunk size: a lower "paragraphs per '
                'chunk" would help, and the log says which provider '
                'served those replies.').format(count=self.realigned))
        return {
            'rows': rows,
            'total': total,
            'run_seconds': self.run_seconds + self._run_elapsed(),
            'chars': chars,
            'bad_chars': self.text_stats.get('bad_chars', 0),
            'providers': providers,
            'advice': advice,
        }

    def build_report(self):
        """The report on the book so far as plain text, for the log:
        what the requests cost and took, per kind and in total; how much
        of the text gave trouble; how each provider behaved; what to do
        about it."""
        data = self.report_data()
        lines = [_('Requests and tokens, this book, every run so far')]
        rows = data['rows']
        if not rows:
            lines.append('  ' + _('(no request yet)'))
        else:
            named = rows + [(_('total'), data['total'])]
            width = max(len(name) for name, _entry in named)
            lines.append('  %-*s %10s %12s %12s %11s %10s' % (
                width, '', _('requests'), _('tokens in'), _('tokens out'),
                _('cost'), _('time')))
            for name, entry in named:
                lines.append('  %-*s %10d %12d %12d %11s %10s' % (
                    width, name, entry['requests'], entry['prompt_tokens'],
                    entry['completion_tokens'],
                    '$' + _money(entry['cost']) if entry['cost'] else '-',
                    self._duration(entry.get('seconds', 0))))
            if data['total']['estimated']:
                lines.append('  ' + _(
                    'Tokens of {} request(s) are estimated: the provider '
                    'did not report them.').format(
                        data['total']['estimated']))
        lines.append('  ' + _('Total time of the runs: {}').format(
            self._duration(data['run_seconds'])))
        lines.append('  ' + _(
            'Text sent for translation: {chars} characters, of which '
            '{share} had to be set aside, moved or asked for '
            'again.').format(
                chars=data['chars'],
                share=self._share(data['bad_chars'], data['chars'])))
        lines.append('')
        lines.append(_('Providers'))
        if not data['providers']:
            lines.append('  ' + _('(none named by the engine)'))
        for provider in data['providers']:
            line = _(
                '{name}: {requests} replies, {failures} unreliable, '
                '{share} of its text set aside or moved').format(**provider)
            if provider['excluded']:
                line += ' ' + _('(excluded from the current run)')
            lines.append('  ' + line)
        lines.append('')
        lines.append(_('Advice'))
        if data['advice']:
            lines.extend('  - ' + line for line in data['advice'])
        else:
            lines.append('  ' + _('Nothing to report: every provider '
                                  'answered reliably so far.'))
        return '\n'.join(lines)

    def build_report_html(self):
        """The same report as :meth:`build_report`, as HTML for the
        Report tab: a real table with the numbers right-aligned, which
        a proportional font cannot be trusted to line up."""
        from html import escape
        data = self.report_data()
        out = ['<h3>%s</h3>' % escape(
            _('Requests and tokens, this book, every run so far'))]
        rows = data['rows']
        if not rows:
            out.append('<p>%s</p>' % escape(_('(no request yet)')))
        else:
            out.append(
                '<table cellpadding="4" cellspacing="0" border="0">'
                '<tr><th align="left"></th>'
                + ''.join(
                    '<th align="right">%s</th>' % escape(title)
                    for title in (
                        _('requests'), _('tokens in'), _('tokens out'),
                        _('cost'), _('time')))
                + '</tr>')
            named = rows + [(_('total'), data['total'])]
            for i, (name, entry) in enumerate(named):
                bold = i == len(named) - 1
                cells = [
                    str(entry['requests']),
                    '{:,}'.format(entry['prompt_tokens']),
                    '{:,}'.format(entry['completion_tokens']),
                    '$' + _money(entry['cost']) if entry['cost'] else '-',
                    self._duration(entry.get('seconds', 0))]
                out.append(
                    '<tr><td align="left">%s</td>' % (
                        '<b>%s</b>' % escape(name) if bold
                        else escape(name))
                    + ''.join(
                        '<td align="right">%s</td>' % (
                            '<b>%s</b>' % escape(cell) if bold
                            else escape(cell))
                        for cell in cells)
                    + '</tr>')
            out.append('</table>')
            if data['total']['estimated']:
                out.append('<p>%s</p>' % escape(_(
                    'Tokens of {} request(s) are estimated: the provider '
                    'did not report them.').format(
                        data['total']['estimated'])))
        out.append('<p>%s<br>%s</p>' % (
            escape(_('Total time of the runs: {}').format(
                self._duration(data['run_seconds']))),
            escape(_(
                'Text sent for translation: {chars} characters, of which '
                '{share} had to be set aside, moved or asked for '
                'again.').format(
                    chars='{:,}'.format(data['chars']),
                    share=self._share(data['bad_chars'], data['chars'])))))
        out.append('<h3>%s</h3>' % escape(_('Providers')))
        if not data['providers']:
            out.append('<p>%s</p>' % escape(_('(none named by the engine)')))
        else:
            out.append(
                '<table cellpadding="4" cellspacing="0" border="0">'
                '<tr><th align="left">%s</th><th align="right">%s</th>'
                '<th align="right">%s</th><th align="right">%s</th>'
                '<th align="left"></th></tr>' % tuple(
                    escape(title) for title in (
                        _('provider'), _('replies'), _('unreliable'),
                        _('text set aside or moved'))))
            for provider in data['providers']:
                out.append(
                    '<tr><td align="left">%s</td><td align="right">%d</td>'
                    '<td align="right">%d</td><td align="right">%s</td>'
                    '<td align="left">%s</td></tr>' % (
                        escape(provider['name']), provider['requests'],
                        provider['failures'], escape(provider['share']),
                        escape(_('excluded from the current run'))
                        if provider['excluded'] else ''))
            out.append('</table>')
        out.append('<h3>%s</h3>' % escape(_('Advice')))
        if data['advice']:
            out.append('<ul>%s</ul>' % ''.join(
                '<li>%s</li>' % escape(line) for line in data['advice']))
        else:
            out.append('<p>%s</p>' % escape(_(
                'Nothing to report: every provider answered reliably so '
                'far.')))
        return '\n'.join(out)

    def _log_reply_coverage(self, response, label, expected, parsed):
        """Say how much of what was asked a reply covered when it did
        not cover it all, with its first characters: the log otherwise
        shows a small reply and nothing of what the model wrote
        instead."""
        if len(parsed) >= len(expected):
            return
        if len(expected) >= 4 and len(parsed) * 2 < len(expected):
            self._note_provider_failure(_(
                'a reply covering {} of {} paragraphs').format(
                    len(parsed), len(expected)))
        line = _(
            '{label}: the reply covered {found} of {asked} paragraphs '
            '(numbers kept: {numbers}).').format(
                label=label, found=len(parsed), asked=len(expected),
                numbers=_ranges(parsed) or _('none'))
        generation = self._reply_fact('last_generation_id')
        if generation:
            # What to look the reply up by in the gateway's own records.
            line += ' ' + _('Generation id: {}.').format(generation)
        excerpt = self.log_reply_excerpt
        if excerpt and response:
            head = ' '.join(str(response).split())
            if len(head) > excerpt:
                head = head[:excerpt] + '…'
            line += ' ' + _('It begins: {}').format(head)
        self.log(line)

    def _check_reply(self, chunk_paragraphs, parsed, accepted, doubted,
                     label, expected=None):
        """Screen a reply (see :meth:`_screen_reply`) and add it to the
        share of the text that gave trouble, ``expected`` being the
        numbers the request asked for."""
        sources = {
            i: (p.original or '')
            for i, p in enumerate(chunk_paragraphs, start=1)}
        moved = self._screen_reply(sources, parsed, accepted, doubted, label)
        if expected is not None:
            self._audit_reply(sources, expected, parsed, moved)

    def _screen_reply(self, sources, parsed, accepted, doubted, label):
        """Drop from ``parsed`` the translations that cannot be of the
        paragraph they came back under, so that they are asked for again
        with the missing ones. Returns the numbers that received a
        translation moved from another number.

        ``accepted`` holds what earlier replies of the chunk already
        gave; ``doubted`` the numbers set aside for a soft reason so
        far. A reply that is wrong in many places is a shifted one, and
        nothing in it is trusted. A doubt about one paragraph of an
        otherwise sound reply, asked for on its own and answered the
        same way, is the paragraph -- an unusual one -- and not the
        reply, and it is kept the second time.
        """
        moved = set()
        if not self.verify_alignment or not parsed:
            return moved
        flagged = suspicious_translations(sources, parsed, accepted)
        if not flagged:
            return moved
        if self.realign_shifted_replies and len(parsed) >= 4:
            realigned, moves = realign_shifted(sources, parsed)
            if moves:
                moved = {to for _number, to in moves}
                first = min(number for number, _to in moves)
                furthest = max(to - number for number, to in moves)
                left_out = len(parsed) - len(realigned)
                self.log(_(
                    '{label}: the reply numbers slipped from paragraph '
                    '{first} on; {count} translation(s) moved back under '
                    'their paragraph, by up to {furthest} place(s), and '
                    '{left} left out. The model or the provider is not '
                    'reliable at this size.').format(
                        label=label, first=first, count=len(moves),
                        furthest=furthest, left=left_out), True)
                with self._lock:
                    self._root.realigned += len(moves)
                self._note_provider_failure(_('slipped numbers'))
                parsed.clear()
                parsed.update(realigned)
                flagged = suspicious_translations(sources, parsed, accepted)
                if not flagged:
                    return moved
        systemic = len(parsed) >= 4 and len(flagged) * 3 > len(parsed)
        if systemic:
            self._note_provider_failure(_(
                'a reply wrong in {} of {} paragraphs').format(
                    len(flagged), len(parsed)))
        rejected = {}
        for number, (reason, hard) in flagged.items():
            if not hard and number in doubted and not systemic:
                continue
            rejected[number] = reason
            if not hard:
                doubted.add(number)
        # What was set aside, kept for the log: once asked for again,
        # the retry's answer is all the cache will ever show.
        set_aside = {number: parsed[number] for number in rejected}
        for number in rejected:
            del parsed[number]
        if not rejected:
            return moved
        by_reason = {}
        for number, reason in rejected.items():
            by_reason.setdefault(reason, []).append(number)
        self.log(_(
            '{label}: {count} translation(s) set aside, they cannot be of '
            'their paragraph: {details}. They are asked for again.').format(
                label=label, count=len(rejected),
                details='; '.join(
                    '%s (%s)' % (VERIFICATION_REASONS[reason],
                                 _ranges(numbers))
                    for reason, numbers in by_reason.items())), True)
        for number in sorted(rejected)[:3]:
            self.log('    [%d] %s\n        -> %s' % (
                number, self._head(sources.get(number, ''), 100),
                self._head(set_aside[number], 100)))
        return moved

    def _wait(self, seconds):
        """Sleep in short steps so a cancel does not wait the whole
        pause out."""
        step = 0.5
        for _step in range(int(seconds / step)):
            if self.cancel_request():
                raise TranslationCanceled(_('Translation canceled.'))
            time.sleep(step)

    def _translate_context_call(self, system_prompt, user_prompt, label,
                                schema=None):
        """Run one auxiliary (summary / glossary) request.

        :schema: when given and the engine supports structured output,
            the reply is constrained to that JSON schema by the server
            instead of being merely asked for in the prompt.

        Unless ``novel_context_reasoning`` says otherwise, the engine's
        chain of thought is turned down for the duration of the call and
        restored afterwards. The adjustment only ever turns reasoning
        *down*: an engine that sends no reasoning field keeps sending
        none, so providers that would reject the parameter are left
        alone.
        """
        translator = self.translator
        saved = {}
        if not self.context_reasoning:
            effort = getattr(translator, 'reasoning_effort', None)
            supported = getattr(translator, 'reasoning_efforts', ())
            if effort and effort not in ('default', 'none') \
                    and 'none' in supported:
                saved['reasoning_effort'] = effort
                translator.reasoning_effort = 'none'
            try:
                budget = int(getattr(translator, 'reasoning_max_tokens', 0)
                             or 0)
            except (TypeError, ValueError):
                budget = 0
            if budget > 0:
                saved['reasoning_max_tokens'] = translator.reasoning_max_tokens
                translator.reasoning_max_tokens = 0
            if saved:
                self.log(_(
                    'Reasoning turned off for {} (novel_context_reasoning '
                    'is off).').format(label))
        cap = self.context_max_tokens
        # Asking for more than the model can write is an error on some
        # providers and a silent truncation on others.
        if cap > 0 and self.model_output_limit:
            cap = min(cap, self.model_output_limit)
        if cap > 0 and hasattr(translator, 'max_tokens'):
            try:
                current = int(getattr(translator, 'max_tokens', 0) or 0)
            except (TypeError, ValueError):
                current = 0
            # 0 means "no limit sent"; anything the user set that is
            # already tighter than the cap is left as it is.
            wanted = cap if current <= 0 else min(cap, current)
            if wanted != current:
                saved['max_tokens'] = translator.max_tokens
                translator.max_tokens = wanted
                self.log(_(
                    'Reply for {} capped at {} tokens '
                    '(novel_context_max_tokens).').format(label, wanted))
        if self._request_kind not in ('author',):
            self._request_kind = 'context'
        try:
            if schema is not None and self._structured_active():
                return self._translate_with_retry_structured(
                    system_prompt, user_prompt, schema=schema,
                    attempts=2, label=label)
            return self._translate_with_retry(
                system_prompt, user_prompt, attempts=2, label=label)
        finally:
            for key, value in saved.items():
                setattr(translator, key, value)

    # -- chunk-level translation with alignment retry ---------------------

    def _translate_chunk(self, chunk_paragraphs, context_text,
                         chapter_num, chapter_title, chunk_num, total_chunks,
                         overlap_translations=None, source_context=None):
        """Dispatcher: choose between structured (JSON) and text-marker
        translation paths depending on engine capability and user config.

        The two paths implement the same contract:

            (chunk_paragraphs, ...) -> {chunk_local_index: translation}

        The dispatcher swaps them transparently; the caller
        (:meth:`_translate_chapter`) does not need to know which one was
        used. A one-shot log line at the very first invocation records
        the chosen path so the user can verify what is happening.
        """
        with self._reply_room(chunk_paragraphs):
            if self._structured_active():
                return self._translate_chunk_structured(
                    chunk_paragraphs, context_text,
                    chapter_num, chapter_title, chunk_num, total_chunks,
                    overlap_translations=overlap_translations,
                    source_context=source_context)
            return self._translate_chunk_markers(
                chunk_paragraphs, context_text,
                chapter_num, chapter_title, chunk_num, total_chunks,
                overlap_translations=overlap_translations,
                source_context=source_context)

    def _context_block(self, overlap_translations, source_context):
        """The text around a chunk, for reading only: the source of the
        paragraphs before it, the translation of the previous chunk's
        last paragraphs when there is one, and the source of the
        paragraphs after it. Empty when there is none of that."""
        before, after = source_context or ([], [])
        parts = []
        joined = '\n\n'.join(t.strip() for t in before if t and t.strip())
        if joined:
            parts.append(
                '\n\n'
                + model_text(
                    '--- The paragraphs BEFORE the ones to translate, in '
                    'the source language (context only: do NOT translate '
                    'them, do NOT number them) ---')
                + '\n' + joined + '\n'
                + model_text('--- End of context ---'))
        joined = '\n\n'.join(
            t.strip() for t in (overlap_translations or []) if t and t.strip())
        if joined:
            parts.append(
                '\n\n'
                + model_text(
                    '--- Context from previous paragraphs '
                    '(already translated -- do NOT modify or '
                    'retranslate this section) ---')
                + '\n' + joined + '\n'
                + model_text('--- End of context ---'))
        joined = '\n\n'.join(t.strip() for t in after if t and t.strip())
        if joined:
            parts.append(
                '\n\n'
                + model_text(
                    '--- The paragraphs AFTER the ones to translate, in '
                    'the source language (context only: do NOT translate '
                    'them, do NOT number them) ---')
                + '\n' + joined + '\n'
                + model_text('--- End of context ---'))
        return ''.join(parts)

    def _translate_chunk_markers(self, chunk_paragraphs, context_text,
                                 chapter_num, chapter_title, chunk_num,
                                 total_chunks, overlap_translations=None,
                                 source_context=None):
        """Translate one chunk of paragraphs, returning a dict
        ``{paragraph_index: translation}`` covering all non-ignored
        paragraphs. If the LLM misses some indices, up to 2 alignment
        retries are attempted before giving up (missing translations end
        up as ``None`` and the caller may re-run with a smaller chunk).

        :overlap_translations: optional list of already-translated
            paragraph texts from the immediately preceding chunk. When
            provided, they are shown to the LLM as narrative context
            (do NOT retranslate) so it can preserve pronouns, dialogue
            threads and stylistic continuity across chunk boundaries.
        """
        tagged, indices = tag_paragraphs(chunk_paragraphs)
        if not indices:
            return {}

        # System prompt: role + languages + narrative context (summary +
        # glossary). Static per-chapter -- no formatting rules here.
        system_prompt = self._translation_system_prompt(context_text)

        # Build the user message. Order matters for the provider's prompt
        # cache: what never changes goes first -- the system prompt is
        # the same for every chunk of a chapter -- then the header, the
        # optional overlap block (already translated, for reading only)
        # and last the tagged source paragraphs.
        header = model_text('Chapter {n}: "{title}" (chunk {c}/{t})').format(
            n=chapter_num, title=chapter_title,
            c=chunk_num, t=total_chunks)

        overlap_block = self._context_block(
            overlap_translations, source_context)

        user_text = '%s\n\n%s%s\n\n%s' % (
            self._fill_placeholders(DEFAULT_NOVEL_FORMAT_RULES),
            header, overlap_block,
            self._fill_placeholders(
                DEFAULT_NOVEL_FORMAT_SOURCE, extra={'{text}': tagged}))

        chunk_label = _('chapter {} chunk {}/{}').format(
            chapter_num, chunk_num, total_chunks)
        self._request_kind = 'translation'
        response = self._translate_with_retry(
            system_prompt, user_text, label=chunk_label)
        parsed = parse_tagged_response(response, indices)
        self._log_reply_coverage(response, chunk_label, indices, parsed)
        doubted = set()
        self._check_reply(
            chunk_paragraphs, parsed, {}, doubted, chunk_label, indices)
        missing = [i for i in indices if i not in parsed]

        # Alignment retries: only ask for the missing paragraphs so the LLM
        # doesn't waste tokens re-translating the ones we already have.
        # The retry request omits the overlap block: the model has already
        # seen the surrounding context in the initial call, and repeating
        # it would just eat into the retry budget.
        for retry in range(2):
            if not missing:
                break
            batches = self._retry_batches(missing)
            self.log(
                _('Alignment retry {}: {} missing markers{}.').format(
                    retry + 1, len(missing), self._batches_note(batches)))
            self._request_kind = 'retry'
            for batch in batches:
                fixup_paras = [chunk_paragraphs[i - 1] for i in batch]
                # Re-tag using the original indices so downstream logic
                # stays consistent.
                fixup_tagged = self._retag(fixup_paras, batch)
                fixup_body = self._fill_placeholders(
                    model_text(
                        'The previous response was incomplete. Translate '
                        'only the numbered paragraphs below, every one of '
                        'them in full, keeping the exact same [N] markers '
                        'with the same numbers as given here -- whether '
                        'or not they start at 1 -- and in the same order. '
                        'Reply with the numbered paragraphs only.\n\n'
                        'Source paragraphs:\n\n{text}'),
                    extra={'{text}': fixup_tagged})
                retry_label = _('{}, alignment retry {}').format(
                    chunk_label, retry + 1)
                response = self._translate_with_retry(
                    system_prompt, fixup_body, label=retry_label)
                fixup_parsed = parse_tagged_response(response, batch)
                self._log_reply_coverage(
                    response, retry_label, batch, fixup_parsed)
                self._check_reply(
                    chunk_paragraphs, fixup_parsed, parsed, doubted,
                    retry_label, batch)
                parsed.update(fixup_parsed)
            missing = [i for i in indices if i not in parsed]

        if missing:
            self.log(
                _('Warning: {} paragraph(s) missing after retries: {}').format(
                    len(missing), missing), True)
        return parsed

    def _retry_batches(self, missing):
        """How a retry asks for ``missing``: all at once, or in two
        halves when there are more than a handful and the setting says
        so (see :attr:`retry_split`)."""
        missing = list(missing)
        if not self.retry_split or len(missing) <= 4:
            return [missing]
        half = (len(missing) + 1) // 2
        return [missing[:half], missing[half:]]

    @staticmethod
    def _batches_note(batches):
        if len(batches) <= 1:
            return ''
        return _(', asked for in {} requests').format(len(batches))

    def _retag(self, paragraphs, indices):
        """Like ``tag_paragraphs`` but forces the marker numbers to be
        exactly ``indices`` (skipping ignored paragraphs). Uses the same
        ``[N]\\ntext`` format as :func:`tag_paragraphs`.
        """
        assert len(paragraphs) == len(indices)
        parts = []
        for p, idx in zip(paragraphs, indices):
            if getattr(p, 'ignored', False):
                continue
            text = (p.original or '').strip()
            parts.append('[%d]\n%s' % (idx, text))
        return '\n\n'.join(parts)

    # -- structured (JSON) output path -------------------------------------

    #: JSON Schema describing the response shape for the structured path.
    #: Kept as a class attribute so it can be reused as a stable reference
    #: across chunks (the value is constant per translation session).
    _STRUCTURED_RESPONSE_SCHEMA = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'paragraphs': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'properties': {
                        'n': {'type': 'integer'},
                        'translation': {'type': 'string'},
                    },
                    'required': ['n', 'translation'],
                },
            },
        },
        'required': ['paragraphs'],
    }

    # Same idea as the translation schema, for the entity extraction
    # call: a server that enforces the shape cannot answer with prose,
    # cannot preface the JSON, and cannot drift into re-listing the
    # names it was told to skip until it exhausts its output limit.
    _GLOSSARY_RESPONSE_SCHEMA = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'entities': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'properties': {
                        'source': {'type': 'string'},
                        'translation': {'type': 'string'},
                        'type': {'type': 'string'},
                        'notes': {'type': 'string'},
                    },
                    'required': [
                        'source', 'translation', 'type', 'notes'],
                },
            },
        },
        'required': ['entities'],
    }

    # The combined summary + glossary reply. "summary" is declared
    # first so a model that follows the schema order writes it before
    # the entity list: if the answer is cut short, the field that cannot
    # be salvaged from a broken object is the one already finished.
    _AUTHOR_CHECK_SCHEMA = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'recognised': {'type': 'boolean'},
            'works': {'type': 'array', 'items': {'type': 'string'}},
            'shared_name': {'type': 'boolean'},
            'note': {'type': 'string'},
        },
        'required': ['recognised', 'works', 'shared_name', 'note'],
    }

    _CONTEXT_RESPONSE_SCHEMA = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'narrative': {'type': 'boolean'},
            'summary': {'type': 'string'},
            'entities': _GLOSSARY_RESPONSE_SCHEMA['properties']['entities'],
        },
        'required': ['narrative', 'summary', 'entities'],
    }

    def _build_structured_payload(self, chunk_paragraphs, indices):
        """Return the JSON payload with source paragraphs to translate.

        The reply must carry ``n`` and ``translation`` only. Echoing
        ``source`` back doubles the billed output tokens for nothing --
        ``n`` is what aligns a translation with its paragraph -- and on a
        full chapter it is what pushes the answer past the model's output
        limit, truncating the JSON mid-object.
        """
        return {
            'paragraphs': [
                {
                    'n': i,
                    'source': (chunk_paragraphs[i - 1].original or '').strip(),
                }
                for i in indices
            ],
        }

    def _parse_structured_response(self, response, expected_indices):
        """Parse a JSON response and return ``{index: translation}``.

        Robust to the common LLM sloppiness:
          * responses wrapped in prose or Markdown fences (delegated to
            :func:`_extract_json_object`);
          * extra fields inside each paragraph object;
          * ``n`` values as strings instead of integers;
          * missing paragraphs (caller handles via alignment retry);
          * hallucinated indices not in ``expected_indices``;
          * a reply cut off at the model's output limit, whose braces
            never balance: the entries that did complete are salvaged
            (see :func:`_iter_json_objects`) so only the tail is retried.
        """
        if not response:
            return {}
        obj = _extract_json_object(response)
        if obj and isinstance(obj.get('paragraphs'), list):
            items = obj['paragraphs']
            truncated = False
        else:
            # The reply never closed its braces (almost always an answer
            # cut off at the model's output limit). Recover the entries
            # that did complete instead of discarding the whole chunk.
            items = list(_iter_json_objects(response))
            truncated = True

        expected = set(expected_indices)
        found = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_n = item.get('n')
            try:
                n = int(raw_n)
            except (TypeError, ValueError):
                continue
            translation = (item.get('translation') or '').strip()
            if translation:
                found[n] = translation
        result = {n: t for n, t in found.items() if n in expected}
        if not result:
            renumbered = _renumbered(found, list(expected_indices))
            if renumbered:
                self.log(_(
                    'The reply numbered its paragraphs from 1 instead of '
                    'keeping the numbers it was given; matched them by '
                    'order.'))
                result = renumbered

        if truncated:
            missing = sorted(expected - set(result))
            self.log(_(
                'Incomplete JSON reply ({} chars): recovered {}/{} '
                'paragraph(s), missing {}. They are requested again '
                'right away, which costs one small extra call. Only if '
                'whole chapters keep coming back short is the model '
                'hitting its output limit -- lower the '
                'paragraphs-per-chunk cap then.').format(
                    len(response), len(result), len(expected),
                    missing[:10]))
        return result

    @contextmanager
    def _body_builder(self, build):
        """Point the engine's ``get_body`` at ``build`` for the duration
        of one request, and put the original back afterwards.

        ``build`` receives the request text and returns the body. It runs
        with the *plain* builder back in place, because every alternative
        body is built by taking the normal one apart and adding to it --
        ``get_body_for_structured`` on Gemini, say -- and leaving the
        swap in place while it runs makes it call itself until the stack
        ends.

        Swapping rather than threading a flag through
        :meth:`_translate_with_retry` keeps the retry, backoff and cancel
        logic in one place, and leaves the engines with one method to
        write per alternative body.
        """
        translator = self.translator
        original = translator.get_body

        def swapped(text):
            translator.get_body = original
            try:
                return build(text)
            finally:
                translator.get_body = swapped

        translator.get_body = swapped
        try:
            yield
        finally:
            translator.get_body = original

    def _translate_with_retry_structured(self, system_prompt, user_text,
                                         schema=None, attempts=None,
                                         label=None):
        """Same retry loop as :meth:`_translate_with_retry` but the
        request body is built via ``engine.get_body_for_structured`` so
        the server enforces the JSON response.

        The structured body includes ``stream: true`` so that long
        responses (80+ translated paragraphs) are delivered token by
        token without triggering the client-side request timeout.
        Because the engine's ``_parse_stream`` returns a generator, we
        must reassemble all chunks into a single string here before the
        caller tries to parse the JSON.

        Additionally, the engine's ``request_timeout`` is temporarily
        raised to at least 300 seconds as a safety net: with streaming
        the timeout only governs the connection and the first byte, so
        300 s is never reached in practice, but it protects against edge
        cases where the server is slow to start streaming.

        TCP keepalive is also enabled for the duration of the structured
        call (``request_keepalive = True``). This prevents intermediate
        NAT devices / stateful firewalls (typical home routers, corporate
        proxies) from dropping the connection during the time the LLM
        spends generating the first token. Without keepalive we observed
        Ollama's ``srv stop: cancel task`` at exactly the router NAT
        session timeout (~30s).

        The request body is swapped in through :meth:`_body_builder`.
        """
        from types import GeneratorType

        translator = self.translator
        original_timeout = getattr(translator, 'request_timeout', None)
        original_keepalive = getattr(translator, 'request_keepalive', False)
        original_stream = getattr(translator, 'stream', False)
        # Raise timeout defensively; with streaming this only covers the
        # time until the first token arrives, not the total generation.
        min_structured_timeout = 300.0
        if original_timeout is not None \
                and original_timeout < min_structured_timeout:
            translator.request_timeout = min_structured_timeout
        # Enable TCP keepalive on the underlying socket. The structured
        # path may spend tens of seconds waiting for the first streamed
        # token from the LLM; without keepalive, intermediate NAT devices
        # (routers, corporate firewalls) silently drop the connection
        # after ~30s of idle, causing the Ollama-side "cancel task" we
        # observed in the logs.
        translator.request_keepalive = True
        # The structured body always asks for `stream: true`, so the engine
        # must read the response as a stream too. Without this an engine
        # configured with streaming disabled would try to json.loads() a
        # raw SSE payload.
        translator.stream = True

        def structured_get_body(text):
            return translator.get_body_for_structured(text, schema)

        try:
            with self._body_builder(structured_get_body):
                result = self._translate_with_retry(
                    system_prompt, user_text, attempts=attempts, label=label)
        finally:
            if original_timeout is not None:
                translator.request_timeout = original_timeout
            translator.request_keepalive = original_keepalive
            translator.stream = original_stream

        # If the engine returned a streaming generator (because stream:true
        # is set in the body), collect all chunks now so the JSON parser
        # receives the complete response string.
        if isinstance(result, GeneratorType):
            result = ''.join(chunk for chunk in result)

        return result or ''

    def _translate_chunk_structured(self, chunk_paragraphs, context_text,
                                    chapter_num, chapter_title, chunk_num,
                                    total_chunks, overlap_translations=None,
                                    source_context=None):
        """Translate one chunk via the engine's native JSON output.

        Contract identical to :meth:`_translate_chunk_markers` -- returns
        ``{chunk_local_index: translation}`` covering all non-ignored
        paragraphs. Alignment retries call the *marker* path as a safety
        net so we always converge on some usable output even when the
        JSON server rejects our schema.
        """
        indices = [
            i for i, p in enumerate(chunk_paragraphs, start=1)
            if not getattr(p, 'ignored', False)
        ]
        if not indices:
            return {}

        # System prompt: role + languages + narrative context (summary +
        # glossary). Same as the marker path.
        system_prompt = self._translation_system_prompt(context_text)

        # User message: header + optional overlap block + JSON schema
        # instructions + serialized payload.
        header = model_text('Chapter {n}: "{title}" (chunk {c}/{t})').format(
            n=chapter_num, title=chapter_title,
            c=chunk_num, t=total_chunks)

        overlap_block = self._context_block(
            overlap_translations, source_context)

        payload = self._build_structured_payload(chunk_paragraphs, indices)
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

        instructions = model_text(
            'You will receive a JSON object listing numbered source '
            'paragraphs. Reply with a JSON object holding one entry per '
            'paragraph, in this exact shape:\n'
            '{"paragraphs": [{"n": 1, "translation": "..."}, '
            '{"n": 2, "translation": "..."}]}\n'
            'Each entry carries exactly two fields: "n", copied verbatim '
            'from the input, and "translation", your translation of that '
            'paragraph\'s "source". Do NOT repeat the "source" text and '
            'do not add any other field: the number is what pairs a '
            'translation with its paragraph, so the "n" of a translation '
            'is always the "n" of the entry it translates, never a count '
            'of your own. Translate every entry in full, however short or '
            'long: never shorten one or stand in for part of it with an '
            'ellipsis such as "[…]". Preserve any inline placeholder like '
            '{id_XXXXX}. Do not add, drop, or renumber paragraphs. Return '
            'ONLY the JSON object, no preamble.')

        # Order matters for the provider's prompt cache: the system
        # prompt is identical for every chunk of a chapter and these
        # instructions are identical for the whole book, so both sit
        # before anything that changes from one request to the next
        # (the chunk number in the header, the overlap, the payload).
        user_text = (
            '%s\n\n%s%s\n\nInput:\n%s'
            % (instructions, header, overlap_block, payload_json))

        chunk_label = _('chapter {} chunk {}/{}').format(
            chapter_num, chunk_num, total_chunks)
        self._request_kind = 'translation'
        response = self._translate_with_retry_structured(
            system_prompt, user_text,
            schema=self._STRUCTURED_RESPONSE_SCHEMA, label=chunk_label)
        parsed = self._parse_structured_response(response, indices)
        self._log_reply_coverage(response, chunk_label, indices, parsed)
        doubted = set()
        self._check_reply(
            chunk_paragraphs, parsed, {}, doubted, chunk_label, indices)
        missing = [i for i in indices if i not in parsed]

        # Alignment retries (structured): ask only for the missing
        # paragraphs, still in JSON form. If two retries still fail, fall
        # back to the marker path for the remaining ones (belt and
        # suspenders).
        for retry in range(2):
            if not missing:
                break
            batches = self._retry_batches(missing)
            self.log(
                _('Structured retry {}: {} missing entries{}.').format(
                    retry + 1, len(missing), self._batches_note(batches)))
            self._request_kind = 'retry'
            for fixup_indices in batches:
                fixup_payload = self._build_structured_payload(
                    chunk_paragraphs, fixup_indices)
                fixup_json = json.dumps(
                    fixup_payload, ensure_ascii=False, indent=2)
                fixup_body = (
                    model_text(
                        'The previous JSON response was incomplete. Reply '
                        'with a JSON object translating ONLY the '
                        'paragraphs below, every one of them in full. Same '
                        'shape as before: "paragraphs" array of {"n": int, '
                        '"translation": string}, with no "source" field '
                        'and nothing else. Copy each "n" from the input as '
                        'it is, whether or not the numbers start at 1. '
                        'Return JSON only.')
                    + '\n\nInput:\n' + fixup_json)
                retry_label = _('{}, JSON retry {}').format(
                    chunk_label, retry + 1)
                response = self._translate_with_retry_structured(
                    system_prompt, fixup_body,
                    schema=self._STRUCTURED_RESPONSE_SCHEMA,
                    label=retry_label)
                fixup_parsed = self._parse_structured_response(
                    response, fixup_indices)
                self._log_reply_coverage(
                    response, retry_label, fixup_indices, fixup_parsed)
                self._check_reply(
                    chunk_paragraphs, fixup_parsed, parsed, doubted,
                    retry_label, fixup_indices)
                parsed.update(fixup_parsed)
            missing = [i for i in indices if i not in parsed]

        if missing:
            # Last-resort fallback: try the marker path for the leftover
            # paragraphs. This handles the corner case where the server
            # refuses the JSON schema on a specific input (rare).
            self.log(
                _('Structured path could not resolve {} paragraph(s); '
                  'falling back to text markers for those.').format(
                      len(missing)))
            fallback_paras = [
                chunk_paragraphs[i - 1] for i in missing]
            fallback_result = self._translate_chunk_markers(
                fallback_paras, context_text,
                chapter_num, chapter_title, chunk_num, total_chunks,
                overlap_translations=overlap_translations,
                source_context=source_context)
            # Map back to the original chunk-local indices.
            for local_i, i in enumerate(missing, start=1):
                if local_i in fallback_result:
                    parsed[i] = fallback_result[local_i]
            missing = [i for i in indices if i not in parsed]

        if missing:
            self.log(
                _('Warning: {} paragraph(s) missing after all retries: {}')
                .format(len(missing), missing), True)
        return parsed

    # -- summary / glossary extraction -------------------------------------

    def _build_translated_chapter_text(self, paragraphs, translations):
        parts = []
        for i, p in enumerate(paragraphs, start=1):
            if getattr(p, 'ignored', False):
                continue
            t = translations.get(i)
            if t:
                parts.append(t)
        return '\n\n'.join(parts)

    def _build_source_chapter_text(self, paragraphs):
        parts = []
        for p in paragraphs:
            if getattr(p, 'ignored', False):
                continue
            text = (p.original or '').strip()
            if text:
                parts.append(text)
        return '\n\n'.join(parts)

    def _head(self, text, max_chars):
        """Truncate ``text`` to at most ``max_chars``, keeping the head.

        Used for the summary and glossary prompts. The opening paragraphs
        of a chapter reliably introduce its characters, setting and plot
        thread, which is what a useful summary needs.

        If the text already fits within ``max_chars`` it is returned verbatim.
        If ``max_chars`` <= 0, truncation is disabled.
        """
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[:max_chars]

    def _summary_input_budget_chars(self):
        """Max characters to feed the summary/glossary LLM calls.

        Rule of thumb: keep the input under ~4x the token budget reserved
        for these auxiliary calls, so the model has room for its own
        response. Hardcoded to a sensible default; the ``novel_summary_
        input_max_chars`` config key can override.
        """
        default = 40000  # ~10000 words in Latin text
        return int(self._cfg('novel_summary_input_max_chars', default))

    def _generate_summary(self, chapter, translated_text):
        if not translated_text.strip():
            return ''
        max_chars = self._summary_input_budget_chars()
        clipped = self._head(translated_text, max_chars)
        if len(clipped) < len(translated_text):
            self.log(_(
                'Summary input truncated: {} -> {} chars.').format(
                    len(translated_text), len(clipped)))
        system_prompt = self._fill_placeholders(model_text(
            'You are a helpful assistant that produces concise '
            'summaries.'))
        user_prompt = self._compose_prompt(
            self.summary_prompt,
            {
                '{chapter_num}': (None, str(chapter.index)),
                '{chapter_title}': (None, chapter.title or ''),
                '{text}': (model_text('Chapter text:'), clipped),
            },
            required=('{text}',))
        try:
            response = self._translate_context_call(
                system_prompt, user_prompt,
                _('summary of chapter {}').format(chapter.index))
        except TranslationFailed as e:
            self.log(_('Summary generation failed: {}').format(e), True)
            return ''
        summary = response.strip()
        condensed = re.sub(r'[^A-Z ]', '', summary.upper()).strip()
        if condensed.startswith(NOT_A_STORY_CHAPTER) \
                and self.context_narrative_only:
            self._discard_context(chapter, '')
            return ''
        return self._clip_summary(summary, chapter)

    def _clip_summary(self, summary, chapter):
        """Keep a runaway summary out of the stored context.

        See :attr:`summary_max_chars`. The cut is moved back to the last
        sentence end so what is stored still reads as prose.
        """
        limit = self.summary_max_chars
        if not limit or len(summary) <= limit:
            return summary
        clipped = summary[:limit]
        stop = max(clipped.rfind('. '), clipped.rfind('.\n'))
        if stop > limit // 2:
            clipped = clipped[:stop + 1]
        self.log(_(
            'Summary of chapter {} came back {} chars long, well past the '
            '{} it was asked for; stored the first {} chars so it does '
            'not crowd out the context of the chapters that '
            'follow.').format(
                chapter.index, len(summary), limit, len(clipped)), True)
        return clipped.strip()

    def _extract_glossary_updates(self, chapter, source_text, translated_text):
        if not source_text.strip() or not translated_text.strip():
            return []
        max_chars = self._summary_input_budget_chars()
        src_clipped = self._head(source_text, max_chars)
        tgt_clipped = self._head(translated_text, max_chars)
        known, existing_keys = self._glossary_prompt_keys(
            chapter, '%s\n%s' % (src_clipped, tgt_clipped))
        system_prompt = self._fill_placeholders(model_text(
            'You are a helpful assistant. Answer with strict JSON '
            'only.'))
        user_prompt = self._compose_prompt(
            self.glossary_prompt,
            {
                '{existing_keys}': (
                    model_text('Already known (skip these):'),
                    existing_keys),
                '{max_entities}': (
                    None, str(self.glossary_chapter_max_entries)),
                '{source_text}': (model_text('Source:'), src_clipped),
                '{translated_text}': (
                    model_text('Translation:'), tgt_clipped),
            },
            required=(
                '{existing_keys}', '{source_text}', '{translated_text}'))
        try:
            response = self._translate_context_call(
                system_prompt, user_prompt,
                _('glossary of chapter {}').format(chapter.index),
                schema=self._glossary_schema())
        except TranslationFailed as e:
            self.log(
                _('Glossary extraction failed: {}').format(e), True)
            return []

        return self._new_entities(
            self._parse_entities(response, obj=None), known, chapter)

    def _parse_entities(self, response, obj=None):
        """Return the entities of a reply, deduplicated.

        :obj: the already decoded JSON object, when the caller has one.
            Otherwise the reply is decoded here, and if it never closed
            its braces the entries that did complete are salvaged from
            it rather than the whole answer being thrown away.
        """
        entities = []
        seen = set()

        def collect(items):
            for item in items:
                if not isinstance(item, dict):
                    continue
                src = (item.get('source') or '').strip()
                tgt = (item.get('translation') or '').strip()
                # A model that loops repeats the same entry over and
                # over; the first spelling of each name is enough.
                if not src or not tgt or src in seen:
                    continue
                seen.add(src)
                entities.append({
                    'source': src,
                    'translation': tgt,
                    'type': (item.get('type') or '').strip() or 'other',
                    'notes': (item.get('notes') or '').strip(),
                })

        # Path 1: JSON parsing (preferred).
        if obj is None:
            obj = _extract_json_object(response)
        if obj and isinstance(obj.get('entities'), list):
            collect(obj['entities'])
        else:
            # Path 1b: a reply the output cap cut off mid-object never
            # closes its outermost brace, so the whole answer would be
            # thrown away. Keep the entries that did complete.
            salvaged = [
                item for item in _iter_json_objects(response)
                if item.get('source') and item.get('translation')]
            if salvaged:
                self.log(_(
                    'Glossary extraction: reply not terminated, recovered '
                    '{} complete entries.').format(len(salvaged)))
                collect(salvaged)

        # Path 2: line-based regex fallback if JSON gave us nothing.
        if not entities:
            fallback = _extract_entities_fallback(response)
            if fallback:
                self.log(
                    _('Glossary extraction: JSON not usable, recovered '
                      '{} entries via fallback parser.').format(
                          len(fallback)))
                entities = fallback
            else:
                self.log(
                    _('Glossary extraction: could not parse JSON, '
                      'skipping.'), True)
        return entities

    def _new_entities(self, entities, known, chapter):
        """Drop the entries already in the glossary (LLMs repeat them
        despite being told not to) and report what was dropped."""
        existing = set(known)
        filtered = [e for e in entities if e['source'] not in existing]
        already_known = len(entities) - len(filtered)
        if already_known:
            self.log(_(
                'Glossary: {} of the {} entries returned for chapter {} '
                'were already known and were dropped.').format(
                    already_known, len(entities), chapter.index))
        limit = self.glossary_chapter_max_entries
        if len(filtered) > limit:
            self.log(_(
                'Glossary: the model listed {} new entries for chapter {} '
                'against a limit of {}; the first {} are kept '
                '(novel_glossary_chapter_max_entries).').format(
                    len(filtered), chapter.index, limit, limit))
            filtered = filtered[:limit]
        if filtered:
            self.log(_('Glossary: +{} new entries (chapter {}).').format(
                len(filtered), chapter.index))
        return filtered

    def _glossary_prompt_keys(self, chapter, text):
        """Return the '(skip these)' list for an extraction prompt."""
        known = self.ctx.get_glossary()
        relevant = self.ctx.glossary_for(
            text if self.glossary_relevant_only else None,
            limit=self.glossary_prompt_max_entries)
        if known and len(relevant) < len(known):
            self.log(_(
                'Glossary prompt: listing {} of {} known entries '
                '(the ones chapter {} mentions).').format(
                    len(relevant), len(known), chapter.index))
        return known, ', '.join(sorted(relevant.keys())) \
            or model_text('(none)')

    def _generate_context(self, chapter, source_text, translated_text):
        """Ask for the summary and the glossary of a chapter at once.

        Returns ``(summary, new_entities)``, the same pair the two
        separate calls produce. See :attr:`combined_context_call`.

        With ``translated_text`` None the chapter has not been translated
        yet: the report is asked from the source alone, and the glossary
        it yields says how the names are to be rendered (see
        :attr:`context_timing`).
        """
        from_source = translated_text is None
        if not from_source and not translated_text.strip():
            return '', []
        max_chars = self._summary_input_budget_chars()
        src_clipped = self._head(source_text, max_chars)
        system_prompt = self._fill_placeholders(model_text(
            'You are a helpful assistant. Answer with strict JSON '
            'only.'))
        if from_source:
            known, existing_keys = self._glossary_prompt_keys(
                chapter, src_clipped)
            user_prompt = self._compose_prompt(
                self.context_source_prompt,
                {
                    '{chapter_num}': (None, str(chapter.index)),
                    '{chapter_title}': (None, chapter.title or ''),
                    '{existing_keys}': (
                        model_text('Already known (skip these):'),
                        existing_keys),
                    '{max_entities}': (
                        None, str(self.glossary_chapter_max_entries)),
                    '{source_text}': (model_text('Source:'), src_clipped),
                },
                required=('{existing_keys}', '{source_text}'))
            label = _('summary + glossary of chapter {}, from the '
                      'source').format(chapter.index)
        else:
            tgt_clipped = self._head(translated_text, max_chars)
            known, existing_keys = self._glossary_prompt_keys(
                chapter, '%s\n%s' % (src_clipped, tgt_clipped))
            user_prompt = self._compose_prompt(
                self.context_prompt,
                {
                    '{chapter_num}': (None, str(chapter.index)),
                    '{chapter_title}': (None, chapter.title or ''),
                    '{existing_keys}': (
                        model_text('Already known (skip these):'),
                        existing_keys),
                    '{max_entities}': (
                        None, str(self.glossary_chapter_max_entries)),
                    '{source_text}': (model_text('Source:'), src_clipped),
                    '{translated_text}': (
                        model_text('Translation:'), tgt_clipped),
                },
                required=(
                    '{existing_keys}', '{source_text}',
                    '{translated_text}'))
            label = _('summary + glossary of chapter {}').format(
                chapter.index)
        try:
            response = self._translate_context_call(
                system_prompt, user_prompt, label,
                schema=self._context_schema())
        except TranslationFailed as e:
            self.log(_(
                'Summary + glossary extraction failed: {}').format(e), True)
            return '', []

        obj = _extract_json_object(response)
        if obj and isinstance(obj.get('summary'), str):
            summary = obj['summary'].strip()
        else:
            # The object never closed: the summary is written before the
            # entity list, so it is usually there in full anyway.
            summary = _extract_json_string(response, 'summary')
            obj = None
        # A missing flag is read as "part of the story": a model that
        # ignored the field is not a reason to drop a real summary.
        if obj is not None and obj.get('narrative') is False \
                and self.context_narrative_only:
            self._discard_context(chapter, summary)
            return '', []
        entities = self._new_entities(
            self._parse_entities(response, obj=obj), known, chapter)
        if not summary.strip() and entities:
            # The reply listed the names first and was cut before the
            # summary: a chapter without one is a hole in every later
            # prompt, so the summary is asked for on its own.
            self.log(_(
                'The reply carried the glossary but no summary; asking '
                'for the summary on its own.'))
            summary = self._generate_summary(
                chapter, source_text if from_source else translated_text)
        return self._clip_summary(summary, chapter), entities

    def _discard_context(self, chapter, summary):
        """Log that a chapter is not part of the story and that nothing
        of it goes into the running context."""
        self._chapter_is_narrative = False
        what = (summary or '').strip().replace('\n', ' ')
        if len(what) > 120:
            what = what[:117] + '...'
        self.log(_(
            'Chapter {} is not part of the story{}: its summary and '
            'glossary are discarded.').format(
                chapter.index, ' (%s)' % what if what else ''))

    # -- persistence -------------------------------------------------------

    def _translation_identity(self):
        """The (engine name, target language) pair stamped on a paragraph
        when its translation is stored."""
        engine_name = getattr(self.translator, 'name', None)
        target_lang = None
        if hasattr(self.translator, 'get_target_lang'):
            try:
                target_lang = self.translator.get_target_lang()
            except Exception:
                target_lang = None
        target_lang = target_lang or getattr(
            self.translator, 'target_lang', None)
        return engine_name, target_lang

    def _already_translated(self, paragraph, identity):
        """Whether the cache already holds a usable translation of
        ``paragraph``, made by the engine and for the language of this run.

        A paragraph carrying no engine or language at all is accepted:
        older caches predate those columns being filled in.
        """
        if not (paragraph.translation or '').strip():
            return False
        engine_name, target_lang = identity
        if engine_name and paragraph.engine_name \
                and paragraph.engine_name != engine_name:
            return False
        if target_lang and paragraph.target_lang \
                and paragraph.target_lang != target_lang:
            return False
        return True

    def _store_chapter(self, chapter, translations, positions=None):
        """Write chapter translations back to cache paragraphs.

        :positions: chapter positions to write. This runs once per chunk
            while ``translations`` keeps growing, so without it every
            chunk rewrites all the paragraphs the previous ones stored.
            ``None`` writes everything that has a translation.
        """
        engine_name, target_lang = self._translation_identity()
        pending = []
        for i, paragraph in enumerate(chapter.paragraphs, start=1):
            if paragraph.ignored:
                continue
            if positions is not None and i not in positions:
                continue
            translation = translations.get(i)
            if translation is None:
                continue
            paragraph.translation = translation
            paragraph.engine_name = engine_name
            paragraph.target_lang = target_lang
            paragraph.is_cache = False
            pending.append(paragraph)
        self.cache.update_paragraphs(pending)

    # -- main loop ---------------------------------------------------------

    def run(self):
        """Execute the pipeline. Returns the number of chapters translated."""
        self.total_chapters = len(self.chapters)
        self.completed_chapters = 0
        if self.total_chapters == 0 and not self.aux_paragraphs:
            self.log(_('Novel mode: no chapters to translate.'))
            return 0

        self._load_usage()
        self._run_started = time.time()
        self._translate_auxiliary()
        if self.total_chapters == 0:
            self.run_seconds += self._run_elapsed()
            self._run_started = None
            self._publish_report()
            return 0

        start = self.ctx.get_progress()
        if start >= self.total_chapters:
            self.log(
                _('Novel mode: nothing to do (progress={}, chapters={}).')
                .format(start, self.total_chapters))
            return 0

        self.log(sep())
        self.log(_('Novel mode: starting.'))
        self.log(_('Total chapters: {}').format(self.total_chapters))
        if self.model_output_limit:
            self.log(_('Model reply limit: {} tokens.').format(
                self.model_output_limit))
        self._log_sizing()
        self.log(_('Resuming from chapter: {}').format(start + 1))
        # Both are worked out once for the whole book and then repeated
        # in the system prompt of every request: the dialogue rule is
        # free (it is read off the source), the author brief costs one
        # request the first time and nothing on a resume.
        self._ensure_author_style()
        self._ensure_dialogue_rules()
        self.log(sep('┈'))

        start_ts = time.time()
        try:
            for chapter in self.chapters[start:]:
                if self.cancel_request():
                    raise TranslationCanceled(_('Translation canceled.'))
                self._translate_chapter(chapter)
                self.completed_chapters += 1
        finally:
            # Whatever ended the run, what it cost, how long it took and
            # how the providers behaved is worth keeping.
            self.run_seconds += self._run_elapsed()
            self._run_started = None
            self._publish_report()

        elapsed = round((time.time() - start_ts) / 60, 2)
        self.log(sep())
        self.log(_('Novel mode: completed {} chapter(s) in {} minutes.')
                 .format(self.completed_chapters, elapsed))
        for line in self.build_report().split('\n'):
            self.log(line)
        self.progress(1.0, _('Novel mode: completed.'))
        return self.completed_chapters

    def _translate_chapter(self, chapter):
        self.chapter_started(chapter)
        self.log(sep())
        self.log(_('Chapter {}/{}: {}').format(
            chapter.index, self.total_chapters, chapter.title))

        total_chars = sum(
            len(p.original or '')
            for p in chapter.paragraphs if not p.ignored)
        self.log(_(
            '  Structure: {} page(s), {} paragraphs, {} chars. '
            'page_ids: {}').format(
                len(chapter.page_ids),
                len(chapter.paragraphs),
                total_chars,
                chapter.page_ids[:5])
            + (' ...' if len(chapter.page_ids) > 5 else ''))

        translatable = chapter.translatable_paragraphs()
        if not translatable:
            self.log(_('Chapter {} has no translatable content, skipping.')
                     .format(chapter.index))
            self.ctx.append_chapter(chapter.index, chapter.title, '', [])
            self.chapter_done(chapter, '', [])
            self._report_progress()
            return
        if title_matches(chapter.title, self.untranslated_titles):
            self._keep_original(chapter, translatable)
            return
        front_matter = title_matches(chapter.title, self.front_matter_titles)
        if front_matter:
            self.log(_(
                'Chapter {} is front matter by its title: translated, but '
                'no summary or glossary is asked for it '
                '(novel_front_matter_titles).').format(chapter.index))

        # Resume inside the chapter. Translations are stored after every
        # chunk but the progress counter only moves once the chapter is
        # over, so a run cancelled at chunk 7 of 9 would otherwise pay
        # for those seven chunks a second time.
        position = {p.id: i
                    for i, p in enumerate(chapter.paragraphs, start=1)}
        translations = {}
        pending = list(translatable)
        if self.reuse_translated_paragraphs:
            identity = self._translation_identity()
            pending = []
            for paragraph in translatable:
                if self._already_translated(paragraph, identity):
                    translations[position[paragraph.id]] = \
                        paragraph.translation
                else:
                    pending.append(paragraph)
            if translations:
                self.log(_(
                    '  {} of {} paragraphs are already translated in the '
                    'cache; asking the model for the remaining {}.').format(
                        len(translations), len(translatable), len(pending)))
            if not pending:
                self.log(_(
                    '  Nothing left to translate in this chapter.'))

        source_text = self._build_source_chapter_text(chapter.paragraphs)
        context_text = self.ctx.context_text(
            budget_tokens=self.context_tokens,
            relevant_to=source_text if self.glossary_relevant_only else None,
            glossary_limit=self.glossary_prompt_max_entries)

        summary = ''
        glossary_delta = []
        context_done = front_matter
        # Summary + glossary from the source, before the chapter is
        # translated: half the tokens of asking afterwards with the
        # translation alongside, and the names the chapter introduces
        # are rendered the same way in every chunk of it.
        if not context_done and pending and self.context_timing == 'before' \
                and self._context_wanted(chapter, source_text):
            self._chapter_is_narrative = True
            summary, glossary_delta = self._generate_context(
                chapter, source_text, None)
            context_done = True
            if self._chapter_is_narrative:
                self._log_summary(chapter, summary)
                if summary or glossary_delta:
                    context_text = self.ctx.context_text(
                        budget_tokens=self.context_tokens,
                        relevant_to=(
                            source_text if self.glossary_relevant_only
                            else None),
                        glossary_limit=self.glossary_prompt_max_entries,
                        pending=(
                            {'chapter': chapter.index,
                             'title': chapter.title or '',
                             'summary': summary},
                            glossary_delta))

        reserved = (self.context_tokens + self.summary_tokens
                    + self.overlap_paragraphs * 80)
        missing = self._translate_pending(
            chapter, pending, context_text, position, translations,
            reserved)
        self._settle_missing(chapter, missing, context_text, position,
                             translations, reserved)

        # Summary + glossary from the translation, once it is there.
        translated_text = self._build_translated_chapter_text(
            chapter.paragraphs, translations)
        if not context_done \
                and self._context_wanted(chapter, translated_text):
            if self.combined_context_call:
                self._chapter_is_narrative = True
                summary, glossary_delta = self._generate_context(
                    chapter, source_text, translated_text)
                if self._chapter_is_narrative:
                    self._log_summary(chapter, summary)
            else:
                self._chapter_is_narrative = True
                summary = self._generate_summary(chapter, translated_text)
                if self._chapter_is_narrative:
                    self._log_summary(chapter, summary)
                    glossary_delta = self._extract_glossary_updates(
                        chapter, source_text, translated_text)

        # Persist context (marks chapter as done, bumps progress). The
        # report is written before the window hears that the chapter is
        # done, because that is when it reads the report back.
        self.ctx.append_chapter(
            chapter.index, chapter.title, summary, glossary_delta)
        self._publish_report()
        self.chapter_done(chapter, summary, glossary_delta)
        self._report_progress()

    def _context_wanted(self, chapter, text):
        """Whether a summary and a glossary are asked for ``chapter``,
        judged on ``text`` -- its source or its translation, whichever
        the timing gives. Trivially short chapters (a copyright page, a
        dedication) are translated but not summarised, and neither is
        the last chapter when the setting says so: nothing comes after
        it."""
        length = len((text or '').strip())
        threshold = self.min_chars_for_context
        if length < threshold:
            self.log(_(
                'Chapter {}: {} chars, below threshold {}. Skipping '
                'summary + glossary extraction.').format(
                    chapter.index, length, threshold))
            return False
        if self._is_last_chapter(chapter) and self.skip_context_last_chapter:
            self.log(_(
                'Chapter {} is the last one: skipping summary + glossary, '
                'nothing comes after them.').format(chapter.index))
            return False
        return True

    def _keep_original(self, chapter, translatable):
        """Store the source of every paragraph as its translation: the
        chapter stays in the language it is written in (see
        :attr:`untranslated_titles`)."""
        self.log(_(
            'Chapter {} ("{}") stays in the original language '
            '(novel_untranslated_titles).').format(
                chapter.index, chapter.title))
        translations = {
            i: (p.original or '')
            for i, p in enumerate(chapter.paragraphs, start=1)
            if not p.ignored}
        self._store_chapter(chapter, translations)
        self.ctx.append_chapter(chapter.index, chapter.title, '', [])
        self._publish_report()
        self.chapter_done(chapter, '', [])
        self._report_progress()

    def _translate_pending(self, chapter, pending, context_text, position,
                           translations, reserved, ratio=1.0):
        """Send ``pending`` to the model chunk by chunk.

        What comes back is written into ``translations`` (keyed by the
        paragraph's position in ``chapter``, see ``position``) and into
        the cache, one transaction per chunk. Returns the paragraphs that
        still have no translation afterwards.

        :ratio: scales both chunk caps down; the second pass over what
            the first one missed uses 0.5.
        """
        chunk_tokens = self._effective_chunk_tokens(reserved)
        max_paragraphs = self.max_paragraphs_per_chunk
        if ratio != 1.0:
            chunk_tokens = max(200, int(chunk_tokens * ratio))
            if max_paragraphs:
                max_paragraphs = max(1, int(max_paragraphs * ratio))
        budget = TokenBudget(
            budget=chunk_tokens, max_paragraphs=max_paragraphs)
        chunks_with_stats = budget.chunk_with_stats(pending)
        if chapter.index and self.parallel_chunks > 1 \
                and self.balanced_chunks and len(chunks_with_stats) > 1:
            chunks_with_stats = budget.balance(chunks_with_stats)
        chunks = [c for c, _tok, _reason in chunks_with_stats]
        total_chunks = len(chunks)
        cap_paragraphs_display = (
            str(max_paragraphs) if max_paragraphs else _('unlimited'))
        overlap_display = (
            str(self.overlap_paragraphs)
            if self.overlap_paragraphs else _('disabled'))
        self.log(_(
            'Split into {} chunk(s). Caps: {} tokens / {} paragraphs. '
            'Overlap: {} paragraphs.')
            .format(
                total_chunks, chunk_tokens, cap_paragraphs_display,
                overlap_display))
        # Per-chunk diagnostic (helps tune the caps against a real book).
        # ``reason`` is one of TokenBudget.REASON_* and tells which limit
        # closed the chunk.
        for i, (chunk_paras, tok_est, reason) in enumerate(
                chunks_with_stats, start=1):
            visible = sum(
                1 for p in chunk_paras if not getattr(p, 'ignored', False))
            self.log(_(
                '  Chunk {}/{}: {} paragraphs, ~{} tokens '
                '(closed by: {}).').format(
                    i, total_chunks, visible, tok_est, reason))

        # What a chunk is shown of its surroundings. The translated
        # overlap needs the previous chunk to be done, so chunks in
        # flight together read the source text around them instead.
        mode = self.chunk_context
        workers = min(self.parallel_chunks, total_chunks) \
            if chapter.index else 1
        if workers > 1 and mode != 'source':
            mode = 'source'
            self.log(_(
                'Chunks in flight at once: the context around each chunk '
                'is the source text, the translated overlap needs the '
                'previous chunk to be done (novel_chunk_context).'))
        if workers > 1:
            self.log(_('Chunks in flight at once: {}.').format(workers))

        def source_context(chunk):
            if mode != 'source':
                return None
            return self._source_context(chapter, chunk, position)

        if workers > 1:
            self._translate_chunks_in_flight(
                chapter, chunks, context_text, position, translations,
                workers, source_context)
            return [p for p in pending if position[p.id] not in translations]

        # Sequential. A chunk-local index is mapped back to the position
        # of that paragraph inside the chapter through ``position``
        # (keyed by the cache row id), so a chunk list that covers only
        # part of the chapter -- which is what resuming produces -- still
        # lands in the right place.
        #
        # Sliding overlap: after each chunk we capture up to
        # ``self.overlap_paragraphs`` of its just-produced translations
        # and pass them to the next chunk as reading context. The first
        # chunk of a chapter starts with an empty overlap.
        overlap_translations = []

        for c_idx, chunk in enumerate(chunks, start=1):
            if self.cancel_request():
                raise TranslationCanceled(_('Translation canceled.'))
            # Local (per-chunk) indices are what the LLM sees; we later
            # rewrite them back into chapter-level indices.
            chunk_result = self._translate_chunk(
                chunk, context_text, chapter.index, chapter.title,
                c_idx, total_chunks,
                overlap_translations=(
                    overlap_translations or None
                    if mode == 'translated' else None),
                source_context=source_context(chunk))
            new_translations_in_chunk = self._store_chunk(
                chapter, chunk, chunk_result, position, translations)

            # Prepare overlap for the next chunk (last N translated
            # paragraphs of THIS chunk). If this chunk produced fewer
            # translations than the overlap window, the window naturally
            # shrinks to what is available.
            if self.overlap_paragraphs > 0 and new_translations_in_chunk:
                overlap_translations = new_translations_in_chunk[
                    -self.overlap_paragraphs:]
            else:
                overlap_translations = []

        return [p for p in pending if position[p.id] not in translations]

    def _store_chunk(self, chapter, chunk, chunk_result, position,
                     translations):
        """Put what a chunk brought back into ``translations`` and the
        cache, move the progress bar, and return the translations in
        the order the model saw them (for the overlap of the next
        chunk). Always on the thread that owns the cache."""
        new_translations_in_chunk = []
        written = set()
        for local_i, p in enumerate(chunk, start=1):
            if p.ignored:
                continue
            translation = chunk_result.get(local_i)
            if translation is not None:
                chapter_position = position[p.id]
                translations[chapter_position] = translation
                written.add(chapter_position)
                new_translations_in_chunk.append(translation)
        # Persist translations as they arrive -- only the ones this
        # chunk produced, in a single transaction.
        self._store_chapter(chapter, translations, positions=written)
        if chapter.index:
            self._report_progress(current_chars=sum(
                len(p.original or '') for p in chapter.paragraphs
                if not p.ignored and position[p.id] in translations))
        return new_translations_in_chunk

    def _source_context(self, chapter, chunk, position):
        """The source of the paragraphs before and after ``chunk`` in
        the chapter, up to :attr:`source_context_paragraphs` each way,
        as ``(before, after)`` lists of text; None when there is
        nothing to show."""
        count = self.source_context_paragraphs
        if not count:
            return None
        visible = [p for p in chunk if not getattr(p, 'ignored', False)]
        if not visible:
            return None
        first = position[visible[0].id]
        last = position[visible[-1].id]
        before = [
            (p.original or '').strip() for p in chapter.paragraphs
            if not p.ignored and position[p.id] < first][-count:]
        after = [
            (p.original or '').strip() for p in chapter.paragraphs
            if not p.ignored and position[p.id] > last][:count]
        if not before and not after:
            return None
        return before, after

    def _clone(self):
        """A copy of this translator for one chunk in flight: its own
        engine, because an engine swaps its prompt, its body builder
        and its reply facts per request and cannot serve two at once;
        the totals, the lock and the callbacks shared with the root.
        The engine copy is registered with the root engine so that a
        cancel (``Base.abort``) reaches the request it is reading."""
        clone = copy.copy(self)
        engine = copy.copy(self.translator)
        try:
            engine.inflight = None
        except Exception:
            pass
        clone.translator = engine
        clone._root = self._root
        clone._request_kind = 'translation'
        root_engine = self._root.translator
        clones = getattr(root_engine, 'clones', None)
        if clones is None:
            try:
                root_engine.clones = clones = []
            except Exception:
                clones = None
        if clones is not None:
            clones.append(engine)
        return clone

    def _translate_chunks_in_flight(self, chapter, chunks, context_text,
                                    position, translations, workers,
                                    source_context):
        """Send the chunks of a chapter with up to ``workers`` requests
        at once, each on a clone (see :meth:`_clone`), and store what
        comes back on this thread as it arrives. A failure or a cancel
        in any chunk ends the chapter: the rest are not started, and
        the ones already reading are cut short through the engine."""
        total_chunks = len(chunks)
        # Anything said once per book is said here, not once per clone.
        self._structured_active()
        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                for c_idx, chunk in enumerate(chunks, start=1):
                    clone = self._clone()
                    futures[pool.submit(
                        clone._translate_chunk, chunk, context_text,
                        chapter.index, chapter.title, c_idx, total_chunks,
                        None, source_context(chunk))] = chunk
                for future in as_completed(futures):
                    chunk = futures[future]
                    chunk_result = future.result()
                    self._store_chunk(
                        chapter, chunk, chunk_result, position,
                        translations)
            except BaseException:
                for future in futures:
                    future.cancel()
                abort = getattr(self.translator, 'abort', None)
                if callable(abort):
                    abort()
                raise
            finally:
                try:
                    self.translator.clones = []
                except Exception:
                    pass

    def _settle_missing(self, chapter, missing, context_text, position,
                        translations, reserved):
        """Deal with the paragraphs a pass over the chapter did not bring
        back: one more pass in smaller chunks, then the configured policy
        (see ``on_missing_paragraphs``)."""
        if not missing:
            return
        # A chunk the model could not finish is the usual reason, and a
        # smaller chunk is the remedy the retries inside a chunk cannot
        # apply: they ask for the missing paragraphs at the same size.
        self.log(_(
            'Chapter {}: {} paragraph(s) still missing; sending them '
            'again in smaller chunks.').format(
                chapter.index, len(missing)), True)
        missing = self._translate_pending(
            chapter, missing, context_text, position, translations,
            reserved, ratio=0.5)
        if not missing:
            return
        message = _(
            'Chapter {}: {} paragraph(s) could not be translated after '
            'every retry (positions {}).').format(
                chapter.index, len(missing),
                ', '.join(str(position[p.id]) for p in missing))
        if self.on_missing_paragraphs == 'stop':
            raise TranslationFailed(message + ' ' + _(
                'The chapter is left unfinished so that a resume asks for '
                'them again; what was translated is in the cache.'))
        self.log(message + ' ' + _(
            'Going on with the next chapter, as configured: these '
            'paragraphs keep their source text.'), True)

    def _translate_auxiliary(self):
        """Translate what sits outside the chapters: the metadata, the
        table of contents and the pages the front-matter filter kept out
        of the narrative (cover, title page, dedication, part dividers).

        They go through the same chunk machinery as a chapter -- one
        request for the lot when they fit -- with no running context and
        no summary afterwards: there is no story in them to keep track
        of. Paragraphs the cache already holds are kept, so a resume
        costs nothing here.
        """
        paragraphs = [p for p in self.aux_paragraphs if not p.ignored]
        if not paragraphs:
            return
        pending = list(paragraphs)
        if self.reuse_translated_paragraphs:
            identity = self._translation_identity()
            pending = [p for p in paragraphs
                       if not self._already_translated(p, identity)]
        if not pending:
            return
        self.log(sep())
        self.log(_(
            'Metadata, contents and front matter: {} of {} paragraphs to '
            'translate.').format(len(pending), len(paragraphs)))
        chapter = Chapter(0, model_text('Front matter'), [], paragraphs)
        position = {p.id: i
                    for i, p in enumerate(chapter.paragraphs, start=1)}
        translations = {}
        reserved = self.overlap_paragraphs * 80
        missing = self._translate_pending(
            chapter, pending, '', position, translations, reserved)
        self._settle_missing(chapter, missing, '', position, translations,
                             reserved)

    def _is_last_chapter(self, chapter):
        return bool(self.chapters) \
            and chapter.index == self.chapters[-1].index

    def _log_summary(self, chapter, summary):
        if summary:
            preview = summary.strip().replace('\n', ' ')
            if len(preview) > 160:
                preview = preview[:157] + '...'
            self.log(_('Summary (chapter {}): {}').format(
                chapter.index, preview))
        else:
            self.log(_(
                'Summary (chapter {}): empty response.').format(
                    chapter.index), True)

    def _report_progress(self, current_chars=0):
        """Move the progress bar by the text translated, not by the
        chapters done: the pages before the story are chapters too, and
        counted as such the bar was at a third before the first real
        chapter. ``current_chars`` is what the chapter being worked on
        has already had translated."""
        done = self.ctx.get_progress()
        total = self.total_chapters or 1
        total_chars = sum(c.char_count for c in self.chapters) or 1
        done_chars = sum(
            c.char_count for c in self.chapters if c.index <= done)
        fraction = min(1.0, (done_chars + current_chars) / float(total_chars))
        self.progress(
            fraction,
            _('Novel mode: chapter {}/{} done, {}% of the text.').format(
                done, total, int(100 * fraction)))


# ---------------------------------------------------------------------------
# Helper: cache-id namespacing for novel mode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Probe: a few paragraphs through the translation path, to see what a
# model and the provider behind it do before a whole book is sent
# ---------------------------------------------------------------------------


PROBE_PARAGRAPHS = (
    'Chapter One',
    '\u2018Leave the rest to the gods,\u2019 she said, and closed the '
    'door behind her.',
    'The house was quiet. From the street below came the smell of bread '
    'and the noise of carts; nobody had yet noticed that the lamp in the '
    'upper window had gone out.',
    '\u2018And what does she want with me?\u2019',
    'He did not answer. He put a sandalled foot out as if to move away, '
    'then thought better of it and sat down again on the cold step, '
    'looking at the {{id_00001}} mark on the wall.',
)


class _ProbeCache:
    """Enough of a cache for a probe: it remembers nothing."""

    def get_info(self, key):
        return None

    def set_info(self, key, value):
        pass

    def update_paragraphs(self, paragraphs):
        pass

    def update_paragraph(self, paragraph):
        pass


def probe_engine(engine, config=None, details=False):
    """Send :data:`PROBE_PARAGRAPHS` through the translation path of a
    configured engine and report what came back, the way a run would
    see it: the provider, why the model stopped, what it cost, how many
    paragraphs came back and whether they are the right ones. The
    engine's languages must be set. Returns the report as text, or
    ``(text, reliable)`` with ``details``."""
    from .cache import Paragraph

    lines = []
    cache = _ProbeCache()
    ctx = ContextManager(cache).load()
    paragraphs = [
        Paragraph(i, 'probe-%d' % i, text, text, page='probe')
        for i, text in enumerate(PROBE_PARAGRAPHS)]
    chapter = Chapter(1, 'Probe', ['probe'], paragraphs)
    translator = NovelTranslator(
        engine, [chapter], ctx, cache, config=dict(config or {}))
    translator.set_logging(
        lambda text, error=False: lines.append(
            ('[ERROR] ' if error else '') + str(text)))
    started = time.time()
    error = None
    try:
        result = translator._translate_chunk(
            paragraphs, '', 1, chapter.title, 1, 1)
    except Exception as e:
        result = {}
        error = describe_error(e)
    elapsed = round(time.time() - started, 1)

    report = []
    report.append(_('Engine: {}').format(getattr(engine, 'name', '?')))
    model = getattr(engine, 'model', None)
    if model:
        report.append(_('Model: {}').format(model))
    report.append(_('Languages: {} to {}').format(
        getattr(engine, 'source_lang', '?'),
        getattr(engine, 'target_lang', '?')))
    report.append(_('Output format: {}').format(
        _('structured JSON') if translator._structured_active()
        else _('text markers')))
    served = getattr(engine, 'last_provider', None)
    if served:
        report.append(_('Provider: {}').format(served))
    reason = getattr(engine, 'last_finish_reason', None)
    if reason:
        report.append(_('Finish reason: {}').format(reason))
    generation = getattr(engine, 'last_generation_id', None)
    if generation:
        report.append(_('Generation id: {}').format(generation))
    usage = getattr(engine, 'last_usage', None) or {}
    if usage.get('prompt_tokens') is not None:
        cost = usage.get('cost')
        report.append(_('Last reply: {} in + {} out tokens{}').format(
            usage.get('prompt_tokens'), usage.get('completion_tokens'),
            ', $' + _money(cost) if cost is not None else ''))
    report.append(_('Time: {}s').format(elapsed))
    if error:
        report.append(_('Failed: {}').format(error))
    expected = len(PROBE_PARAGRAPHS)
    report.append(_('Paragraphs back: {} of {}').format(len(result), expected))
    if len(result) == expected:
        report.append(_('Numbers and content: aligned, every check passed.'))
    else:
        report.append(_(
            'Numbers and content: NOT reliable -- {} paragraph(s) never '
            'came back right, even after the retries. Do not send a book '
            'to this model on this provider.').format(expected - len(result)))
    calls = sum(
        entry.get('requests', 0) for entry in translator.usage.values())
    if calls > 1:
        report.append(_('Requests needed: {} (retries were necessary).')
                      .format(calls))
    report.append('')
    report.append(_('Translations:'))
    for i, text in enumerate(PROBE_PARAGRAPHS, start=1):
        report.append('  [%d] %s' % (i, text))
        report.append('      -> %s' % (result.get(i) or _('(missing)')))
    report.append('')
    report.append(_('Log:'))
    report.extend('  ' + line for line in lines)
    text = '\n'.join(report)
    if details:
        return text, len(result) == expected and error is None
    return text


def novel_cache_id(input_path, engine_name, target_lang, encoding=''):
    """Compute a cache id specific to novel mode.

    The classic cache id (see ``lib/conversion.py:convert_item``) mixes in
    ``merge_length``. Novel mode has no merge length, so we substitute the
    tag ``novel_v1`` to keep the two caches strictly separated. That way,
    switching modes on the same book does not clobber the other mode's
    stored translations.
    """
    return uid(
        input_path + engine_name + target_lang + 'novel_v1'
        + (encoding or ''))

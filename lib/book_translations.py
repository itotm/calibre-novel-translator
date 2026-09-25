"""The translations of a book.

A book can be translated more than once -- by another model, into another
language, at another service tier -- to compare the results or to keep
the better one. Every translation is a cache of its own: its paragraphs,
its summaries and glossary, its log and its report. This module creates
them, finds the ones of a book, says what each one is, and applies what a
translation records about itself (the model, the tier) to the engine that
continues it. The dialog that lists them and the window that runs one are
in ``novel.py``.

A translation records, in the info table of its cache, the book it was
made from (calibre's library and book id, and the path of the input
file), the engine, the model and the tier. Caches written before 1.3
record none of that and are recognised by the id they were given, which
was worked out from the input path, the engine and the language.
"""
import os
import json
import time
import uuid
from glob import glob
from datetime import datetime

from calibre.utils.localization import _  # type: ignore

from .cache import TranslationCache, get_cache
from .utils import uid
from .novel import (
    novel_cache_id, INFO_NOVEL_CHAPTERS, INFO_NOVEL_PROGRESS)


load_translations()  # type: ignore


INFO_TITLE = 'title'
INFO_AUTHOR = 'author'
INFO_ENGINE = 'engine_name'
INFO_TARGET_LANG = 'target_lang'
INFO_SOURCE_LANG = 'source_lang'
INFO_INPUT_PATH = 'input_path'
INFO_INPUT_FORMAT = 'input_format'
INFO_ENCODING = 'encoding'
INFO_BOOK_ID = 'book_id'
INFO_LIBRARY_ID = 'library_id'
INFO_MODEL = 'model'
# The provider preset of the OpenAI-compatible engine the translation
# was made with: the model is that provider's, and sent to another one
# it is a model that does not exist.
INFO_PROVIDER = 'provider'
INFO_SERVICE_TIER = 'service_tier'
# What the provider said about the model when the translation was
# created: its reply limit and the parameters it accepts. The engine
# preferences hold the same two figures for the model chosen in the
# settings, which is not necessarily this one.
INFO_MODEL_LIMITS = 'model_limits'
INFO_CREATED = 'created'
# The ids of the paragraphs whose translation was corrected by hand, so
# that translating the book again can say how many it would replace.
INFO_EDITED = 'novel_edited_paragraphs'


def ebook_encoding(ebook):
    """The encoding as it enters the cache id: '' for UTF-8."""
    encoding = (getattr(ebook, 'encoding', '') or '').lower()
    return '' if encoding in ('', 'utf-8') else encoding


def new_translation_id(input_path, engine_name, target_lang, model,
                       encoding=''):
    """A cache id no other translation has.

    The id of a cache used to be worked out from the book, the engine and
    the language, so there could be one translation of a book per engine
    and language. It is now unique to the translation, and the book is
    recorded inside the cache instead.
    """
    return uid(
        input_path or '', engine_name or '', target_lang or '', 'novel_v2',
        encoding or '', model or '', repr(time.time()), uuid.uuid4().hex)


def stamp_book(cache, ebook, library_id=None):
    """Write down which book ``cache`` translates.

    Done when a translation is created and again every time it is
    opened: calibre moves a book's files when its title or author
    change, and the library and book id are what find it afterwards.
    """
    cache.set_info(INFO_TITLE, ebook.title)
    # The author is asked about once per book. Written here so a build
    # started from the background job -- which only receives the title
    # -- can still find it.
    cache.set_info(INFO_AUTHOR, ebook.get_author())
    cache.set_info(INFO_INPUT_PATH, ebook.get_input_path() or '')
    cache.set_info(INFO_INPUT_FORMAT, ebook.input_format or '')
    cache.set_info(INFO_ENCODING, ebook_encoding(ebook))
    if ebook.id is not None:
        cache.set_info(INFO_BOOK_ID, str(ebook.id))
    if library_id:
        cache.set_info(INFO_LIBRARY_ID, str(library_id))


def model_limits(engine_class, model, configured_model=None):
    """What is known about ``model``: its reply limit and the parameters
    it accepts, from the listing when it was fetched in this session,
    from the engine preferences when it is the model chosen there, and
    nothing otherwise."""
    limits = {}
    get_limits = getattr(engine_class, 'get_model_limits', None)
    if callable(get_limits):
        limits = get_limits(model) or {}
    if limits:
        return {
            'max_output_tokens': int(limits.get('max_output_tokens') or 0),
            'supported_parameters': list(
                limits.get('supported_parameters') or []),
        }
    config = getattr(engine_class, 'config', None) or {}
    if model and model == configured_model and (
            config.get('model_max_output_tokens')
            or config.get('model_supported_parameters')):
        return {
            'max_output_tokens': int(
                config.get('model_max_output_tokens') or 0),
            'supported_parameters': list(
                config.get('model_supported_parameters') or []),
        }
    return {}


def engine_provider(engine_class):
    """The provider preset an engine class is set to, None for an engine
    without presets."""
    if not getattr(engine_class, 'providers', None):
        return None
    return (getattr(engine_class, 'config', None) or {}).get('provider') \
        or getattr(engine_class, 'provider', None)


def engine_class_for(engine_class, info):
    """``engine_class`` set to the provider the translation was made
    with, when the settings have moved on to another one.

    The preferences of the other providers are kept aside in the engine
    preferences (``providers``); they make the configuration of a class
    of its own, derived from ``engine_class``, so the class the settings
    and the other windows use is left as it is.
    """
    provider = info.get(INFO_PROVIDER)
    providers = getattr(engine_class, 'providers', None) or {}
    if not provider or provider not in providers \
            or provider == engine_provider(engine_class):
        return engine_class
    config = dict(engine_class.config or {})
    own = (config.get('providers') or {}).get(provider) or {}
    for key in getattr(engine_class, 'per_provider_keys', ()):
        config.pop(key, None)
    config.update(own)
    config['provider'] = provider
    return type(engine_class.__name__, (engine_class,), {'config': config})


def create_translation(ebook, engine_name, model, service_tier=None,
                       limits=None, library_id=None, provider=None):
    """Start a new translation of ``ebook`` into its target language,
    with ``model`` on the engine called ``engine_name`` (and, for the
    OpenAI-compatible engine, its ``provider`` preset). Returns the id
    of its cache, which the preparation then fills."""
    cache_id = new_translation_id(
        ebook.get_input_path(), engine_name, ebook.target_lang, model,
        ebook_encoding(ebook))
    cache = get_cache(cache_id)
    try:
        stamp_book(cache, ebook, library_id)
        cache.set_info(INFO_ENGINE, engine_name)
        cache.set_info(INFO_TARGET_LANG, ebook.target_lang)
        cache.set_info(INFO_SOURCE_LANG, ebook.source_lang or '')
        cache.set_info(INFO_CREATED, datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S'))
        if model:
            cache.set_info(INFO_MODEL, model)
        if provider:
            cache.set_info(INFO_PROVIDER, provider)
        if service_tier:
            cache.set_info(INFO_SERVICE_TIER, service_tier)
        if limits:
            cache.set_info(INFO_MODEL_LIMITS, json.dumps(limits))
    finally:
        cache.close()
    return cache_id


def matching_format(info, cache_id, ebook, library_id=None):
    """The format of ``ebook`` the translation in a cache was made from,
    or None when the cache belongs to another book.

    ``info`` is the info table of the cache. A translation is the
    book's when it names the same library and book id, or, failing
    that, the path of one of the book's files. A cache written before
    the book was recorded is the book's when its id is the one it would
    have been given for one of the book's files, its engine and its
    language.
    """
    files = {fmt: path for fmt, path in (ebook.files or {}).items() if path}
    book_id = info.get(INFO_BOOK_ID)
    if book_id and library_id and ebook.id is not None \
            and str(info.get(INFO_LIBRARY_ID) or '') == str(library_id) \
            and str(book_id) == str(ebook.id):
        return info.get(INFO_INPUT_FORMAT) or next(iter(files), None)
    input_path = info.get(INFO_INPUT_PATH)
    if input_path:
        for fmt, path in files.items():
            if path == input_path:
                return fmt
        return None
    engine_name = info.get(INFO_ENGINE)
    target_lang = info.get(INFO_TARGET_LANG)
    if not (engine_name and target_lang
            and info.get(INFO_NOVEL_CHAPTERS) is not None):
        return None
    encodings = {'', ebook_encoding(ebook)}
    for fmt, path in files.items():
        for encoding in encodings:
            if novel_cache_id(
                    path, engine_name, target_lang, encoding) == cache_id:
                return fmt
    return None


class BookTranslation:
    """What the list of a book's translations shows about one of them."""

    def __init__(self, cache_id, info, input_format=None, modified=None):
        self.cache_id = cache_id
        self.info = info
        self.title = info.get(INFO_TITLE) or ''
        self.engine_name = info.get(INFO_ENGINE) or ''
        self.model = info.get(INFO_MODEL) or ''
        self.provider = info.get(INFO_PROVIDER) or ''
        self.service_tier = info.get(INFO_SERVICE_TIER) or ''
        self.target_lang = info.get(INFO_TARGET_LANG) or ''
        self.source_lang = info.get(INFO_SOURCE_LANG) or ''
        self.input_format = input_format or info.get(INFO_INPUT_FORMAT) or ''
        self.encoding = info.get(INFO_ENCODING) or ''
        self.created = info.get(INFO_CREATED) or ''
        self.modified = modified or ''
        try:
            chapters = json.loads(info.get(INFO_NOVEL_CHAPTERS) or '[]')
        except (TypeError, ValueError):
            chapters = []
        self.chapters_total = len(chapters) if isinstance(
            chapters, list) else 0
        try:
            progress = int(info.get(INFO_NOVEL_PROGRESS) or 0)
        except (TypeError, ValueError):
            progress = 0
        self.chapters_done = min(progress, self.chapters_total)
        self.edited = len(parse_ids(info.get(INFO_EDITED)))

    def model_label(self):
        return self.model or _('(not recorded)')

    def tier_label(self):
        if not self.service_tier and not self.model:
            # Written before 1.3 and not run since: it runs on the tier
            # of the settings, whatever that is at the time.
            return _('(from the settings)')
        return self.service_tier if self.service_tier not in (
            '', 'default') else _('Default')

    def engine_label(self):
        return '%s (%s)' % (self.engine_name, self.provider) \
            if self.provider else self.engine_name

    def progress_label(self):
        if not self.chapters_total:
            return _('not prepared')
        if self.chapters_done >= self.chapters_total:
            return _('done ({})').format(self.chapters_total)
        return '%d/%d' % (self.chapters_done, self.chapters_total)

    def is_done(self):
        return 0 < self.chapters_total <= self.chapters_done


def find_translations(ebook, library_id=None):
    """The translations of ``ebook`` in the cache, the most recently
    changed first. Nothing is written: a legacy cache is recorded as the
    book's when it is next opened (see :func:`stamp_book`)."""
    found = []
    pattern = os.path.join(TranslationCache.cache_path, '*.db')
    for file_path in glob(pattern):
        cache_id = os.path.splitext(os.path.basename(file_path))[0]
        try:
            cache = TranslationCache(cache_id)
        except Exception:
            continue
        try:
            info = cache.all_info()
        except Exception:
            info = {}
        finally:
            cache.close()
        fmt = matching_format(info, cache_id, ebook, library_id)
        if fmt is None:
            continue
        modified = datetime.fromtimestamp(
            os.path.getmtime(file_path)).strftime('%Y-%m-%d %H:%M:%S')
        found.append(BookTranslation(cache_id, info, fmt, modified))
    found.sort(key=lambda item: item.modified, reverse=True)
    return found


def delete_translation(cache_id):
    TranslationCache.remove('%s.db' % cache_id)


def read_translation(cache):
    """The info table of ``cache`` as a dict."""
    return cache.all_info()


def configure_translator(translator, info):
    """Make ``translator`` the engine of the translation whose info
    table is ``info``: its model, with what is known about that model,
    and its service tier. A translation that records neither (written
    before 1.3) runs on what the settings say, as it always did."""
    model = info.get(INFO_MODEL)
    if model and hasattr(translator, 'model'):
        configured = translator.model
        translator.model = model
        try:
            limits = json.loads(info.get(INFO_MODEL_LIMITS) or '{}')
        except (TypeError, ValueError):
            limits = {}
        if isinstance(limits, dict) and limits:
            translator.model_max_output_tokens = int(
                limits.get('max_output_tokens') or 0)
            translator.model_supported_parameters = list(
                limits.get('supported_parameters') or [])
        elif model != configured:
            # The figures the settings hold describe another model: a
            # parameter list that is not this model's filters the
            # request wrongly. Unknown is safer -- everything is sent.
            translator.model_max_output_tokens = 0
            translator.model_supported_parameters = []
    tier = info.get(INFO_SERVICE_TIER)
    set_tier = getattr(translator, 'set_service_tier', None)
    if tier and callable(set_tier):
        set_tier(tier)
    return translator


def same_book(first, second):
    """Whether the info tables ``first`` and ``second`` are of
    translations of one book: by the library and book id where both
    record them, by the input file where both record it, by the title
    otherwise (a cache written before 1.3 and never opened since)."""
    def book(info):
        if info.get(INFO_LIBRARY_ID) and info.get(INFO_BOOK_ID):
            return (str(info[INFO_LIBRARY_ID]), str(info[INFO_BOOK_ID]))
        return None
    if book(first) and book(second):
        return book(first) == book(second)
    if first.get(INFO_INPUT_PATH) and second.get(INFO_INPUT_PATH):
        return first[INFO_INPUT_PATH] == second[INFO_INPUT_PATH]
    return bool(first.get(INFO_TITLE)) \
        and first.get(INFO_TITLE) == second.get(INFO_TITLE)


def comparison_problem(infos):
    """Why the translations whose info tables are ``infos`` cannot be
    compared, or None when they can: at least two, of one book, made
    from the same file of it -- another format is another list of
    paragraphs, and they would not line up."""
    if len(infos) < 2:
        return _('Select at least two translations of the same book.')
    if not all(same_book(infos[0], info) for info in infos[1:]):
        return _('The translations selected are not of the same book.')
    if not all(info.get(INFO_INPUT_FORMAT) for info in infos):
        # Written before 1.3 and never opened since: which file of the
        # book it was made from is only known from the book.
        return _(
            'A translation selected was made before version 1.3: open it '
            'once from its book, or compare it from the list of the '
            'book, where its file is known.')
    formats = {info.get(INFO_INPUT_FORMAT) for info in infos}
    if len(formats) > 1:
        return _(
            'The translations were made from different files of the book '
            '({}): their paragraphs do not line up.').format(
                ', '.join(sorted(fmt.upper() for fmt in formats)))
    return None


def compare_labels(infos):
    """A column title for each translation: the model, with the tier
    when it is not the default, the language when they differ, and the
    date it was created when two would still read the same."""
    languages = {info.get(INFO_TARGET_LANG) for info in infos}
    labels = []
    for info in infos:
        label = info.get(INFO_MODEL) or _('(model not recorded)')
        tier = info.get(INFO_SERVICE_TIER)
        if tier and tier != 'default':
            label += ' (%s)' % tier
        if len(languages) > 1:
            label += ' \u2192 %s' % (info.get(INFO_TARGET_LANG) or '?')
        labels.append(label)
    for i, info in enumerate(infos):
        if labels.count(labels[i]) > 1:
            labels[i] += ' [%s]' % (info.get(INFO_CREATED) or str(i + 1))
    return labels


def alignment_keys(paragraphs):
    """A key for each paragraph that the same paragraph has in another
    translation of the book: its text and how many paragraphs with the
    same text come before it. The ids and the checksums count positions,
    and one paragraph more or less in an extraction (a filter rule
    changed in between) would shift every one after it."""
    seen = {}
    keys = {}
    for paragraph in sorted(paragraphs, key=lambda p: p.id):
        text = paragraph.original or ''
        count = seen.get(text, 0)
        seen[text] = count + 1
        keys[paragraph.id] = (text, count)
    return keys


def parse_ids(raw):
    try:
        ids = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return []
    return ids if isinstance(ids, list) else []


def edited_paragraphs(cache):
    """The ids of the paragraphs corrected by hand."""
    return set(parse_ids(cache.get_info(INFO_EDITED)))


def save_translation(cache, paragraph_id, text):
    """Replace the translation of one paragraph with ``text``, typed by
    hand, and remember that it was."""
    cache.update(paragraph_id, translation=text)
    edited = edited_paragraphs(cache)
    edited.add(paragraph_id)
    cache.set_info(INFO_EDITED, json.dumps(sorted(edited, key=str)))


def forget_edits(cache, before=None):
    """What translating the book again does: the corrections go with the
    translations they replace.

    :before: the translations of the corrected paragraphs when the run
        started, ``{id: text}``. Only those whose text has changed since
        are forgotten, so a run that stops half way leaves the rest of
        the corrections marked as such. None forgets them all.
    """
    if before is None:
        cache.del_info(INFO_EDITED)
        return
    still = {
        paragraph.id for paragraph in cache.get_paragraphs(list(before))
        if paragraph.translation == before[paragraph.id]}
    if still:
        cache.set_info(INFO_EDITED, json.dumps(sorted(still, key=str)))
    else:
        cache.del_info(INFO_EDITED)


def edited_texts(cache):
    """The corrected paragraphs with their text, ``{id: text}``."""
    edited = edited_paragraphs(cache)
    if not edited:
        return {}
    return {paragraph.id: paragraph.translation
            for paragraph in cache.get_paragraphs(list(edited))}

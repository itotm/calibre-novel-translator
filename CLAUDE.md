# Novel Translator -- notes for Claude

A calibre plugin that translates a novel chapter by chapter with a
language model, keeping a running summary and glossary. Forked from
bookfere's Ebook-Translator-Calibre-Plugin; the chapter-by-chapter
pipeline originated in upstream PR #590 by BiG86. Those two credits stay
in the README and the About dialog; no other upstream branding, links or
legacy modes come back. The plugin does one thing, so nothing in it is
called "Novel Mode": there is no other mode for it to be set against.

## Every editing session ends with a rebuilt, installed package

The user runs the plugin from the zip installed in calibre, not from the
working tree. A session that changes the plugin is not finished until:

1. `tests/run_tests.sh` passes;
2. `./build_plugin.sh` has rebuilt `../novel-translator_vX.Y.Z.zip`;
3. the zip is installed into the flatpak calibre, with calibre closed
   (`pgrep -af calibre` first):
   `flatpak run --command=calibre-customize com.calibre_ebook.calibre -a ../novel-translator_vX.Y.Z.zip`
4. the final message says so, with the version.

When the version changes, bump `__init__.py` (`version = (X, Y, Z)`), the
install line in README.md and the section title in CHANGELOG.md together,
and remove the previous zip from the parent directory.

## Rules of the code

- Everything configurable, with a default that needs no configuration:
  a new behaviour exists in `lib/config.py` (defaults),
  `lib/conversion.py` (`get_novel_config`), a property on
  `NovelTranslator` in `lib/novel.py`, a widget in `setting.py` (load and
  persist, with a tooltip saying why the default is what it is) and the
  defaults copy in `tests/lib/test_config.py`. Engine-level knobs are a
  class attribute listed in the engine's `preference_keys`.
- Chapters are translated one after the other, always: each depends on
  the summary and glossary of the one before, and the summary/glossary
  calls are never concurrent. Chunks inside a chapter may be in flight
  together only through `novel_parallel_chunks` (default 1); then each
  runs on a clone of the translator with its own engine copy (engines
  swap prompt, body builder and reply facts per request), the context
  around a chunk is the source text and not the translated overlap, and
  cache writes stay on the worker's own thread. Do not raise the default.
- The model's reply carries the translation keyed by its paragraph
  number, never the source text echoed back.
- OpenRouter is the primary engine: the default, listed first, the one new
  features are designed against. Other OpenAI-compatible providers are
  presets of the OpenAI-compatible engine, not engine classes.
- Text sent to the model is wrapped in `model_text()`, user-facing text in
  `_()`.

## Tests

`tests/run_tests.sh` runs the suite with the flatpak calibre; a file name
and a method pattern narrow it (`tests/run_tests.sh test_novel.py chunk`).
`tests/run_tests.sh --live` also sends five paragraphs to the engine
configured in calibre, with the user's key: only on request.

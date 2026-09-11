# Changelog

## v1.0.0

The first release under its own name. Novel Translator is a fork of
[Ebook Translator](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin)
that keeps one thing, chapter-by-chapter translation with a language model,
and drops everything else; the history before this release is that
project's.

**What it is**

* One mode. The toolbar button opens the translation directly.
* Four engines: OpenRouter, the default; one OpenAI-compatible engine with
  a provider to pick (OpenAI, DeepSeek, Groq, Mistral, Together AI,
  Fireworks AI, xAI, Moonshot AI, Azure OpenAI, Ollama, LM Studio, or a
  custom endpoint); Claude; Gemini.
* Its own name, settings file and cache directory, so it installs next to
  the original plugin without touching it.

**Fixed on the way**

* Chunks collapsed to 200 tokens under a model reply limit: the reserve for
  the running context was subtracted twice.
* An empty marker was stored as an empty translation and blanked the
  paragraph.
* Paragraphs the retries never brought back were lost for good: the chapter
  was marked done and a resume skipped past them. They get one more pass in
  smaller chunks, and what is still missing stops the run so a resume asks
  again.
* The front matter (title page, dedication, part dividers) was dropped, and
  a book translated from the window came out with its title, contents and
  metadata untranslated.
* Claude sent every request with a hardcoded 4096-token reply limit, so a
  chunk sized at 16000 tokens came back cut in half; its stream loop never
  ended on a closed connection.
* Closing the window left the translation running in the background with
  nothing to cancel it from.
* "Re-run all" did nothing.
* The model listing of OpenAI-compatible gateways with a path prefix
  answered 404.
* An API key was swapped because a traceback frame sat at line 401; a title
  typed in Japanese failed every OpenRouter request.

**Requirements**

calibre 7.0 or later.

# Changelog

## v1.0.1

**Translations checked before they are kept**

* Every translation a reply brings back is checked against its paragraph
  before it is stored, and the ones that cannot be right are asked for
  again with the missing ones. The number a model puts on a translation
  is the only thing that pairs it with its paragraph: a model that skips
  one paragraph and numbers on from there files everything after it under
  the wrong number, and a run of *Murder Imperial* on DeepSeek V4 Flash,
  served by the cheapest provider OpenRouter had for it, came back with
  whole chapters shifted, shortened with "[…]" and duplicated, all of it
  stored as good. The same chunk sent to another provider of the same
  model came back complete and in order. The checks cost no request: an
  ellipsis in place of text, placeholders unlike the source, the same
  text under two numbers, a line that opens as dialogue where the source
  does not, a length out of proportion with the rest of the reply. On by
  default (`novel_verify_alignment`).
* A retry that came back with fewer paragraphs than asked, numbered from
  1, was matched to the request by order, a guess that put translations
  under the wrong paragraphs. Only a reply that covers the whole request
  is matched that way now.
* The prompts say it too: never number on from a skipped paragraph, never
  shorten a paragraph with an ellipsis, copy the numbers of a retry as
  given.

**A log that shows what the model did**

* A reply that covered fewer paragraphs than asked is logged with the
  numbers it kept and its first characters (`novel_log_reply_excerpt`,
  300 by default), which is what tells a refusal from a reply cut short
  or from a model numbering its own way, and with the id the gateway
  gave the reply, which is what its record is looked up by afterwards.
* The reply line names the provider that served it, when the gateway says
  (OpenRouter does), and why the model stopped when it was not because it
  had finished: "length" is the output limit, a filter name is a filter.

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

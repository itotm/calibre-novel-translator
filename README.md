__English__ · [简体中文](README.zh-CN.md)

---

# Ebook Translator (Novel) — a Calibre plugin

![Ebook Translator Calibre Plugin](images/logo.png)

A Calibre plugin to translate ebooks into a specified language.

> **This repository is a fork** of
> [bookfere/Ebook-Translator-Calibre-Plugin](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin).
> The plugin is upstream's work and all the credit for it belongs there; this
> repository only adds what is listed below. It registers under its own plugin
> name, configuration file and cache directory, so it installs **side by side**
> with the official plugin without either one touching the other's state.

![Translation illustration](images/sample-en.png)

---

## What this fork adds

### Novel Mode

A third translation mode, next to Advanced Mode and Batch Mode, for long-form
narrative. It works chapter by chapter instead of paragraph by paragraph, and
keeps the model informed about the book so far.

It comes from
[PR #590](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/pull/590)
by [Simone Norcini (BiG86)](https://github.com/BiG86), still open upstream, and
was extended here.

**How it works**

* A **running summary** and a **dynamic glossary** of characters, places and
  objects are rebuilt after every chapter and carried into the requests that
  follow, keeping names, pronouns, register and terminology consistent over a
  whole book.
* Chapters are split under a **dual cap**, a token budget *and* a paragraph
  count, so short paragraphs (dialogue, lists) cannot make the model lose the
  alignment markers. A **sliding overlap** replays the last few translated
  paragraphs as context that is not to be translated again.
* **Structured JSON output** is used where the engine supports it, so paragraph
  alignment is enforced by the server rather than by prompt discipline, with a
  text-marker path as fallback.
* Chapter boundaries come from TOC level 1, TOC level 2 (anthologies whose
  level-1 entry is a whole novel) or one XHTML file per chapter, with a
  front-matter filter that keeps cover and title pages out of the narrative.
* Its own dialog shows the chapter list with per-chapter status and tabs for
  the summaries, the glossary and the log.
* Work is written to the translation cache as it is produced, so an interrupted
  run **resumes** — by chapter, and inside a chapter by paragraph, so the chunks
  that did finish are not paid for twice. The final ebook is built from the
  cache with no further model calls.

**What it costs**

Everything below is a setting with a sensible default, in *Preferences →
Engine → Novel Mode*.

* The summary and the glossary of a chapter are asked for in **one request
  instead of two** — both read the chapter that was just translated — and are
  skipped entirely on the last chapter and on short front and back matter,
  where nothing ever reads them.
* A prompt carries **only the glossary entries the chapter mentions**, capped
  per prompt. A glossary that keeps growing otherwise costs input tokens on
  every request, and invites the model to copy back the list of names it was
  told to skip.
* Reasoning is turned **off for the summary and glossary calls**, which are not
  reasoning tasks, their replies are capped, and a summary longer than expected
  is truncated before it is stored — it would otherwise be re-read in every
  later prompt.
* The chunk size is capped by **what the model can actually write**. Reading
  room and writing room are unrelated: context windows run to hundreds of
  thousands of tokens while reply limits start at 4096, and a chunk the model
  cannot finish is answered half-way and asked for again. The figure is read
  from the provider's model listing and shown next to the model in the settings.
* The parts of a request that never change are sent first so a provider's
  **prompt cache** can serve them, with an explicit cache breakpoint on Claude.
* Starting a translation no longer converts the ebook a second time, and
  translated paragraphs are written to the cache in one transaction per chunk.

### OpenRouter engine

[OpenRouter](https://openrouter.ai) proxies hundreds of models behind one
OpenAI-compatible endpoint. The engine inherits from the ChatGPT one and adds
what is specific to the gateway. Every setting defaults to a neutral value that
is simply left out of the request, because OpenRouter deliberately does not
substitute defaults for absent parameters.

| Group | Settings |
|---|---|
| Reasoning | `effort` (default `none`), `max_tokens` budget, `exclude` |
| Sampling ¹ | `top_k`, `min_p`, `top_a` |
| Penalties ¹ | `frequency_penalty`, `presence_penalty`, `repetition_penalty` |
| Limits | `max_tokens`, `seed` |
| Provider routing | `only`, `order`, `ignore`, `quantizations`, `sort`, `data_collection`, `allow_fallbacks`, `require_parameters`, `zdr` |
| Attribution | `HTTP-Referer`, `X-Title` |
| Escape hatches | extra request headers and extra body fields, as JSON |

¹ Hidden behind an *Advanced parameters* checkbox: specialist knobs a
book-length translation has no use for.

The escape hatches are merged into the request last, so anything the UI does
not expose (`logit_bias`, `stop`, `transforms`, `plugins`, …) can still be
sent; malformed JSON is flagged in the settings dialog and ignored at request
time rather than breaking a translation.

The model listing also reports, for every model, the context window, the
longest reply it will write, whether it honours a JSON schema and which
parameters it accepts. The first three are shown under the model in the
settings, the reply limit is what Novel Mode sizes its chunks against, and the
last one decides what the request carries.

### Engine settings

* **Reasoning is a preference**, not a hardcoded value, for ChatGPT, Azure
  ChatGPT, DeepSeek and any custom OpenAI-compatible endpoint. *Default* omits
  the field, which is what plain OpenAI models expect; `none` suppresses the
  reasoning tokens some local servers emit unprompted (Ollama with Gemma); the
  rest spend reasoning tokens on purpose.
* **A low temperature on OpenRouter**, 0.3. Translation is a high-certainty
  task where diversity is noise — measured over six temperatures, quality falls
  as it rises — and a low value also keeps a model on the requested JSON shape
  where the provider does not enforce it. Every engine that already existed
  keeps the default it ships with upstream.
* **Only the parameters the model accepts are sent.** A fifth of the OpenRouter
  catalogue takes no `temperature` at all, and a parameter a model cannot take
  is at best ignored and at worst, with `require_parameters` on, leaves the
  request with no provider to route to. The same listing decides whether a JSON
  schema can be asked for at all. Anything the listing does not mention can
  still be forced through the extra body field.
* **Prompts are plain prose.** No placeholder is mandatory in the Novel Mode
  prompt fields: what a template does not place is appended under its own
  label, and a stray brace no longer raises. The scaffolding built around a
  prompt is kept out of the translation catalogs, so translating the interface
  cannot hand the model instructions in one language wrapped around a prompt
  written in another.

### Fixes

* The structured-output path forced `stream: true` in the request body without
  telling the engine, so an engine with streaming disabled tried to parse a raw
  SSE payload as JSON.
* OpenRouter reports some upstream provider failures inside an HTTP 200 body;
  the error message is surfaced instead of an opaque parsing error.
* Test expectations left behind by the Novel Mode branch were brought back in
  line with the code.

---

## Installing side by side with the official plugin

The fork registers as **Ebook Translator (Novel)** and keeps its own state, so
nothing is shared with an installation of the official plugin:

| | Official | This fork |
|---|---|---|
| Plugin name | Ebook Translator | Ebook Translator (Novel) |
| Import name | `ebook_translator` | `ebook_translator_novel` |
| Settings | `plugins/ebook_translator.json` | `plugins/ebook_translator_novel.json` |
| Cache directory | `…EbookTranslator` | `…EbookTranslator.Novel` |

This fork carries no CI: the release archive is built locally, by the same
script used for day-to-day installs.

```sh
./build_plugin.sh
```

It writes `../ebook-translator-novel_v<version>.zip`, checks the archive is
installable before handing it over, and prints the `calibre-customize -a …`
line to run. In the GUI the equivalent is *Preferences → Plugins → Load plugin
from file*.

Because the two plugins keep separate settings, engine API keys have to be
entered again in the fork the first time you use it.

---

## Features

* Support "Novel Mode" to better use LLM capabilities preserving a dynamic context.
* Support both "Advanced Mode" and "Batch Mode" for different usage situations.
* Support languages supported by the selected translation engine (e.g. Google Translate supports 134 languages)
* Support multiple translation engines, including Google Translate, ChatGPT, Gemini, DeepL, OpenRouter, etc.
* Support custom translation engines (you can configure to parse response in JSON or XML format)
* Support all ebook formats supported by Calibre (48 input formats, 20 output formats), as well as additional formats such as .srt
* Support to translate more than one ebooks. The translation process of each book is carried out simultaneously without affecting one another
* Support caching translated content, with no need to re-translate after request failure or network interruption
* Provide a large number of customization settings, such as saving translated ebooks to Calibre library or designated location

---

## Manual

The upstream documentation applies to this fork as well, except for the
fork-specific settings described above.

* [Tutorial](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/wiki#a-brief-tour)
* [Installation](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/wiki/English#installation)
* [Usage](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/wiki/English#usage)
* [Settings](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/wiki/English#settings)

---

## Links

* [This fork](https://github.com/itotm/calibre-plugin-ebook-translator)
* [Upstream project](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin)
* [Upstream homepage](https://translator.bookfere.com)
* [MobileRead](https://www.mobileread.com/forums/showthread.php?t=353052)
* [Contributing](CONTRIBUTING.md)
* [Donate to the upstream author](https://www.paypal.com/paypalme/bookfere)

---

## License

[GNU General Public License v3.0](LICENSE)

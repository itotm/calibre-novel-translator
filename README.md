__English__ · [简体中文](README.zh-CN.md)

---

# Ebook Translator (Novel) — a Calibre plugin

![Ebook Translator Calibre Plugin](images/logo.png)

A Calibre plugin to translate ebook into a specified language.

> **This is a fork** of [bookfere/Ebook-Translator-Calibre-Plugin](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin).
> All the credit for the plugin belongs upstream; this repository only adds the
> features listed below. It ships under its own plugin name, configuration file
> and cache directory, so it can be installed **side by side** with the official
> plugin without either one disturbing the other.

![Translation illustration](images/sample-en.png)

---

## What this fork adds

### Novel Mode

A third translation mode, next to Advanced Mode and Batch Mode, built for
long-form narrative content. Instead of sending one request per paragraph, it
works chapter by chapter and keeps the model informed about the book so far:

* A **running summary** and a **dynamic glossary** of characters, places and
  objects are rebuilt after every chapter and injected into each subsequent
  request, which keeps names, pronouns, register and terminology consistent
  across the whole book.
* Chapters are split into chunks under a **dual cap** — a token budget *and* a
  paragraph count — so short paragraphs (dialogue, lists) cannot make the model
  lose track of the alignment markers.
* A **sliding-window overlap** replays the last few translated paragraphs as
  already-translated context, preserving dialogue threads across chunk
  boundaries.
* **Structured JSON output** is used when the engine supports it, so paragraph
  alignment is enforced by the server rather than by prompt discipline.
* Progress is persisted to the translation cache after every chapter, so an
  interrupted run **resumes** where it stopped, and the final ebook is built
  from the cache without further LLM calls.
* Chapter boundaries can be taken from TOC level 1, TOC level 2 (for
  anthologies where a level-1 entry is a whole novel) or one XHTML file per
  chapter, with a front-matter filter that keeps cover and title pages out of
  the narrative.

Novel Mode comes from
[PR #590](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/pull/590)
by [Simone Norcini (BiG86)](https://github.com/BiG86), merged here while it is
still open upstream.

### OpenRouter translation engine

A built-in engine for [OpenRouter](https://openrouter.ai), which proxies
hundreds of models behind one OpenAI-compatible endpoint. Beyond the usual
prompt / model / sampling settings, the engine exposes the parameters that are
specific to the gateway, each defaulting to a neutral value that is simply left
out of the request so the upstream provider keeps applying its own default:

| Group | Settings |
|---|---|
| Reasoning | `effort` (default `minimal`), `max_tokens` budget, `exclude` |
| Sampling | `top_k`, `min_p`, `top_a` |
| Penalties | `frequency_penalty`, `presence_penalty`, `repetition_penalty` |
| Limits | `max_tokens`, `seed` |
| Provider routing | `only`, `order`, `ignore`, `quantizations`, `sort`, `data_collection`, `allow_fallbacks`, `require_parameters`, `zdr` |
| Attribution | `HTTP-Referer`, `X-Title` |
| Escape hatches | extra request headers and extra body fields, as JSON |

The two escape hatches are merged into the request last, so anything the UI
does not expose (`logit_bias`, `stop`, `transforms`, `plugins`, cache control,
custom session headers, …) can still be sent. Malformed JSON is flagged in the
settings dialog and ignored at request time rather than breaking a translation.

### Configurable reasoning for OpenAI-compatible engines

`reasoning_effort` is now an engine preference for ChatGPT, Azure ChatGPT,
DeepSeek and any custom OpenAI-compatible endpoint, instead of being hardcoded.
Leaving it at *Default* omits the field entirely, which is what plain OpenAI
models expect; `none` suppresses the reasoning tokens some local servers emit
before every answer (Ollama with Gemma, for instance); the remaining levels
spend reasoning tokens on purpose. Novel Mode honors the setting too, so a
reasoning model can keep thinking while still being forced to answer in JSON.

### Fixes carried by this fork

* The structured-output path forced `stream: true` in the request body without
  telling the engine, so an engine configured with streaming disabled tried to
  parse a raw SSE payload as JSON.
* OpenRouter reports some upstream provider failures inside an HTTP 200 body;
  the error message is now surfaced instead of an opaque parsing error.
* Test expectations left behind by the Novel Mode branch were brought back in
  line with the code (`keepalive` request argument, structured-output payload).

---

## Installing side by side with the official plugin

The fork registers itself as **Ebook Translator (Novel)** and keeps its own
state, so nothing is shared with an installation of the official plugin:

| | Official | This fork |
|---|---|---|
| Plugin name | Ebook Translator | Ebook Translator (Novel) |
| Import name | `ebook_translator` | `ebook_translator_novel` |
| Settings | `plugins/ebook_translator.json` | `plugins/ebook_translator_novel.json` |
| Cache directory | `…EbookTranslator` | `…EbookTranslator.Novel` |

Build the installable archive and add it to Calibre:

```sh
./build_plugin.sh
calibre-customize -a ../ebook-translator-novel.zip
```

Or, in the GUI: *Preferences → Plugins → Load plugin from file*.

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

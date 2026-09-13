# Novel Translator

![Novel Translator](images/logo.png)

A calibre plugin that translates a novel with a language model, chapter by
chapter, and keeps the model informed about the book as it goes: a running
summary of the story so far, a glossary of the names it has met, a brief on
how the author writes. The result reads like one book translated by one
translator, not like a thousand paragraphs translated by a thousand.

It talks to [OpenRouter](https://openrouter.ai) out of the box, and to any
provider that speaks the OpenAI chat API, to Claude and to Gemini.

---

## How it works

**Chapter by chapter, in order.** The book is split into chapters from its
table of contents (level 1, level 2 for anthologies whose level-1 entry is a
whole novel, or one XHTML file per chapter). Chapters are translated one
after the other, never in parallel, because each one is translated with
what the previous ones taught.

**A running summary and a dynamic glossary.** After every chapter one
request asks for a summary of it and for the new names it introduced:
characters, places, objects, how each is translated. Both are carried into
the requests that follow, so names, pronouns, register and terminology stay
consistent over the whole book. A prompt carries only the glossary entries
the chapter mentions. The same request says whether the chapter is part of
the story at all: a copyright page, a list of the author's other books, a
preface or a note is translated but leaves nothing in the running context.

**The author is asked about once.** Before the first chapter one request
asks how this author habitually writes, the register, the texture of the
sentences, the use of irony, dialect or period language, and the answer is
repeated in the prompt of every chapter. The brief is about the author in
general, never about the book: the model is told to say nothing of its
plot, setting or period, which the translation reads off the text. A
model that knows nothing reliable about the author says so, and the
Author tab of the window says so too, rather than carry a brief the
model made up.

**Dialogue punctuated like the source.** The quotation marks of the source
are read once, chapter by chapter, and the convention most chapters use is
stated in every request, nested quotations and dash dialogue included, so
the same book does not come back with guillemets in one chapter and
quotation marks in the next. A preface that quotes at length cannot
outvote the novel. You can also prescribe a convention outright, from
guillemets to corner brackets to dash dialogue, or write a rule of your
own.

**Chunks the model can finish.** A chapter goes to the model in chunks
sized by a token budget, a paragraph count and, where the provider publishes
it, the longest reply the model can write; each request asks for the room
its reply needs, so a provider's default limit does not cut it short.
Structured JSON output keeps the paragraphs aligned where the engine
supports it, numbered text markers elsewhere. Missing paragraphs are asked
for again, then once more in smaller chunks; what still comes back missing
stops the run so that a resume picks it up, instead of leaving a hole in
the book.

**Resumable, and built from the cache.** Everything is written to the
translation cache as it is produced, so an interrupted run resumes by
chapter and, inside a chapter, by paragraph. The metadata, the table of
contents and the front matter (title page, dedication, part dividers) are
translated too, apart from the narrative. When every chapter is done the
ebook is built from the cache with no further requests.

The window shows the chapter list with its status, a progress bar, and tabs
for the summaries, the glossary, the author brief and the log.

---

## Providers

| Engine | Notes |
|---|---|
| **OpenRouter** | The default. One key, hundreds of models. The model listing carries each model's context window, reply limit and accepted parameters; the plugin sizes its requests against them and sends only the parameters the model takes. Reasoning, provider routing, sampling and two escape hatches (extra headers, extra body) are settings. |
| **OpenAI-compatible** | One engine, a provider to pick: OpenAI, DeepSeek, Groq, Mistral, Together AI, Fireworks AI, xAI, Moonshot AI, Azure OpenAI, Ollama and LM Studio on this machine, or any custom endpoint. The preset fills in the endpoint, the key hint and a default model; keys and models are kept per provider. |
| **Claude** | Anthropic's Messages API, with prompt caching. The reply limit is a setting, sized for the model by default. |
| **Gemini** | Google's API, with structured output and Google Search grounding. |

Every behaviour is a setting with a sensible default, under *Preferences →
Plugins → Novel Translator*, or from the plugin's own menu.

---

## Installation

Novel Translator needs calibre 7.0 or later.

Build the plugin archive from a checkout of this repository:

```sh
./build_plugin.sh
```

It writes `../novel-translator_v<version>.zip`, checks that the archive is
installable and prints the command that installs it:

```sh
calibre-customize -a ../novel-translator_v1.1.1.zip
```

In the GUI the equivalent is *Preferences → Plugins → Load plugin from
file*. Restart calibre: the plugin puts its button on the main toolbar
the first time it runs. If you take it off later it stays off; it is
under *Preferences → Toolbars & menus* whenever you want it back.

---

## Usage

1. Open the settings from the plugin's menu, choose the engine, paste the
   API key, pick a model and save. OpenRouter is preselected.
2. Select one book in the library and click the plugin's button.
3. Choose the input and output formats and the languages, then *Start*.
4. The window prepares the book, lists its chapters and waits. *Start /
   Resume* runs the translation; *Cancel* stops it at once, cutting short
   the request in flight, and everything done so far is kept. The log of
   a run is kept with the book and shown again when the window reopens.
5. When every chapter is done, *Build translated ebook* writes the
   translated book into the library (or to the folder set in the General
   tab). *Re-run all* translates the whole book again while keeping the
   summaries, the glossary and the author brief; *Reset context* discards
   those too.

The cache manager, in the plugin's menu, lists the books in the cache and
lets you move, inspect or delete them. The cache lives under calibre's own
cache directory and survives calibre being closed, so a book can be
finished across several sessions.

---

## Settings worth knowing

*General*: where the output goes, preferred formats, proxy, cache, log and
notifications.

*Engine*: the engine, its key, the languages, the request timing, the model
and the sampling, then a section for the engine's own options and the
**Novel Mode** section, which holds among others:

* chapter detection and the front-matter threshold;
* the chunk caps: tokens, paragraphs, overlap, and whether the reply limit
  of the model caps them;
* structured output: automatic, off, or forced;
* the context budget, the summary size, the glossary caps and whether the
  prompt carries only the glossary entries the chapter uses;
* whether the summary and the glossary are asked for in one request,
  whether the last chapter skips them, and whether those calls may spend
  reasoning tokens;
* what to do with paragraphs the model never returns;
* whether the author brief is asked for, and how dialogue is punctuated;
* the translation prompt, plain prose with no mandatory placeholder. The
  summary, glossary and author-brief prompts are the plugin's own: they
  ask for a shape the code parses, and are not settings.

*Content*: where the translation sits relative to the original (alone by
default, or below, above or beside it), colours, CSS rules for elements
to prioritise, ignore or keep, and the metadata written into the output;
by default the output book carries the target language as its language.

---

## Development

```sh
tests/run_tests.sh                 # the whole suite, on the working tree
tests/run_tests.sh test_novel.py   # one module
python3 translations/update.py     # refresh the translation catalogs
python3 images/artwork.py          # redraw the icon and the logo
```

The test runner needs a calibre: `calibre-debug` on the PATH, the one
named in `$CALIBRE_DEBUG`, or the `com.calibre_ebook.calibre` flatpak.
See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Credits

Novel Translator is a fork of
[Ebook Translator](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin)
by bookfere.com, whose code is still most of what runs here: the ebook
handling, the caches, the settings dialog and the engines all come from
there. Novel Mode, the idea this plugin is built around, originated in
[pull request #590](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/pull/590)
to that project by [Simone Norcini (BiG86)](https://github.com/BiG86).
Thank you both.

## License

[GNU General Public License v3.0](LICENSE)

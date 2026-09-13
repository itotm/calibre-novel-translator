# Changelog

## v1.1.1

* The author brief is asked of the model alone, about the author alone.
  The web search is gone: the pages it found were about the plot of the
  author's other books, the model declined with them in front of it or
  placed the book in the wrong century, and every search was billed. The
  request now asks how the author habitually writes, tells the model to say
  nothing of this book's plot, setting, period or series, to make sure it
  is describing this author and no other, and to decline rather than fill
  the gaps with what such novels are usually like. A brief that used to
  come back empty on every book by Paul Doherty comes back on all of them.
* Before the brief, the model is asked whether it can name real books by
  this author (`novel_author_check`, one small request). Asked for a brief
  on an invented name, DeepSeek V4 Flash wrote a confident one about
  nobody; asked first whether it knows the author, it says no and the
  translation goes on without a brief. The titles it names go into the
  request for the brief, which keeps it about this author and not another
  of the same name.

## v1.1.0

**A run that defends itself**

* A provider that keeps answering unreliably -- slipped numbers, or less
  than half of what was asked -- is left out for the rest of the run after
  a set number of failures (`novel_provider_failures_before_exclusion`, 2).
  The setting on disk is not touched: a provider bad with one model today
  is not bad with every model always. OpenRouter acts on it; the other
  engines have no providers to choose between.
* A reply whose numbers slipped is read as it was meant: when the
  translations make sense a few places further on and nowhere else, they
  are moved back under their paragraph instead of asked for again
  (`novel_realign_shifted_replies`). It is logged as a warning either way.
* A retry asks for the missing paragraphs in two halves rather than all at
  once (`novel_retry_split`): what a model could not manage at one size it
  seldom manages again at the same size.
* Chunks of 50 paragraphs by default, down from 75.
* Chunks of a chapter in flight together, on request
  (`novel_parallel_chunks`, 1 by default): each runs on its own copy of the
  engine, and a chapter of three chunks takes about as long as its longest
  one. The context around each chunk is then the source text before and
  after it (`novel_chunk_context`, `novel_source_context_paragraphs`),
  which needs nothing to have been translated yet and also shows what
  comes next; the same context can be chosen for the sequential pipeline.
  Chapters stay one after the other. A cancel reaches every request in
  flight. Chunks in flight are cut to about the same size
  (`novel_balanced_chunks`): the chapter takes as long as its longest one.
* The author brief with web search on came back as "no information" every
  time with DeepSeek V4 Flash: the pages a search finds are about the plot,
  and a model told to rely on them said it knew nothing about the prose.
  The request now lets the model add what it knows where the sources are
  silent, and when it still declines with the results in front of it, it is
  asked once more from what it knows on its own, the cheap call that used to
  produce the brief.
* The progress bar follows the text translated, not the chapters done:
  the pages before the story are chapters too, and counted as such the bar
  stood at a third before the first real chapter. It moves after every
  chunk.

**Fewer requests**

* The summary and the glossary of a chapter are asked from its source
  before it is translated (`novel_context_timing`, 'before'): half the
  tokens of sending source and translation together, and the names a
  chapter introduces are rendered the same way in its first chunk and in
  its last, because the glossary is already in the prompt. 'after' keeps
  the old behaviour.
* Chapters whose title says what they are -- copyright, praise, about the
  author, dedication, contents -- are translated without the request that
  used to establish that they are not part of the story
  (`novel_front_matter_titles`).
* The list of the author's other books stays in the original language
  (`novel_untranslated_titles`): those are titles the reader will look for
  as they were published.

**What it costs, and who served it**

* Every reply line in the log says the tokens as the provider counted them
  and, through OpenRouter, what the request cost (OpenRouter setting
  "usage accounting", on by default).
* A Report tab in the Novel window, rewritten after every chapter and kept
  with the book: requests, tokens, cost and time per kind of request and in
  total, over every run on the book, with the total time of the runs; how
  much of the text sent had to be set aside, moved or asked for again; the
  providers that served it, how many of their replies were unreliable and
  what share of their text gave trouble, which were excluded; and advice on
  what to change. The same table closes the log of a run. The tabs now run
  Author, Summaries, Glossary, Log, Report.
* "Test the model" in the Novel Mode settings sends five paragraphs
  through the translation path with the settings as they are and shows the
  provider, the finish reason, the cost, how many paragraphs came back and
  whether they are the right ones. A minute and a fraction of a cent,
  before a book of a hundred requests.
* `tests/run_tests.sh --live` does the same from the command line, after
  the unit tests, with the engine configured in calibre. Never implied.

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

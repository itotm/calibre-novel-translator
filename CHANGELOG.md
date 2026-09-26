# Changelog

## v1.3.3

**An author brief about this author, not about the genre**

* The request for the brief carries about 2000 words of the book, half
  from its opening and half from its middle, and asks for the manner of
  the series the book belongs to: the narrator, the voices of the
  recurring characters, the humour and whom it targets, the oaths,
  titles and forms of address, and what they call for in the target
  language, formal and familiar address included. Asked for the author's
  manner in general, every model wrote much the same brief for every
  writer of historical mysteries, and GPT-6 Luna Pro filled it with "it
  varies" and "no reliable information on this". Asked with the title
  alone, models put a Paul Doherty novel in the wrong one of his series
  or credited it with another writer's detective; with the text in front
  of them DeepSeek V4 Flash, V4.1 Flash and GPT-6 Luna Pro all named the
  right series, and quoted the book's own titles and oaths.
* The brief leaves out what the model cannot state with confidence
  instead of saying so, leaves the names to the glossary, and is 200 to
  350 words.
* *Book text for the brief* in the settings sets how many words go with
  the request; 0 sends none, which is how the brief was asked for
  until now.

## v1.3.2

**A tidier, more compact interface**

* Every window follows one set of measures, after the KDE guidelines and
  kept compact: 8 pixels around the content of a window or a tab page, 6
  between the widgets, nothing added by the containers in between.
  Settings, the list of a book's translations, the translation window,
  the comparison, the cache manager and About were each spaced their own
  way.
* Sections have bold headings instead of frames: the settings were
  frames inside a scroll area inside a tab inside a window. The tabs of
  the translation window lie flat on it. *Save* in the settings sits at
  the bottom right, where a dialog's actions go, instead of across the
  whole width.
* Buttons carry icons, from calibre's own set, so they follow the icon
  theme chosen in calibre.
* Hints, figures and notes are in the colour the theme gives secondary
  text, and every other colour works on a dark theme too: the warning
  about an edited brief or glossary is a message box in the manner of
  KDE's, as is the note on why there is no author brief; the chapter
  marks and the untranslated paragraphs use the Breeze colours; the
  translation position preview and the code blocks in About no longer
  come out black on dark grey or light grey on black.
* Tables read like KDE lists: alternate rows, no grid, compact rows. Long
  chapter titles are cut short with an ellipsis, and shown whole on
  hover, instead of scrolling the chapter list sideways.
* The preparation view shows the cover fitted into a box of its own,
  the title and the progress under it, and what preparing the book does
  beside it: a very wide or very narrow cover no longer stretches or
  squeezes that column.

**Fixes**

* A run that fails marks the chapter it stopped on as failed, and one
  that is cancelled puts it back to pending: the chapter kept the
  "running" mark while nothing ran.
* *Reset context* on a finished book turns *Re-run all* back into *Start /
  Resume* and the progress line back to nothing done.
* Closing the window while a run goes on, and answering the question
  after the run had ended by itself, left the window open with *Close*
  disabled; it closes.
* A glossary cell still being edited when the translation starts, or the
  window closes, is saved first. It could otherwise be written into the
  cache during the run, which keeps a glossary of its own.
* Typing in the *Author* tab of a book with nothing translated yet read
  the whole cache at every key.
* A new translation cannot be started while the plugin is still finding
  out whether its model has flex: started before the answer, with flex
  in the settings, a model without flex failed every request. A model
  without flex is never left on flex, not even when flex was picked by
  hand for the model chosen before. A flex endpoint only counts when the
  provider routing (*Provider: only*, *Provider: ignore*) lets requests
  reach it.
* The preview of the translation position keeps the colour of dimmed
  text when the theme makes it translucent; a section heading is never
  cut short in a narrow window; the About window's text is owned by the
  window, and its title grows with a font set in pixels too.

## v1.3.1

**The author brief and the glossary, in your hands; flex when there is flex**

* The *Author* and *Glossary* tabs of the translation window can be
  edited before the translation starts, while it is stopped, and when a
  translation is opened again; during a run they are read-only, as the
  run holds its own copy. Both are saved as you type.
* A brief written in the *Author* tab is used as it is and the model is
  not asked for one, even with "How the author writes" set to *Do not
  ask*. Left empty, the model is asked when the translation starts, as
  before. When the last run found no brief, the reason is shown above
  the field instead of in it.
* The *Glossary* tab has *Add entry* and *Remove*, and every cell can be
  edited. An entry written or corrected by hand is marked as yours: the
  model never changes its translation, and the glossary size limit never
  drops it.
* Editing either once some of the book is translated brings up a warning
  that what is already translated followed the previous version, so the
  book may not read consistently. It stays until the translation starts
  again.
* Enter in the glossary table edits the entry selected instead of
  starting the translation, and *Reset context* is disabled while a run
  is going.
* With OpenRouter, a new translation preselects the flex tier when its
  model has a flex endpoint, read off the model's endpoint listing, and
  says next to the tier whether it has one. A model without flex is
  never preselected on flex, even when the settings name it, since every
  request would fail. A tier picked by hand in the dialog stays. The
  engine setting *flex when the model has it*, on by default, turns the
  preselection off.

## v1.3.0

**Several translations of a book, to read, correct and compare**

* A book can be translated more than once, and every translation is kept:
  by another model, into another language, at another service tier. The
  plugin's button now opens the list of the translations of the selected
  book, with the model that made each one in bold, its tier, engine,
  language, how many chapters are done, how many paragraphs were
  corrected by hand and when it last changed. *Open* continues one, reads
  it or builds the book from it; *Delete* removes it; *Start new
  translation* makes another. Two translations of the same book can be
  open side by side.
* A new translation asks for one thing besides the formats and the
  languages: the model, searchable in the engine's listing and open to one
  it does not carry, with the model of the settings preselected. With
  OpenRouter the service tier goes with it. Everything else is what the
  settings say. The model and the tier are recorded with the translation
  and used whenever it runs again, whatever the settings name by then;
  what the listing says about the model (its reply limit, the parameters
  it takes) is recorded with it, so a model other than the one in the
  settings is not sized or filtered by that one's figures.
* The window of a translation has a *Text* tab, first: the paragraphs of
  the chapter chosen on the left, or of the whole book, original and
  translation side by side, with a search over both. The translation of
  the paragraph selected can be corrected and saved into the cache before
  the book is built, or built again. The search runs when asked, with
  *Search* or Enter, never while typing, and the whole book is only ever
  searched, never listed whole; a search showing more than a thousand
  paragraphs asks to be narrowed. The metadata, the table of contents
  and the front matter are an entry of their own at the top of the
  chapter list. Corrections wait for a run to end; a resume keeps them;
  *Re-run all* says how many it would replace before it does. The output
  format is now chosen next to *Build translated ebook*.
* Two or more translations of a book can be compared: select them in the
  list of the book, or in the cache manager, and *Compare*. The window
  shows the chapters of the book and, for each paragraph, the original
  and every translation side by side, each column named by its model
  (with the tier, and the language when they differ); the search looks
  through all of them. Any translation can be corrected there, and a
  paragraph copied from one into another, or to the clipboard; the
  table's menu copies a whole cell. The paragraphs are lined up by their
  text, not their position, so one extra paragraph in an extraction does
  not shift all the others. *Compare* is enabled only for translations of
  one book made from the same file of it, and a translation that a run
  is writing stays read-only. The Text tab of a translation window is
  the same view with one column.
* OpenRouter's service tiers (*Service tier* in the OpenRouter section,
  and per translation): *flex* is discounted and slower, *priority* costs
  more for faster service; the default leaves the field out. A flex
  request is given fifteen minutes before it is taken for a dead
  connection, and never falls back to the standard tier: with no flex
  capacity it fails, and a run that gives up says so. A priority request
  may fall back. The reply says which tier served it, which is the one
  billed: the log says it whenever flex or priority is involved, and the
  report counts the replies per tier.
* Corrections are not lost by accident. Leaving a paragraph, a chapter
  or the window with a correction not saved asks *Save*, *Discard* or
  *Cancel*: Enter saves and Esc stays. Clicking another cell of the same
  paragraph keeps what is typed. A paragraph saved from another window
  since it was shown is not overwritten without asking. A correction that
  cannot be saved because a run is writing that translation says so.
* A translation keeps what it was made with: the model, the service tier
  and, on the OpenAI-compatible engine, the provider, whose key, endpoint
  and model are taken from the ones kept for it even after the settings
  moved to another. A translation written before this version records
  them at its next run. With the cache turned off, a translation already
  in the cache is opened, compared and built from there, not from an
  empty temporary copy.
* A book built into a folder does not replace one built there before
  from another translation: it takes the model in its name. A build
  whose translation was deleted meanwhile stops instead of writing a book
  with nothing translated in it.
* The cache manager shows the model of every cache, and opens one with
  *Open* or a double click, as the list of a book does. Caches written before
  this version are listed with their book as they are, the model "not
  recorded" until they next run, and learn which book they belong to the
  first time they are opened, so a book whose files calibre moves (a new
  title or author) keeps its translations.

## v1.2.1

**A cancel that ends, not a window that freezes**

* Cancelling a chapter whose chunks were in flight together froze the
  window, and so did a chunk failing while the others were still reading.
  The copies of the engine reading the chunks were made by `copy.copy`
  once the list of copies existed, so each carried that same list, itself
  included; an abort that asked every copy to abort its copies in turn
  went round the list without end, swallowed a RecursionError at every
  turn and never came back, on the worker thread and, on Cancel, on the
  window's own. Each copy now closes its own response and nothing else,
  and a copy carries no list.
* A chunk whose request was cut short by that abort read the error as a
  passing one and asked again, twice, with the pauses in between, for a
  reply nobody was waiting for. A chunk in flight now stops when the
  chapter is being abandoned, the way it stops on a cancel.
* A provider that the settings pin the request to (*Provider: only*) is
  no longer excluded when it keeps answering unreliably: ignoring the
  one provider allowed left OpenRouter nothing to route to, and every
  request of the rest of the run failed with "All providers have been
  ignored". The run says so once and keeps using it; a pinned provider
  among others is taken off the list instead. The lookup of a
  provider's routing slug is made once per model, not once per engine
  copy under the lock that holds the other chunks up.
* The log of a run is written to the cache after every chapter, not only
  at the end: the run that froze the window took its log with it.
* Nothing is called "Novel Mode" any more, in the window, the settings
  (the section is now *Chapters and context*), the log or the code: the
  plugin does one thing, and there is no other mode for it to be set
  against. The credit for the idea stays.

## v1.2.0

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
* The summary and glossary reply may run to 8000 tokens, up from 4000,
  which cut a crowded chapter short; and one chapter adds at most 50 new
  glossary entries (`novel_glossary_chapter_max_entries`): the request
  states it as a hard limit, the JSON schema enforces it where the server
  honours schemas, and a longer list is cut anyway. Told "at most forty" in
  prose, the model listed ninety-eight.
* A translation set aside by the checks is written into the log, source
  and translation, before it is asked for again: the retry's answer was all
  the cache would ever show of it.

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

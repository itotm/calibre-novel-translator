# Contributing

Novel Translator is a calibre plugin. If you have not written one before,
calibre's own documentation is the place to start: [setting up a
development environment](https://manual.calibre-ebook.com/develop.html),
[writing your own plugin](https://manual.calibre-ebook.com/creating_plugins.html)
and the [plugin API](https://manual.calibre-ebook.com/plugins.html).

## Running the tests

```sh
tests/run_tests.sh                      # everything
tests/run_tests.sh test_novel.py        # one module
tests/run_tests.sh test_novel.py chunk  # methods whose name contains "chunk"
```

The runner makes the working tree importable under the plugin's name, so
nothing has to be built or installed first. It looks for `calibre-debug`
on the PATH, then in `$CALIBRE_DEBUG`, then in the
`com.calibre_ebook.calibre` flatpak.

## Building

```sh
./build_plugin.sh
```

writes the installable archive next to the checkout and prints the
`calibre-customize` line that installs it.

## Strings and artwork

User-visible strings go through `_()`. After adding or changing any, run
`python3 translations/update.py`: it refreshes `translations/message.pot`,
carries the existing translations over and compiles the catalogs. The icon
and the logo are drawn by `python3 images/artwork.py`.

## What to keep in mind

* The pipeline is strictly sequential: chapter N+1 is translated with what
  chapter N taught, so requests are never made in parallel.
* Every behaviour is a setting with a sensible default, never a hardcoded
  value.
* The model's replies carry only the translation keyed by its paragraph
  number, never the source text.

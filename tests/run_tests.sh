#!/usr/bin/env bash
# tests/run_tests.sh - Run the test suite against the working tree.
#
# The tests import the plugin as ``calibre_plugins.<import name>``, which
# calibre only resolves for an installed zip. This script makes the
# working tree importable under that name instead, so the tests run on
# the code as it is, without building or installing anything.
#
# Usage:
#   tests/run_tests.sh                    # the whole suite
#   tests/run_tests.sh test_novel.py      # one module, by file name
#   tests/run_tests.sh test_novel.py chunk  # ...and a method name pattern
#   tests/run_tests.sh --live             # the suite, then one real request
#
# ``--live`` is never implied: after the unit tests pass it sends five
# paragraphs to the engine and the model configured in calibre, with the
# key stored there, and prints what came back (see lib.novel.probe_engine).
# It costs a fraction of a cent and a minute, and it is the only test that
# sees what a provider actually does.
#
# It needs a calibre. In order of preference: the ``calibre-debug`` on
# PATH, the one named in ``$CALIBRE_DEBUG``, or the flatpak
# ``com.calibre_ebook.calibre``.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

IMPORT_NAME=$(find . -maxdepth 1 -name 'plugin-import-name-*.txt' \
    -printf '%f\n' | sed 's/^plugin-import-name-//; s/\.txt$//')
[ -n "$IMPORT_NAME" ] || fail "no plugin-import-name-*.txt in $ROOT"

WORK="${TMPDIR:-/tmp}/calibre-plugin-tests-$$"
mkdir -p "$WORK/calibre_plugins"
trap 'rm -rf "$WORK"' EXIT
ln -s "$ROOT" "$WORK/calibre_plugins/$IMPORT_NAME"

LIVE=''
ARGS=()
for argument in "$@"; do
    if [ "$argument" = "--live" ]; then
        LIVE='live-probe'
    else
        ARGS+=("$argument")
    fi
done
set -- "${ARGS[@]}" $LIVE

cat > "$WORK/run.py" <<PY
import builtins
import sys
import unittest
from pathlib import Path
from importlib import import_module

# What the zip loader injects into every plugin module.
builtins.load_translations = lambda: None

# calibre resolves ``calibre_plugins.*`` to the installed zips through
# its own finder, so an installed copy of this plugin would shadow the
# working tree. A finder ahead of it points this one name at the tree.
import importlib.abc
import importlib.machinery


class WorkingTreeFinder(importlib.abc.MetaPathFinder):
    package = 'calibre_plugins.$IMPORT_NAME'

    def find_spec(self, name, path, target=None):
        if name != self.package and not name.startswith(self.package + '.'):
            return None
        if not path:  # calibre_plugins.__path__ is an empty list
            path = [sys.argv[1]]
        return importlib.machinery.PathFinder.find_spec(name, path, target)


import calibre_plugins  # noqa: E402  the namespace calibre reserves for plugins
sys.meta_path.insert(0, WorkingTreeFinder())
# calibre-debug has already imported the installed copy while loading
# its plugins; forget it so the imports below go through the finder.
for loaded in [n for n in sys.modules if n == WorkingTreeFinder.package
               or n.startswith(WorkingTreeFinder.package + '.')]:
    del sys.modules[loaded]

# The shell turns --live into this token: calibre-debug would take the
# flag for one of its own.
live = 'live-probe' in sys.argv
argv = [a for a in sys.argv if a != 'live-probe']
root = Path(argv[2])
filenames = [Path(a).name for a in argv[3:] if a.endswith('.py')]
methods = [a for a in argv[3:] if not a.endswith('.py')]

patterns = filenames or ['test_*.py']
suite = unittest.TestSuite()
for pattern in patterns:
    for path in sorted((root / 'tests').rglob(pattern)):
        module = '.'.join(path.relative_to(root).with_suffix('').parts)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(
            import_module('calibre_plugins.$IMPORT_NAME.' + module)))
if methods:
    filtered = unittest.TestSuite()
    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            elif any(m in t.id() for m in methods):
                filtered.addTest(t)
    walk(suite)
    suite = filtered

result = unittest.TextTestRunner(verbosity=1).run(suite)
if not result.wasSuccessful():
    sys.exit(1)
if live:
    # One real request through the configured engine, on request only.
    package = 'calibre_plugins.$IMPORT_NAME'
    translation = import_module(package + '.lib.translation')
    novel = import_module(package + '.lib.novel')
    conversion = import_module(package + '.lib.conversion')
    engine_class = translation.get_engine_class()
    engine = translation.get_translator(engine_class)
    engine_config = engine_class.config or {}
    engine.set_source_lang(engine_config.get('source_lang') or 'English')
    engine.set_target_lang(engine_config.get('target_lang') or 'Italian')
    print()
    print('Live probe: %s, model %s' % (
        engine_class.name, getattr(engine, 'model', '?')))
    report, reliable = novel.probe_engine(
        engine, conversion.get_novel_config(), details=True)
    print(report)
    sys.exit(0 if reliable else 1)
PY

if command -v calibre-debug > /dev/null; then
    calibre-debug -e "$WORK/run.py" "$WORK/calibre_plugins" "$ROOT" "$@"
elif [ -n "${CALIBRE_DEBUG:-}" ]; then
    "$CALIBRE_DEBUG" -e "$WORK/run.py" "$WORK/calibre_plugins" "$ROOT" "$@"
elif command -v flatpak > /dev/null \
        && flatpak info com.calibre_ebook.calibre > /dev/null 2>&1; then
    flatpak run --filesystem="$WORK" --filesystem="$ROOT" \
        --command=calibre-debug com.calibre_ebook.calibre \
        -e "$WORK/run.py" "$WORK/calibre_plugins" "$ROOT" "$@"
else
    fail "no calibre-debug found (PATH, \$CALIBRE_DEBUG or flatpak)"
fi

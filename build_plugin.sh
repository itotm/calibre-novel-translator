#!/usr/bin/env bash
# build_plugin.sh - Build the installable zip of the Novel Translator
# Calibre plugin.
#
# Releases are cut by hand: this script builds the archive, checks it
# and prints the line that installs it.
#
# Usage:
#   ./build_plugin.sh                 # ../novel-translator_vX.Y.Z.zip
#   ./build_plugin.sh some-name.zip   # write that file instead
#   ./build_plugin.sh --no-check      # skip the preflight checks
#   ./build_plugin.sh --help
#
# It always packages the directory it lives in, whatever the working
# directory is.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="$(basename "$0")"
cd "$SCRIPT_DIR"

fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

RUN_CHECKS=1
OUTPUT=''
for argument in "$@"; do
    case "$argument" in
        --no-check) RUN_CHECKS=0 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) fail "unknown option: $argument" ;;
        *) OUTPUT="$argument" ;;
    esac
done

command -v zip > /dev/null || fail "'zip' is required but not installed"
command -v python3 > /dev/null || fail "'python3' is required but not found"
[ -f __init__.py ] || fail "__init__.py not found in $SCRIPT_DIR"

# The plugin version is the single source of truth for the archive name.
VERSION="$(sed -n \
    's/^ *version = (\([0-9]*\), *\([0-9]*\), *\([0-9]*\)).*/\1.\2.\3/p' \
    __init__.py)"
[ -n "$VERSION" ] || fail "could not read the version from __init__.py"

if [ -z "$OUTPUT" ]; then
    OUTPUT="../novel-translator_v${VERSION}.zip"
fi
# Turn a relative output path into an absolute one.
if [[ "$OUTPUT" != /* ]]; then
    OUTPUT="$SCRIPT_DIR/$OUTPUT"
fi

# Everything that must not reach an installed plugin.
EXCLUDES=(
    -x '*.pyc'
    -x '*/__pycache__/*' -x './__pycache__/*'
    -x './.git/*' -x './.github/*'
    -x './tests/*' -x './page/*'
    -x "./$SCRIPT_NAME"
)

preflight() {
    echo "Checking..."

    local declarations
    declarations=$(find . -maxdepth 1 -name 'plugin-import-name-*.txt' | wc -l)
    [ "$declarations" -eq 1 ] || fail \
        "expected exactly one plugin-import-name-*.txt, found $declarations"

    local import_name
    import_name=$(find . -maxdepth 1 -name 'plugin-import-name-*.txt' \
        -printf '%f\n' | sed 's/^plugin-import-name-//; s/\.txt$//')
    echo "  import name : $import_name"

    # An absolute import that does not match the declared namespace would
    # reach for a different plugin at runtime, or for nothing at all.
    local stale
    stale=$(grep -rn 'calibre_plugins\.[A-Za-z0-9_]*' --include='*.py' . \
        | grep -v "calibre_plugins\.$import_name\b" || true)
    [ -z "$stale" ] || fail "imports disagree with the declared name:
$stale"

    python3 - <<'PY' || fail "some modules do not compile"
import pathlib
import sys
import warnings

broken = []
for path in sorted(pathlib.Path('.').rglob('*.py')):
    if '__pycache__' in path.parts:
        continue
    try:
        with warnings.catch_warnings():
            # Upstream sources raise a few SyntaxWarnings that are not
            # this script's business; only real errors matter here.
            warnings.simplefilter('ignore')
            compile(path.read_text(encoding='utf-8'), str(path), 'exec')
    except SyntaxError as error:
        broken.append('  %s:%s: %s' % (path, error.lineno, error.msg))
if broken:
    print('\n'.join(broken), file=sys.stderr)
    sys.exit(1)
PY
    echo "  modules     : compile"
}

verify_archive() {
    python3 - "$1" <<'PY' || fail "the archive is not installable"
import sys
import zipfile

names = zipfile.ZipFile(sys.argv[1]).namelist()
problems = []
declarations = [n for n in names if n.startswith('plugin-import-name-')]
if len(declarations) != 1:
    problems.append('plugin-import-name-*.txt entries: %s' % declarations)
if '__init__.py' not in names:
    problems.append('__init__.py is missing')
leaked = sorted({
    n.split('/')[0] for n in names
    if n.startswith(('tests/', 'page/', '.git/', '.github/'))})
if leaked:
    problems.append('excluded directories leaked in: %s' % leaked)
if problems:
    print('\n'.join('  ' + p for p in problems), file=sys.stderr)
    sys.exit(1)
print('  entries     : %d files' % len(names))
PY
}

echo "Building plugin zip..."
echo "  source      : $SCRIPT_DIR"
echo "  version     : v$VERSION"
echo "  output      : $OUTPUT"

if [ "$RUN_CHECKS" -eq 1 ]; then
    preflight
fi

rm -f "$OUTPUT"
zip -r "$OUTPUT" . "${EXCLUDES[@]}" > /dev/null

if [ "$RUN_CHECKS" -eq 1 ]; then
    verify_archive "$OUTPUT"
fi

echo "Done: $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
echo ""
echo "To install it in Calibre:"
echo "  calibre-customize -a \"$OUTPUT\""
echo "  or: Preferences → Plugins → Load plugin from file"

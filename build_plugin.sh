#!/usr/bin/env bash
# build_plugin.sh - Build the installable zip of the Ebook Translator
# (Novel) Calibre plugin.
#
# Usage:
#   ./build_plugin.sh              # produces ../ebook-translator-novel.zip
#   ./build_plugin.sh my-file.zip  # produces my-file.zip
#
# Run it from inside the plugin directory (the one holding __init__.py).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_OUTPUT="../ebook-translator-novel.zip"
OUTPUT="${1:-$DEFAULT_OUTPUT}"

# Turn a relative output path into an absolute one.
if [[ "$OUTPUT" != /* ]]; then
    OUTPUT="$SCRIPT_DIR/$OUTPUT"
fi

echo "Building plugin zip..."
echo "  Source : $SCRIPT_DIR"
echo "  Output : $OUTPUT"

# Drop any previous archive.
rm -f "$OUTPUT"

cd "$SCRIPT_DIR"

zip -r "$OUTPUT" . \
    -x "*.pyc" \
    -x "./__pycache__" \
    -x "./__pycache__/*" \
    -x "*/__pycache__" \
    -x "*/__pycache__/*" \
    -x "./.git" \
    -x "./.git/*" \
    -x "./.github" \
    -x "./.github/*" \
    -x "./tests" \
    -x "./tests/*" \
    -x "./page" \
    -x "./page/*" \
    -x "./build_plugin.sh" \
    > /dev/null

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo "Done: $OUTPUT ($SIZE)"
echo ""
echo "To install it in Calibre:"
echo "  Preferences → Plugins → Load plugin from file → $OUTPUT"
echo "  or: calibre-customize -a \"$OUTPUT\""

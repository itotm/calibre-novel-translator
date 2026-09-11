#!/usr/bin/env python3
"""Refresh the translation catalogs from the sources.

Collects every ``_('...')`` string in the plugin, rewrites
``message.pot``, carries the translations each ``*.po`` already has over
to the new template (dropping what no longer exists in the sources) and
compiles the ``*.mo`` files calibre loads. Pure Python: it needs neither
gettext nor a third-party library.

    python3 translations/update.py
"""
import ast
import os
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
PROJECT = 'Novel Translator'
BUGS = 'https://github.com/itotm/calibre-plugin-ebook-translator/issues'
SKIP_DIRS = {'vendor', 'tests', '__pycache__', '.git', 'translations'}


def extract(root):
    """Return {msgid: [locations]} for every _() call, in source order."""
    found = {}
    for path in sorted(root.rglob('*.py')):
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ('_', '_z') and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                where = '%s:%d' % (path.relative_to(root), node.lineno)
                found.setdefault(arg.value, []).append(where)
    return found


def po_escape(text):
    return (text.replace('\\', '\\\\').replace('"', '\\"')
            .replace('\t', '\\t').replace('\n', '\\n'))


def po_string(text):
    """A msgid/msgstr value the way gettext lays it out."""
    if '\n' not in text.rstrip('\n') and len(text) < 70:
        return '"%s"' % po_escape(text)
    lines = text.split('\n')
    parts = ['""']
    for i, line in enumerate(lines):
        if i < len(lines) - 1:
            line += '\n'
        if line:
            parts.append('"%s"' % po_escape(line))
    return '\n'.join(parts)


def parse_po(path):
    """Return (header, {msgid: msgstr}) of a .po file."""
    entries = {}
    header = ''
    msgid = msgstr = None
    current = None
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if line.startswith('msgid '):
            if msgid is not None:
                entries[msgid] = msgstr or ''
            msgid = ast.literal_eval(line[6:])
            msgstr = ''
            current = 'id'
        elif line.startswith('msgstr '):
            msgstr = ast.literal_eval(line[7:])
            current = 'str'
        elif line.startswith('"') and current:
            value = ast.literal_eval(line)
            if current == 'id':
                msgid += value
            else:
                msgstr += value
        elif not line:
            current = None
    if msgid is not None:
        entries[msgid] = msgstr or ''
    header = entries.pop('', '')
    return header, entries


def header_lines(language, revision):
    fields = [
        ('Project-Id-Version', PROJECT),
        ('Report-Msgid-Bugs-To', BUGS),
        ('POT-Creation-Date', datetime.now(timezone.utc)
         .strftime('%Y-%m-%d %H:%M+0000')),
        ('PO-Revision-Date', revision),
        ('Language', language),
        ('MIME-Version', '1.0'),
        ('Content-Type', 'text/plain; charset=UTF-8'),
        ('Content-Transfer-Encoding', '8bit'),
    ]
    return ''.join('%s: %s\n' % f for f in fields)


def field(header, name, default=''):
    for line in header.split('\n'):
        if line.startswith(name + ':'):
            return line.split(':', 1)[1].strip()
    return default


def write_catalog(path, msgids, translations, header, comment):
    out = [comment, 'msgid ""', 'msgstr %s' % po_string(header), '']
    for msgid, locations in msgids.items():
        out.append('#: ' + ' '.join(locations))
        out.append('msgid %s' % po_string(msgid))
        out.append('msgstr %s' % po_string(translations.get(msgid, '')))
        out.append('')
    path.write_text('\n'.join(out), encoding='utf-8')


def compile_mo(po_path, mo_path):
    header, entries = parse_po(po_path)
    messages = {'': header}
    messages.update({k: v for k, v in entries.items() if v})
    keys = sorted(messages)
    ids = strs = b''
    offsets = []
    for key in keys:
        msgid = key.encode('utf-8')
        msgstr = messages[key].encode('utf-8')
        offsets.append((len(ids), len(msgid), len(strs), len(msgstr)))
        ids += msgid + b'\0'
        strs += msgstr + b'\0'
    n = len(keys)
    keystart = 7 * 4 + 16 * n
    valuestart = keystart + len(ids)
    koffsets = []
    voffsets = []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    data = struct.pack('Iiiiiii', 0x950412de, 0, n, 7 * 4, 7 * 4 + n * 8,
                       0, 0)
    data += struct.pack('%di' % len(koffsets), *koffsets)
    data += struct.pack('%di' % len(voffsets), *voffsets)
    mo_path.write_bytes(data + ids + strs)


def main():
    msgids = extract(ROOT)
    template_comment = (
        '# Translation template for %s.\n'
        '# This file is distributed under the same license as the plugin.\n'
        '#' % PROJECT)
    write_catalog(HERE / 'message.pot', msgids, {},
                  header_lines('', 'YEAR-MO-DA HO:MI+ZONE'),
                  template_comment)
    for po in sorted(HERE.glob('*.po')):
        header, old = parse_po(po)
        language = po.stem  # the file name is the truth; pt.po said 'fr'
        kept = {k: v for k, v in old.items() if k in msgids and v}
        comment = (
            '# %s translation of %s.\n'
            '# This file is distributed under the same license as the '
            'plugin.\n#' % (language, PROJECT))
        write_catalog(po, msgids, kept,
                      header_lines(language, field(
                          header, 'PO-Revision-Date', 'YEAR-MO-DA HO:MI+ZONE')),
                      comment)
        compile_mo(po, po.with_suffix('.mo'))
        print('%-8s %4d of %d strings translated' % (
            language, len(kept), len(msgids)))
    print('template: %d strings' % len(msgids))


if __name__ == '__main__':
    sys.exit(main())

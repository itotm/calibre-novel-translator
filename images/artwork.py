"""Draw the plugin icon and the README logo: an open book whose right
page is being written in a second colour, on a deep blue tile."""
from PIL import Image, ImageDraw, ImageFont

BOLD = '/usr/share/fonts/google-noto/NotoSans-Bold.ttf'
REGULAR = '/usr/share/fonts/google-noto/NotoSans-Regular.ttf'
NAVY = (28, 46, 92)
NAVY_DARK = (18, 30, 64)
PAPER = (247, 243, 232)
INK = (60, 70, 96)
ACCENT = (226, 132, 58)


def rounded_tile(size, radius, color):
    scale = 4
    big = Image.new('RGBA', (size * scale, size * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1),
        radius=radius * scale, fill=color)
    return big.resize((size, size), Image.LANCZOS)


def book(size, cx, cy, width, height):
    """An open book drawn at 4x and downsampled, as an RGBA layer."""
    scale = 4
    layer = Image.new('RGBA', (size * scale, size * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    s = lambda v: int(v * scale)
    left = cx - width / 2
    right = cx + width / 2
    top = cy - height / 2
    bottom = cy + height / 2
    sag = height * 0.08
    spine = width * 0.02
    # Two pages, each a quadrilateral leaning towards the spine.
    lp = [(left, top + sag), (cx - spine, top), (cx - spine, bottom),
          (left, bottom - sag)]
    rp = [(cx + spine, top), (right, top + sag), (right, bottom - sag),
          (cx + spine, bottom)]
    d.polygon([(s(x), s(y)) for x, y in lp], fill=PAPER)
    d.polygon([(s(x), s(y)) for x, y in rp], fill=PAPER)
    # The spine.
    d.polygon([(s(cx - spine), s(top)), (s(cx + spine), s(top)),
               (s(cx + spine), s(bottom)), (s(cx - spine), s(bottom))],
              fill=NAVY_DARK)
    # Lines of text: the source on the left, the translation on the
    # right, in a second colour and not yet finished.
    margin = width * 0.08
    line_h = height * 0.055
    gap = height * 0.11
    y = top + height * 0.2
    lengths_left = (0.85, 0.7, 0.9, 0.6, 0.8, 0.5)
    lengths_right = (0.8, 0.65, 0.9, 0.55, 0.3, 0.0)
    for i, (ll, lr) in enumerate(zip(lengths_left, lengths_right)):
        yy = y + i * gap
        page_w = width / 2 - spine - 2 * margin
        d.rounded_rectangle(
            (s(left + margin), s(yy), s(left + margin + page_w * ll),
             s(yy + line_h)), radius=s(line_h / 2), fill=INK)
        if lr:
            d.rounded_rectangle(
                (s(cx + spine + margin), s(yy),
                 s(cx + spine + margin + page_w * lr), s(yy + line_h)),
                radius=s(line_h / 2), fill=ACCENT)
    return layer.resize((size, size), Image.LANCZOS)


def icon(path, size=1000):
    tile = rounded_tile(size, int(size * 0.22), NAVY)
    layer = book(size, size / 2, size * 0.52, size * 0.72, size * 0.5)
    tile.alpha_composite(layer)
    tile.save(path)


def logo(path, width=1200, height=460):
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    tile_size = 300
    tile = rounded_tile(tile_size, int(tile_size * 0.22), NAVY)
    layer = book(tile_size, tile_size / 2, tile_size * 0.52,
                 tile_size * 0.72, tile_size * 0.5)
    tile.alpha_composite(layer)
    img.alpha_composite(tile, (70, (height - tile_size) // 2))
    d = ImageDraw.Draw(img)
    title = ImageFont.truetype(BOLD, 84)
    sub = ImageFont.truetype(REGULAR, 40)
    x = 70 + tile_size + 60
    d.text((x, 140), 'Novel Translator', font=title, fill=NAVY)
    d.text((x + 5, 258), 'A calibre plugin', font=sub, fill=INK)
    img.save(path)


if __name__ == '__main__':
    icon('images/icon.png')
    logo('images/logo.png')

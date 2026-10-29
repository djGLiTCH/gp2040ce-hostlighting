# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: make_icons.py
# Folder: scripts
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Draw the hlp app's icon, a leverless controller's buttons, into packaging/icons.

It writes hlp.ico, hlp.icns, hlp.png and hlp.svg. The large sizes show the whole layout, 48 and 32 pixels the eight
action buttons, 24 and 16 pixels four buttons. Each size is drawn at four times its size and scaled down. Needs
Pillow. Run: python scripts/make_icons.py
"""
import pathlib

from PIL import Image, ImageDraw

ICONS = pathlib.Path(__file__).parent.parent / 'packaging' / 'icons'
BODY = '#1b1b1f'
WHITE = '#F1EFE8'

# On a 64-unit grid: (x, y, radius, colour) per button
FULL = [(9, 27, 3.6, WHITE), (16.5, 24, 3.6, WHITE), (24, 25.5, 3.6, WHITE), (27, 43, 4.4, WHITE),
        (34, 24, 3.4, '#E24B4A'), (41.5, 21.5, 3.4, '#EF9F27'), (49, 21.5, 3.4, '#97C459'), (56, 23.5, 3.4, '#5DCAA5'),
        (35, 32.5, 3.4, '#378ADD'), (42.5, 30, 3.4, '#7F77DD'), (50, 30, 3.4, '#D4537E'), (57, 32, 3.4, '#F09595')]
EIGHT = [(12, 27, 6, '#E24B4A'), (25, 23, 6, '#EF9F27'), (39, 23, 6, '#97C459'), (52, 26, 6, '#5DCAA5'),
         (13, 41, 6, '#378ADD'), (26, 37, 6, '#7F77DD'), (40, 37, 6, '#D4537E'), (53, 40, 6, WHITE)]
FOUR = [(21, 23, 10, '#E24B4A'), (43, 23, 10, '#97C459'), (21, 43, 10, '#378ADD'), (43, 43, 10, '#EF9F27')]


def buttons(size: int) -> list[tuple[float, float, float, str]]:
    """Choose the buttons drawn at a size."""
    return FULL if size >= 64 else EIGHT if size >= 32 else FOUR


def draw(size: int) -> Image.Image:
    """Draw the icon at one size."""
    scale = size * 4 / 64
    image = Image.new('RGBA', (size * 4, size * 4), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    canvas.rounded_rectangle((2 * scale, 2 * scale, 62 * scale, 62 * scale), radius=14 * scale, fill=BODY)
    for x, y, radius, colour in buttons(size):
        canvas.ellipse(((x - radius) * scale, (y - radius) * scale, (x + radius) * scale, (y + radius) * scale),
                       fill=colour)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def svg() -> str:
    """Write the large icon as SVG, for the README."""
    circles = ''.join(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{colour}"/>' for x, y, radius, colour in FULL)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<rect x="2" y="2" width="60" height="60" rx="14" fill="{BODY}"/>{circles}</svg>\n')


def main() -> None:
    """Write the icon files into packaging/icons."""
    images = {size: draw(size) for size in (16, 24, 32, 48, 64, 128, 256, 512, 1024)}
    ico = [16, 24, 32, 48, 64, 128, 256]
    images[256].save(ICONS / 'hlp.ico', sizes=[(size, size) for size in ico],
                     append_images=[images[size] for size in ico if size != 256])
    icns = [16, 32, 64, 128, 256, 512, 1024]
    images[1024].save(ICONS / 'hlp.icns', append_images=[images[size] for size in icns if size != 1024])
    images[256].save(ICONS / 'hlp.png')
    (ICONS / 'hlp.svg').write_text(svg(), encoding='utf-8')


if __name__ == '__main__':
    main()

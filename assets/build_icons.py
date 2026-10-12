"""Regenerate all icon assets from the SVG sources.

Detailed artwork (assets/logo.svg) is used for 48px and above; a simplified
bold-tower variant is used for 16-32px where fine detail would blur. Tray
state icons reuse the simplified tower on a status-colored badge.

Outputs: assets/logo.ico, assets/tray-{online,offline,busy}.ico
Requires: Edge/Chrome headless for SVG rasterization, Pillow for resizing.
"""
import re
import struct
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'

BADGE = '#315e48'
STATES = {'online': '#2e9e5b', 'offline': '#9aa59b', 'busy': '#d98e32'}

# Small-size variant: bold tower silhouette only. Waves, mountains, clock
# hands and signal arcs are dropped below 48px; the clock becomes a dot.
SMALL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="{size}" height="{size}">
  <circle cx="8" cy="8" r="7.6" fill="{color}"/>
  <rect x="7.45" y="1.7" width="1.1" height="1.5" rx="0.3" fill="#f5f5ef"/>
  <polygon points="6.1,5.6 9.9,5.6 8,3.1" fill="#f5f5ef"/>
  <polygon points="6.35,5.6 9.65,5.6 10.15,12.7 5.85,12.7" fill="#f5f5ef"/>
  <circle cx="8" cy="8.1" r="1.15" fill="{color}"/>
  <rect x="7.35" y="11.1" width="1.3" height="1.6" rx="0.65" fill="{color}"/>
</svg>
"""


def render(svg_text, size, out_path):
    """Rasterize via headless Edge at 256px, then Lanczos-downscale."""
    big = ROOT / '_icon_tmp.svg'
    big.write_text(svg_text, encoding='utf-8')
    subprocess.run([EDGE, '--headless', '--disable-gpu', f'--screenshot={out_path}',
                    '--window-size=256,256', '--default-background-color=00000000',
                    big.as_uri()], check=True, capture_output=True)
    img = Image.open(out_path).convert('RGBA')
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    return img


def write_ico(path, frames):
    """Pack (size, RGBA image) frames as PNG-compressed ICO entries."""
    payloads = []
    for size, img in sorted(frames.items()):
        png = ROOT / '_icon_tmp.png'
        img.save(png, format='PNG')
        payloads.append((size, png.read_bytes()))
    header = struct.pack('<HHH', 0, 1, len(payloads))
    offset = 6 + 16 * len(payloads)
    entries, blobs = [], []
    for size, blob in payloads:
        dim = size if size < 256 else 0
        entries.append(struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    Path(path).write_bytes(header + b''.join(entries) + b''.join(blobs))


def main():
    detailed_svg = (ROOT / 'logo-256.svg').read_text(encoding='utf-8')
    tmp_png = ROOT / '_render.png'

    def detailed(size):
        render(detailed_svg, size, tmp_png)
        return Image.open(tmp_png).convert('RGBA').resize((size, size), Image.LANCZOS)

    def small(size, color):
        svg = SMALL_SVG.format(size=256, color=color)
        render(svg, size, tmp_png)
        return Image.open(tmp_png).convert('RGBA').resize((size, size), Image.LANCZOS)

    # App icon: simplified artwork below 48px, detailed at 48px and above.
    frames = {**{s: small(s, BADGE) for s in (16, 24, 32, 40)},
              **{s: detailed(s) for s in (48, 64, 128, 256)}}
    write_ico(ROOT / 'logo.ico', frames)
    print('logo.ico:', sorted(frames))

    # Tray icons: simplified tower on a status-colored badge, small sizes only.
    for state, color in STATES.items():
        frames = {s: small(s, color) for s in (16, 20, 24, 32, 40, 48)}
        write_ico(ROOT / f'tray-{state}.ico', frames)
        print(f'tray-{state}.ico:', sorted(frames))

    # Verification previews at exact pixel size.
    small(16, STATES['online']).save(ROOT / 'preview-tray-online-16.png')
    small(32, STATES['busy']).save(ROOT / 'preview-tray-busy-32.png')
    small(16, BADGE).save(ROOT / 'preview-app-16.png')
    for junk in ('_icon_tmp.svg', '_icon_tmp.png', '_render.png'):
        (ROOT / junk).unlink(missing_ok=True)


if __name__ == '__main__':
    sys.exit(main())

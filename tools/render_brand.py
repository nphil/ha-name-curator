"""Deterministic Pillow renderer for the Name Curator brand kit.

Writes the eight images Home Assistant serves straight from
``custom_components/name_curator/brand/`` (no brands-repo submission needed):

    icon.png        256x256      dark_icon.png        256x256
    icon@2x.png     512x512      dark_icon@2x.png     512x512
    logo.png        864x256      dark_logo.png        864x256
    logo@2x.png     1728x512     dark_logo@2x.png     1728x512

The mark: a rounded name tag with a string hole and a text cursor (I-beam)
cut out of it: a label being tidied. Flat colour only, three shapes, no
gradients. ``../icon.svg`` is the same mark as hand-authored vector art; this
script does not read it, so keep the numbers below in sync with it.

Light variant ("icon", "logo"): teal plate, white tag.
Dark variant ("dark_icon", "dark_logo"): pale plate, teal tag, so it keeps its
contrast on the dark Home Assistant header.

Pillow only, 4x supersampling, no randomness: re-running produces
byte-identical PNGs.

Usage: python3 tools/render_brand.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "custom_components" / "name_curator" / "brand"

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Liberation Sans Bold, then DejaVu Bold, then Pillow's default font."""
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


# ---------------------------------------------------------------------------
# Geometry, in a 256x256 reference space (identical numbers to ../icon.svg).
# Capsules are ((x0, y0), (x1, y1), radius): a round-capped stroke.
# ---------------------------------------------------------------------------

PLATE_RX = 56.0

# The tag: this polygon grown by TAG_R with round joins (a stroked polygon in SVG).
TAG_POINTS = ((48.0, 92.0), (148.0, 92.0), (200.0, 128.0), (148.0, 164.0), (48.0, 164.0))
TAG_R = 12.0
HOLE = (168.0, 128.0, 9.0)  # cx, cy, r
CURSOR = (  # text cursor: stem plus two serifs
    ((92.0, 106.0), (92.0, 150.0), 6.0),
    ((76.0, 106.0), (108.0, 106.0), 6.0),
    ((76.0, 150.0), (108.0, 150.0), 6.0),
)

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

LIGHT_VARIANT = {
    "plate": (0x0E, 0x93, 0x84),
    "mark": (0xFF, 0xFF, 0xFF),
    "cut": (0x0E, 0x93, 0x84),  # hole and cursor show the plate through the tag
}
DARK_VARIANT = {
    "plate": (0xE6, 0xF6, 0xF3),
    "mark": (0x0B, 0x7A, 0x6E),
    "cut": (0xE6, 0xF6, 0xF3),
}

WORDMARK_ON_LIGHT_BG = (0x0A, 0x3B, 0x36)  # logo.png
SUBMARK_ON_LIGHT_BG = (0x0E, 0x93, 0x84)
WORDMARK_ON_DARK_BG = (0xEC, 0xFA, 0xF7)  # dark_logo.png
SUBMARK_ON_DARK_BG = (0x5F, 0xD6, 0xC6)

SS = 4  # supersampling factor


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _capsule(draw, capsule, k, fill):
    (x0, y0), (x1, y1), r = capsule
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    nx, ny = -dy / length * r, dx / length * r
    draw.polygon(
        [((x0 + nx) * k, (y0 + ny) * k), ((x1 + nx) * k, (y1 + ny) * k),
         ((x1 - nx) * k, (y1 - ny) * k), ((x0 - nx) * k, (y0 - ny) * k)],
        fill=fill,
    )
    for cx, cy in ((x0, y0), (x1, y1)):
        draw.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)


def _rounded_polygon(draw, points, r, k, fill):
    """Polygon grown by r with round corners: fill + a round-capped edge each."""
    draw.polygon([(x * k, y * k) for x, y in points], fill=fill)
    for i, start in enumerate(points):
        _capsule(draw, (start, points[(i + 1) % len(points)], r), k, fill)


def render_mark(size, palette):
    """(size, size) RGBA icon: flat rounded plate with the tag-and-cursor mark."""
    big = size * SS
    k = big / 256.0
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=PLATE_RX * k, fill=palette["plate"] + (255,))

    _rounded_polygon(d, TAG_POINTS, TAG_R, k, palette["mark"] + (255,))
    cx, cy, r = HOLE
    cut = palette["cut"] + (255,)
    d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=cut)
    for capsule in CURSOR:
        _capsule(d, capsule, k, cut)

    return canvas.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Wordmark lockup
# ---------------------------------------------------------------------------

LOGO_ASPECT = 864 / 256  # width / height (about 3.4x)
LOGO_MARK_FRAC = 0.86  # mark size as a fraction of canvas height
LOGO_LEFT_FRAC = 0.05
LOGO_GAP_FRAC = 0.11
LOGO_NAME_FRAC = 0.34  # "Name" font size / canvas height
LOGO_SUB_FRAC = 0.34  # "Curator" font size / canvas height
LOGO_TRACK_FRAC = 0.004
LOGO_LINE_GAP_FRAC = 0.09


def _tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(draw, x, y, text, font, fill, tracking):
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def render_logo(height, palette, name_rgb, sub_rgb):
    """RGBA lockup: mark on the left, "Name" over "Curator" on the right,
    transparent background, exactly `height` tall."""
    width = round(height * LOGO_ASPECT)
    mark_size = round(height * LOGO_MARK_FRAC)
    mark = render_mark(mark_size, palette)

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    left = round(height * LOGO_LEFT_FRAC)
    canvas.alpha_composite(mark, (left, (height - mark_size) // 2))

    name_font = load_font(round(height * LOGO_NAME_FRAC))
    sub_font = load_font(round(height * LOGO_SUB_FRAC))
    tracking = height * LOGO_TRACK_FRAC
    draw = ImageDraw.Draw(canvas)

    # Vertically centre the two-line block on ink bounds, not font ascent.
    n_l, n_t, n_r, n_b = draw.textbbox((0, 0), "Name", font=name_font)
    s_l, s_t, s_r, s_b = draw.textbbox((0, 0), "Curator", font=sub_font)
    line_gap = height * LOGO_LINE_GAP_FRAC
    block_h = (n_b - n_t) + line_gap + (s_b - s_t)
    top = (height - block_h) / 2
    text_x = left + mark_size + height * LOGO_GAP_FRAC

    name_w = _tracked_width(draw, "Name", name_font, tracking)
    _draw_tracked(draw, text_x - n_l, top - n_t, "Name", name_font, name_rgb + (255,), tracking)
    sub_y = top + (n_b - n_t) + line_gap
    _draw_tracked(draw, text_x - s_l, sub_y - s_t, "Curator", sub_font, sub_rgb + (255,), tracking)

    sub_w = _tracked_width(draw, "Curator", sub_font, tracking)
    right_edge = text_x + max(name_w, sub_w)
    assert right_edge <= width - height * 0.03, f"wordmark clipped at height {height}: {right_edge} > {width}"
    return canvas


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _save_pair(master, stem):
    """<stem>@2x.png from the 512-based master, <stem>.png as an exact half."""
    master.save(BRAND_DIR / f"{stem}@2x.png")
    half = (master.width // 2, master.height // 2)
    master.resize(half, Image.LANCZOS).save(BRAND_DIR / f"{stem}.png")


def main():
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    _save_pair(render_mark(512, LIGHT_VARIANT), "icon")
    _save_pair(render_mark(512, DARK_VARIANT), "dark_icon")
    _save_pair(render_logo(512, LIGHT_VARIANT, WORDMARK_ON_LIGHT_BG, SUBMARK_ON_LIGHT_BG), "logo")
    _save_pair(render_logo(512, DARK_VARIANT, WORDMARK_ON_DARK_BG, SUBMARK_ON_DARK_BG), "dark_logo")


if __name__ == "__main__":
    main()

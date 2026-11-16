"""Card and table artwork rendered with Pillow.

Card faces come from the MIT-licensed ``playing-cards-assets`` deck (see
``assets/cards/LICENSE``). Every image is rendered to PNG bytes ready to be
attached to a Discord message.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import CARDS_DIR

# Palette
FELT_TOP = (13, 47, 38)
FELT_BOTTOM = (5, 24, 19)
GOLD = (232, 188, 92)
GOLD_SOFT = (196, 158, 82)
CREAM = (243, 238, 226)
MUTED = (157, 177, 166)
PANEL_EDGE = (31, 78, 62)
RED = (214, 88, 78)

RANK_NAMES = {
    "A": "ace",
    "K": "king",
    "Q": "queen",
    "J": "jack",
    "T": "10",
    "9": "9",
    "8": "8",
    "7": "7",
    "6": "6",
    "5": "5",
    "4": "4",
    "3": "3",
    "2": "2",
}
SUIT_NAMES = {"s": "spades", "h": "hearts", "d": "diamonds", "c": "clubs"}

_FONT_CANDIDATES_BOLD = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
_FONT_CANDIDATES_REGULAR = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


@dataclass
class PlayerVisual:
    name: str
    chips: int
    bet: int = 0
    status: str = "active"  # active | folded | out | all-in
    is_turn: bool = False
    is_button: bool = False
    hole: list[str] | None = None  # None = hidden, [] = shown as none


@dataclass
class TableVisual:
    hand_number: int
    street: str
    pot: int
    board: list[str]
    players: list[PlayerVisual]
    blinds: tuple[int, int]
    buy_in: int


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit(draw, text: str, font, max_width: int) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    while text and _text_width(draw, text + ellipsis, font) > max_width:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


def _card_base(art: Image.Image, width: int, height: int) -> Image.Image:
    """Place transparent card art on an opaque white face with round corners."""
    base = Image.new("RGBA", (width, height), (250, 249, 245, 255))
    base.alpha_composite(art)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=max(3, width // 12),
        fill=255,
    )
    base.putalpha(mask)
    return base


@lru_cache(maxsize=128)
def _card_image(code: str, width: int, height: int) -> Image.Image:
    rank = RANK_NAMES.get(code[0], code[0])
    suit = SUIT_NAMES.get(code[-1])
    if suit is None:
        raise ValueError(f"Unknown card: {code!r}")
    path = Path(CARDS_DIR) / f"{rank}_of_{suit}.png"
    art = Image.open(path).convert("RGBA")
    return _card_base(art.resize((width, height), Image.LANCZOS), width, height)


@lru_cache(maxsize=8)
def _card_back(width: int, height: int) -> Image.Image:
    art = Image.open(Path(CARDS_DIR) / "back.png").convert("RGBA")
    return _card_base(art.resize((width, height), Image.LANCZOS), width, height)


def _paste_card(
    canvas: Image.Image,
    code: str,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    shadow: bool = True,
) -> None:
    if shadow:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle(
            (x + 3, y + 5, x + width + 3, y + height + 5),
            radius=max(4, width // 12),
            fill=(0, 0, 0, 90),
        )
        layer = layer.filter(ImageFilter.GaussianBlur(4))
        canvas.alpha_composite(layer)
    canvas.alpha_composite(_card_image(code, width, height), (x, y))


def _paste_back(
    canvas: Image.Image, x: int, y: int, width: int, height: int, *, shadow: bool = True
) -> None:
    if shadow:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle(
            (x + 2, y + 4, x + width + 2, y + height + 4),
            radius=max(3, width // 12),
            fill=(0, 0, 0, 80),
        )
        canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(3)))
    canvas.alpha_composite(_card_back(width, height), (x, y))


def _background(width: int, height: int) -> Image.Image:
    image = Image.new("RGBA", (width, height), FELT_BOTTOM)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(
            int(FELT_TOP[i] + (FELT_BOTTOM[i] - FELT_TOP[i]) * t) for i in range(3)
        )
        draw.line((0, y, width, y), fill=color + (255,))

    # Soft felt rings, drawn on their own layer so the alpha really blends.
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ring = ImageDraw.Draw(layer)
    ring.ellipse(
        (60, 20, width - 60, height - 6),
        outline=(255, 255, 255, 16),
        width=2,
    )
    ring.ellipse(
        (95, 55, width - 95, height - 42),
        outline=(255, 255, 255, 10),
        width=1,
    )
    image.alpha_composite(layer)

    # Vignette-like corners.
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow = ImageDraw.Draw(layer)
    glow.rectangle((0, 0, width, height), outline=(0, 0, 0, 150), width=24)
    layer = layer.filter(ImageFilter.GaussianBlur(22))
    image.alpha_composite(layer)
    return image


def _draw_chip(draw: ImageDraw.ImageDraw, x: int, y: int, radius: int, color) -> None:
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        outline=(255, 255, 255, 70),
        width=max(1, radius // 4),
    )
    draw.ellipse(
        (x - radius // 2, y - radius // 2, x + radius // 2, y + radius // 2),
        outline=(0, 0, 0, 45),
        width=1,
    )


def render_table(view: TableVisual) -> bytes:
    width, height = 1000, 620
    image = _background(width, height)
    draw = ImageDraw.Draw(image)

    # Header ----------------------------------------------------------------
    draw.text((38, 28), "♠", font=_font(30, True), fill=GOLD)
    draw.text((72, 33), "TEXAS HOLD'EM", font=_font(23, True), fill=CREAM)
    draw.text(
        (74, 58),
        f"TABLE · {view.buy_in:,} BUY-IN · BLINDS {view.blinds[0]:,}/{view.blinds[1]:,}",
        font=_font(12, True),
        fill=GOLD_SOFT,
    )
    right = f"HAND #{view.hand_number}"
    right_w = _text_width(draw, right, _font(18, True))
    draw.text((width - 40 - right_w, 34), right, font=_font(18, True), fill=CREAM)
    street = view.street.upper()
    street_w = _text_width(draw, street, _font(13, True))
    draw.text((width - 40 - street_w, 60), street, font=_font(13, True), fill=GOLD_SOFT)
    draw.line((38, 84, width - 38, 84), fill=(46, 84, 69), width=1)

    # Pot -------------------------------------------------------------------
    pot_pill_w, pot_pill_h = 250, 56
    px = (width - pot_pill_w) // 2
    draw.rounded_rectangle(
        (px, 108, px + pot_pill_w, 108 + pot_pill_h),
        radius=28,
        fill=(5, 19, 15),
        outline=(112, 91, 47),
        width=1,
    )
    _draw_chip(draw, px + 30, 136, 13, (191, 70, 64))
    draw.text((px + 52, 120), "POT", font=_font(12, True), fill=MUTED)
    draw.text(
        (px + 52, 134),
        f"{view.pot:,}",
        font=_font(24, True),
        fill=GOLD,
    )

    # Board -----------------------------------------------------------------
    board_w = 86
    board_h = 125
    gap = 10
    total = 5 * board_w + 4 * gap
    start_x = (width - total) // 2
    board_y = 202
    for i in range(5):
        x = start_x + i * (board_w + gap)
        if i < len(view.board):
            _paste_card(image, view.board[i], x, board_y, board_w, board_h)
        else:
            draw.rounded_rectangle(
                (x, board_y, x + board_w, board_y + board_h),
                radius=7,
                fill=(11, 38, 30),
                outline=(48, 89, 72),
                width=1,
            )

    # Players ---------------------------------------------------------------
    players = view.players
    half = (len(players) + 1) // 2
    columns = (players[:half], players[half:])
    panel_w, panel_h, panel_gap = 230, 84, 9
    for column_index, column in enumerate(columns):
        if not column:
            continue
        block_h = len(column) * panel_h + (len(column) - 1) * panel_gap
        y = 330 - block_h // 2
        x = 22 if column_index == 0 else width - 22 - panel_w
        for player in column:
            _draw_player_panel(image, draw, player, x, y, panel_w, panel_h)
            y += panel_h + panel_gap

    # Footer ----------------------------------------------------------------
    footer = "Use /cards to see your hole cards privately"
    footer_w = _text_width(draw, footer, _font(13, False))
    draw.text(
        ((width - footer_w) // 2, height - 38),
        footer,
        font=_font(13, False),
        fill=(150, 170, 160),
    )

    out = BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _draw_player_panel(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    player: PlayerVisual,
    x: int,
    y: int,
    width: int,
    height: int,
) -> None:
    folded = player.status == "folded"
    removed = player.status == "removed"
    busted = player.status == "busted"
    out = removed or busted
    all_in = player.status == "all-in"
    active = player.is_turn and not folded and not out

    fill = (6, 20, 16) if not out else (5, 15, 12)
    border = PANEL_EDGE
    border_width = 1
    if active:
        border = GOLD
        border_width = 3
    elif all_in:
        border = (150, 104, 190)

    if active:
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.rounded_rectangle(
            (x - 3, y - 3, x + width + 3, y + height + 3),
            radius=16,
            outline=(GOLD[0], GOLD[1], GOLD[2], 110),
            width=6,
        )
        image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(7)))

    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=14,
        fill=fill,
        outline=border,
        width=border_width,
    )
    if active:
        draw.rounded_rectangle(
            (x, y + 18, x + 3, y + height - 18),
            radius=2,
            fill=GOLD,
        )

    name_color = MUTED if (folded or out) else CREAM
    name_font = _font(18, True)
    name = _fit(draw, player.name, name_font, width - 116)
    draw.text((x + 14, y + 11), name, font=name_font, fill=name_color)

    chips_color = MUTED if (folded or out) else GOLD
    _draw_chip(draw, x + 20, y + 46, 6, chips_color)
    draw.text(
        (x + 32, y + 36),
        f"{player.chips:,}",
        font=_font(16, True),
        fill=chips_color,
    )

    if player.bet and not out:
        draw.text(
            (x + 14, y + 61),
            f"BET {player.bet:,}",
            font=_font(12, True),
            fill=(220, 174, 100) if not folded else (120, 120, 112),
        )

    if folded:
        tag_w = _text_width(draw, "FOLDED", _font(11, True))
        draw.text(
            (x + width - 16 - tag_w, y + 62),
            "FOLDED",
            font=_font(11, True),
            fill=(137, 148, 141),
        )
    elif removed or busted:
        tag = "REMOVED" if removed else "BUSTED"
        tag_w = _text_width(draw, tag, _font(11, True))
        draw.text(
            (x + width - 16 - tag_w, y + 62),
            tag,
            font=_font(11, True),
            fill=RED,
        )
    elif all_in:
        tag_w = _text_width(draw, "ALL-IN", _font(11, True))
        draw.text(
            (x + width - 16 - tag_w, y + 62),
            "ALL-IN",
            font=_font(11, True),
            fill=(190, 148, 222),
        )

    # Hole cards: real cards at showdown, backs while hidden, nothing folded.
    if player.hole is None and not folded and not out:
        _paste_back(image, x + width - 92, y + 12, 38, 55, shadow=False)
        _paste_back(image, x + width - 50, y + 12, 38, 55, shadow=False)
    elif player.hole:
        for i, card in enumerate(player.hole[:2]):
            _paste_card(
                image,
                card,
                x + width - 92 + i * 42,
                y + 12,
                38,
                55,
                shadow=False,
            )

    if player.is_button:
        _draw_chip(draw, x + width - 18, y + height - 16, 11, (243, 243, 238))
        draw.text(
            (x + width - 23, y + height - 26),
            "D",
            font=_font(12, True),
            fill=(40, 34, 22),
        )


def render_hole_cards(
    cards: list[str],
    *,
    title: str = "YOUR HAND",
    subtitle: str | None = None,
    note: str | None = None,
) -> bytes:
    width, height = 640, 380
    image = _background(width, height)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (18, 18, width - 18, height - 18),
        radius=22,
        outline=(42, 79, 65),
        width=1,
    )

    title_w = _text_width(draw, title, _font(14, True))
    draw.text(
        ((width - title_w) // 2, 38),
        title,
        font=_font(14, True),
        fill=GOLD_SOFT,
    )
    if subtitle:
        subtitle_w = _text_width(draw, subtitle, _font(28, True))
        draw.text(
            ((width - subtitle_w) // 2, 60),
            subtitle,
            font=_font(28, True),
            fill=CREAM,
        )

    card_w, card_h, gap = 136, 198, 20
    total = len(cards) * card_w + max(0, len(cards) - 1) * gap
    x = (width - total) // 2
    y = 112
    for card in cards:
        _paste_card(image, card, x, y, card_w, card_h)
        x += card_w + gap

    if note:
        note_font = _font(14, False)
        note = _fit(draw, note, note_font, width - 80)
        note_w = _text_width(draw, note, note_font)
        draw.text(
            ((width - note_w) // 2, height - 48),
            note,
            font=note_font,
            fill=MUTED,
        )

    out = BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


@dataclass(frozen=True)
class RankingRow:
    name: str
    description: str
    cards: tuple[str, ...]


HELP_RANKINGS: tuple[RankingRow, ...] = (
    RankingRow("Royal flush", "A-K-Q-J-10, all one suit", ("As", "Ks", "Qs", "Js", "Ts")),
    RankingRow("Straight flush", "Five in a row, same suit", ("9h", "8h", "7h", "6h", "5h")),
    RankingRow("Four of a kind", "All four cards of one rank", ("Ac", "Ad", "Ah", "As", "Kd")),
    RankingRow("Full house", "Three of a kind plus a pair", ("Kc", "Kd", "Kh", "9s", "9d")),
    RankingRow("Flush", "Five cards of one suit", ("Ah", "Jh", "8h", "5h", "2h")),
    RankingRow("Straight", "Five cards in sequence", ("9c", "8d", "7h", "6s", "5c")),
    RankingRow("Three of a kind", "Three cards of one rank", ("Qc", "Qd", "Qh", "7s", "2d")),
    RankingRow("Two pair", "Two different pairs", ("Jc", "Jd", "8s", "8h", "3c")),
    RankingRow("One pair", "Two cards of one rank", ("Tc", "Td", "As", "7h", "4c")),
    RankingRow("High card", "Nothing else; highest card", ("As", "Jd", "8c", "5h", "2s")),
)


@lru_cache(maxsize=2)
def render_help_chart(scale: int = 1) -> bytes:
    """Render the hand-ranking chart. ``scale`` enlarges it for game starts."""
    width, height = 1000 * scale, 1270 * scale
    image = _background(width, height)
    draw = ImageDraw.Draw(image)

    draw.text(
        (54 * scale, 44 * scale), "♠", font=_font(34 * scale, True), fill=GOLD
    )
    draw.text(
        (94 * scale, 49 * scale),
        "POKER HAND RANKINGS",
        font=_font(30 * scale, True),
        fill=CREAM,
    )
    draw.text(
        (96 * scale, 87 * scale),
        "Texas Hold'em · your best five cards from two hole cards and five community cards",
        font=_font(14 * scale, False),
        fill=MUTED,
    )
    draw.line(
        (54 * scale, 120 * scale, width - 54 * scale, 120 * scale),
        fill=(45, 82, 68),
        width=max(1, scale),
    )

    row_height = 104 * scale
    top = 142 * scale
    card_w, card_h, card_gap = 54 * scale, 79 * scale, 6 * scale
    for index, row in enumerate(HELP_RANKINGS):
        y = top + index * row_height
        if index % 2 == 0:
            draw.rounded_rectangle(
                (
                    42 * scale,
                    y - 6 * scale,
                    width - 42 * scale,
                    y + row_height - 14 * scale,
                ),
                radius=12 * scale,
                fill=(17, 43, 35),
            )

        rank_text = str(index + 1)
        center_x = 78 * scale
        center_y = y + 32 * scale
        if index == 0:
            draw.ellipse(
                (
                    center_x - 20 * scale,
                    center_y - 20 * scale,
                    center_x + 20 * scale,
                    center_y + 20 * scale,
                ),
                fill=(GOLD[0], GOLD[1], GOLD[2], 235),
            )
            draw.text(
                (center_x - 7 * scale, center_y - 13 * scale),
                rank_text,
                font=_font(19 * scale, True),
                fill=(24, 24, 18),
            )
        else:
            draw.ellipse(
                (
                    center_x - 19 * scale,
                    center_y - 19 * scale,
                    center_x + 19 * scale,
                    center_y + 19 * scale,
                ),
                outline=(74, 112, 96),
                width=max(1, scale),
            )
            draw.text(
                (center_x - 7 * scale, center_y - 13 * scale),
                rank_text,
                font=_font(19 * scale, True),
                fill=MUTED,
            )

        draw.text(
            (120 * scale, y + 14 * scale),
            row.name,
            font=_font(22 * scale, True),
            fill=CREAM,
        )
        draw.text(
            (120 * scale, y + 44 * scale),
            row.description,
            font=_font(13 * scale, False),
            fill=MUTED,
        )

        x = width - 54 * scale - (
            len(row.cards) * card_w + (len(row.cards) - 1) * card_gap
        )
        for card in row.cards:
            _paste_card(
                image,
                card,
                x,
                y + 2 * scale,
                card_w,
                card_h,
                shadow=False,
            )
            x += card_w + card_gap

        draw.line(
            (
                54 * scale,
                y + row_height - 12 * scale,
                width - 54 * scale,
                y + row_height - 12 * scale,
            ),
            fill=(32, 62, 51),
            width=max(1, scale),
        )

    footer = "Community cards are shared by everyone. Betting rounds: pre-flop, flop, turn, river."
    footer_font = _font(13 * scale, False)
    footer_w = _text_width(draw, footer, footer_font)
    draw.text(
        ((width - footer_w) // 2, height - 48 * scale),
        footer,
        font=footer_font,
        fill=(150, 170, 160),
    )

    out = BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()

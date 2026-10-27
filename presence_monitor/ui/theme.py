"""Design tokens and the application style sheet (Windows 11 inspired).

Controls Windows draws well (combo boxes, check boxes, text fields, sliders,
scroll bars, menus) keep Qt's native ``windows11`` style. The style sheet only
dresses this app's own surfaces: the navigation pane, cards, typography,
status pills, banners and accent buttons. Colours follow the Windows accent
colour and the light/dark setting.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

TEXT_FONT = "Segoe UI Variable Text"
TEXT_STRONG = "Segoe UI Variable Text Semibold"
DISPLAY_FONT = "Segoe UI Variable Display Semibold"
ICON_FONT = "Segoe Fluent Icons"
FALLBACK_TEXT = "Segoe UI"
FALLBACK_ICON = "Segoe MDL2 Assets"


@dataclass(frozen=True)
class Tokens:
    dark: bool
    window: str
    card: str
    card_hover: str
    card_border: str
    divider: str
    text: str
    text2: str
    text3: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_border: str
    on_accent: str
    accent_text: str
    subtle_hover: str
    subtle_pressed: str
    selected: str
    focus: str
    success: str
    success_bg: str
    caution: str
    caution_bg: str
    critical: str
    critical_bg: str
    info_bg: str
    preview_bg: str


_current: Tokens | None = None
_fonts: dict[str, str] = {}


def _mix(a: QColor, b: QColor, amount: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * amount),
                  round(a.green() + (b.green() - a.green()) * amount),
                  round(a.blue() + (b.blue() - a.blue()) * amount))


def _luminance(color: QColor) -> float:
    def channel(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + \
        0.0722 * channel(color.blue())


def _contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def system_accent(dark: bool) -> QColor:
    accent = QGuiApplication.palette().color(QPalette.ColorRole.Accent)
    if not accent.isValid() or accent.alpha() == 0:
        accent = QColor("#4CC2FF" if dark else "#0067C0")
    # Keep accent fills readable against the window in both themes.
    window = QColor("#202020" if dark else "#F3F3F3")
    target = QColor("white" if dark else "black")
    for _ in range(6):
        if _contrast(accent, window) >= 3.0:
            break
        accent = _mix(accent, target, 0.2)
    return accent


def build_tokens(dark: bool) -> Tokens:
    accent = system_accent(dark)
    on_accent = QColor("black") if _contrast(accent, QColor("black")) >= \
        _contrast(accent, QColor("white")) else QColor("white")
    if dark:
        hover = _mix(accent, QColor("black"), 0.10)
        pressed = _mix(accent, QColor("black"), 0.20)
        accent_text = _mix(accent, QColor("white"), 0.15)
        return Tokens(
            dark=True, window="#202020", card="#2B2B2B", card_hover="#323232",
            card_border="#1D1D1D", divider="#3A3A3A", text="#FFFFFF", text2="#C5C5C5",
            text3="#9D9D9D", accent=accent.name(), accent_hover=hover.name(),
            accent_pressed=pressed.name(), accent_border=_mix(accent, QColor("white"), 0.15).name(),
            on_accent=on_accent.name(), accent_text=accent_text.name(),
            subtle_hover="#2D2D2D", subtle_pressed="#292929", selected="#2D2D2D",
            focus="#FFFFFF", success="#6CCB5F", success_bg="#2B3A28", caution="#FCE100",
            caution_bg="#433A19", critical="#FF99A4", critical_bg="#442726", info_bg="#2B2B2B",
            preview_bg="#141414")
    hover = _mix(accent, QColor("white"), 0.10)
    pressed = _mix(accent, QColor("white"), 0.20)
    accent_text = _mix(accent, QColor("black"), 0.10)
    return Tokens(
        dark=False, window="#F3F3F3", card="#FFFFFF", card_hover="#FBFBFB",
        card_border="#E5E5E5", divider="#EAEAEA", text="#1B1B1B", text2="#5D5D5D",
        text3="#8A8A8A", accent=accent.name(), accent_hover=hover.name(),
        accent_pressed=pressed.name(), accent_border=_mix(accent, QColor("black"), 0.15).name(),
        on_accent=on_accent.name(), accent_text=accent_text.name(),
        subtle_hover="#EAEAEA", subtle_pressed="#E0E0E0", selected="#EAEAEA",
        focus="#000000", success="#0F7B0F", success_bg="#DFF6DD", caution="#9D5D00",
        caution_bg="#FFF4CE", critical="#C42B1C", critical_bg="#FDE7E9", info_bg="#F6F6F6",
        preview_bg="#1B1B1B")


def current() -> Tokens:
    if _current is None:
        return build_tokens(QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark)
    return _current


def font_family(name: str) -> str:
    """The requested family, or a Windows 10 fallback when it is missing."""
    return _fonts.get(name, name)


def font(size_px: float, *, strong: bool = False, display: bool = False) -> QFont:
    family = font_family(DISPLAY_FONT if display else TEXT_STRONG if strong else TEXT_FONT)
    f = QFont(family)
    f.setPixelSize(round(size_px))
    if strong and family == FALLBACK_TEXT:
        f.setWeight(QFont.Weight.DemiBold)
    return f


def icon_font(size_px: float) -> QFont:
    f = QFont(font_family(ICON_FONT))
    f.setPixelSize(round(size_px))
    return f


def stylesheet(t: Tokens) -> str:
    text = font_family(TEXT_FONT)
    strong = font_family(TEXT_STRONG)
    display = font_family(DISPLAY_FONT)
    return f"""
QWidget#AppRoot, QDialog {{ background: {t.window}; }}
QWidget {{ color: {t.text}; }}
QScrollArea, QScrollArea > QWidget > QWidget#PageContent {{ background: transparent; border: none; }}
QWidget#PageContent, QWidget#Transparent {{ background: transparent; }}

QFrame#Card {{ background: {t.card}; border: 1px solid {t.card_border}; border-radius: 8px; }}
QFrame#Card[interactive="true"]:hover {{ background: {t.card_hover}; }}
QFrame#Divider {{ background: {t.divider}; border: none; min-height: 1px; max-height: 1px; }}
QFrame#Preview {{ background: {t.preview_bg}; border-radius: 12px; }}

QLabel {{ background: transparent; font-family: "{text}"; font-size: 14px; }}
QLabel#PageTitle {{ font-family: "{display}"; font-size: 28px; }}
QLabel#HeroTitle {{ font-family: "{display}"; font-size: 20px; }}
QLabel#Metric {{ font-family: "{display}"; font-size: 28px; }}
QLabel#SectionTitle {{ font-family: "{strong}"; font-size: 14px; padding-top: 6px; }}
QLabel#CardTitle {{ font-family: "{strong}"; font-size: 14px; }}
QLabel#Secondary {{ color: {t.text2}; font-size: 12px; }}
QLabel#Body2 {{ color: {t.text2}; font-size: 14px; }}
QLabel#Caption {{ color: {t.text3}; font-size: 12px; }}
QLabel#Link {{ color: {t.accent_text}; }}

QLabel#Pill {{ border-radius: 10px; padding: 2px 10px; font-family: "{strong}"; font-size: 12px; }}
QLabel#Pill[tone="success"] {{ background: {t.success_bg}; color: {t.success}; }}
QLabel#Pill[tone="caution"] {{ background: {t.caution_bg}; color: {t.caution}; }}
QLabel#Pill[tone="critical"] {{ background: {t.critical_bg}; color: {t.critical}; }}
QLabel#Pill[tone="neutral"] {{ background: {t.subtle_hover}; color: {t.text2}; }}
QLabel#Pill[tone="accent"] {{ background: {t.accent}; color: {t.on_accent}; }}

QFrame#Banner {{ border-radius: 6px; border: 1px solid {t.card_border}; background: {t.info_bg}; }}
QFrame#Banner[tone="success"] {{ background: {t.success_bg}; }}
QFrame#Banner[tone="caution"] {{ background: {t.caution_bg}; }}
QFrame#Banner[tone="critical"] {{ background: {t.critical_bg}; }}

QPushButton#Accent {{
    background: {t.accent}; color: {t.on_accent}; border: 1px solid {t.accent_border};
    border-radius: 4px; padding: 4px 16px; font-family: "{text}"; font-size: 14px;
}}
QPushButton#Accent:hover {{ background: {t.accent_hover}; }}
QPushButton#Accent:pressed {{ background: {t.accent_pressed}; }}
QPushButton#Accent:disabled {{ background: {t.subtle_pressed}; color: {t.text3}; border-color: {t.subtle_pressed}; }}
QPushButton#Accent:focus {{ border: 2px solid {t.focus}; padding: 3px 15px; }}
QPushButton#Subtle {{
    background: transparent; border: none; border-radius: 4px; padding: 4px 10px;
    color: {t.accent_text}; font-size: 14px;
}}
QPushButton#Subtle:hover {{ background: {t.subtle_hover}; }}
QPushButton#Subtle:pressed {{ background: {t.subtle_pressed}; }}
QPushButton#Subtle:focus {{ border: 2px solid {t.focus}; padding: 2px 8px; }}
QPushButton#Subtle:disabled {{ color: {t.text3}; }}

QPushButton#Glyph {{
    background: transparent; border: none; border-radius: 4px; padding: 0;
    color: {t.text2}; font-family: "{font_family(ICON_FONT)}"; font-size: 13px;
}}
QPushButton#Glyph:hover {{ background: {t.subtle_hover}; color: {t.text}; }}
QPushButton#Glyph:pressed {{ background: {t.subtle_pressed}; }}
QPushButton#Glyph:focus {{ border: 2px solid {t.focus}; }}
QPushButton#Glyph:checked {{ color: {t.accent_text}; }}

QFrame#Toast {{
    background: {t.card}; border: 1px solid {t.divider}; border-bottom: 2px solid {t.divider};
    border-radius: 8px;
}}

QToolTip {{
    background: {t.card}; color: {t.text}; border: 1px solid {t.card_border};
    border-radius: 4px; padding: 6px 8px; font-family: "{text}"; font-size: 12px;
}}
"""


def apply_theme(app: QApplication, preference: str) -> Tokens:
    """Apply light/dark/system appearance; returns the active tokens."""
    global _current
    if not _fonts:
        from PySide6.QtGui import QFontDatabase
        families = set(QFontDatabase.families())
        for wanted, fallback in ((TEXT_FONT, FALLBACK_TEXT), (TEXT_STRONG, FALLBACK_TEXT),
                                 (DISPLAY_FONT, FALLBACK_TEXT), (ICON_FONT, FALLBACK_ICON)):
            _fonts[wanted] = wanted if wanted in families else fallback
    hints = QGuiApplication.styleHints()
    scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(
        preference, Qt.ColorScheme.Unknown)
    if hasattr(hints, "setColorScheme"):
        hints.setColorScheme(scheme)
    if app.style().name().lower() != "windows11":
        app.setStyle("windows11")
    base = QFont(font_family(TEXT_FONT))
    base.setPixelSize(14)
    app.setFont(base)
    dark = hints.colorScheme() == Qt.ColorScheme.Dark
    _current = build_tokens(dark)
    app.setStyleSheet(stylesheet(_current))
    return _current

from __future__ import annotations

from pathlib import Path

TEXT = "#0f172a"
MUTED = "#64748b"
BORDER = "#e2e8f0"
SURFACE = "#ffffff"
SUCCESS = "#16a34a"
WARNING = "#d97706"
DANGER = "#dc2626"
INFO = "#2563eb"
TODAY_BG = "#fef3c7"

# Gradient (start, end) per migration block.
TONES = {
    "current": ("#2563eb", "#06b6d4"),
    "next": ("#7c3aed", "#db2777"),
}
AVATAR_COLORS = ("#2563eb", "#7c3aed", "#db2777", "#ea580c", "#059669", "#0891b2", "#4f46e5", "#c026d3")
# Chip (background, foreground)
CHIPS = {
    "blue": ("#dbeafe", "#1d4ed8"),
    "violet": ("#ede9fe", "#6d28d9"),
    "green": ("#dcfce7", "#15803d"),
    "amber": ("#fef3c7", "#b45309"),
    "red": ("#fee2e2", "#b91c1c"),
    "slate": ("#f1f5f9", "#475569"),
}

_CHECK_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
    "<path d='M3.5 8.5l3 3 6-7' fill='none' stroke='white' stroke-width='2.2' "
    "stroke-linecap='round' stroke-linejoin='round'/></svg>"
)


def _gradient(start: str, end: str, diagonal: bool = True) -> str:
    return f"qlineargradient(x1:0, y1:0, x2:1, y2:{1 if diagonal else 0}, stop:0 {start}, stop:1 {end})"


def build_stylesheet(asset_dir: Path) -> str:
    asset_dir.mkdir(parents=True, exist_ok=True)
    check = asset_dir / "check.svg"
    check.write_text(_CHECK_SVG, encoding="utf-8")

    tones = "\n".join(
        f'#DetailsHeader[tone="{name}"], #MigrationTab[tone="{name}"][selected="true"] '
        f'{{ background: {_gradient(start, end)}; border: none; }}\n'
        f'#MigrationTab[tone="{name}"] QLabel[role="tabBadge"] {{ color: {start}; }}\n'
        f'#MigrationTab[tone="{name}"][selected="true"] QLabel[role="tabBadge"] {{ color: white; }}'
        for name, (start, end) in TONES.items()
    )
    selected_chips = "\n".join(
        f'#MigrationTab[selected="true"] QLabel[chip="{name}"] {{ background: rgba(255, 255, 255, 60); color: white; }}'
        for name in CHIPS
    )
    chips = "\n".join(
        f'QLabel[chip="{name}"] {{ background: {bg}; color: {fg}; border-radius: 10px; '
        f'padding: 2px 10px; font-size: 9pt; font-weight: 600; }}'
        for name, (bg, fg) in CHIPS.items()
    )
    return f"""
QWidget {{ font-family: "Segoe UI"; font-size: 10pt; color: {TEXT}; }}
#Root {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #e0e7ff, stop:0.5 #f8fafc, stop:1 #fce7f3); }}

QLabel[role="section"] {{ font-size: 12pt; font-weight: 700; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="caption"] {{ color: {MUTED}; font-size: 8pt; font-weight: 700; }}

#MigrationTab, #Panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
#MigrationTab:hover {{ border-color: #94a3b8; }}
#MigrationTab[selected="true"] QLabel {{ color: white; background: transparent; }}
#MigrationTab[selected="true"] #Skeleton {{ background: rgba(255, 255, 255, 90); }}
QLabel[role="tabBadge"] {{ font-size: 8pt; font-weight: 700; }}
QLabel[role="tabTitle"] {{ font-size: 20pt; font-weight: 700; }}
QLabel[role="tabTitleSmall"] {{ font-size: 14pt; font-weight: 700; }}
#Divider {{ min-height: 2px; max-height: 2px; border: none;
            background: {_gradient("#2563eb", "#db2777", False)}; }}
#Today {{ border: none; border-radius: 12px; background: {_gradient("#94a3b8", "#cbd5e1")}; }}
#Today[state="due"] {{ background: {_gradient("#f59e0b", "#ef4444")}; }}
#Today[state="clear"] {{ background: {_gradient("#10b981", "#06b6d4")}; }}
#Today QLabel {{ color: white; background: transparent; }}
QPushButton[role="todayItem"] {{ background: rgba(255, 255, 255, 45); border: 1px solid rgba(255, 255, 255, 120);
                                 border-radius: 6px; color: white; text-align: left; padding: 6px 10px;
                                 font-weight: 600; }}
QPushButton[role="todayItem"]:hover {{ background: rgba(255, 255, 255, 85); }}
#DetailsHeader {{ border-top-left-radius: 11px; border-top-right-radius: 11px; }}
#DetailsHeader QLabel {{ color: white; background: transparent; }}
QLabel[role="cardBadge"] {{ font-size: 8pt; font-weight: 700; }}
QLabel[role="cardTitle"] {{ font-size: 15pt; font-weight: 700; }}
QLabel[role="countdown"] {{ font-size: 12pt; font-weight: 700; }}
QLabel[role="value"] {{ font-size: 11pt; font-weight: 600; }}
#DetailsHeader QPushButton {{ background: rgba(255, 255, 255, 40); color: white; border: 1px solid rgba(255, 255, 255, 150);
                              border-radius: 6px; padding: 6px 16px; font-weight: 600; }}
#DetailsHeader QPushButton:hover {{ background: rgba(255, 255, 255, 80); }}
#DetailsHeader QPushButton:disabled {{ color: rgba(255, 255, 255, 120); border-color: rgba(255, 255, 255, 70);
                                       background: transparent; }}
#Skeleton {{ background: #e2e8f0; border-radius: 5px; }}

QPushButton {{ background: {SURFACE}; border: 1px solid #cbd5e1; border-radius: 6px; padding: 5px 14px; }}
QPushButton:hover {{ background: #f1f5f9; }}
QPushButton:pressed {{ background: #e2e8f0; }}
QPushButton[role="link"] {{ background: transparent; border: none; color: #2563eb; padding: 0 4px; font-weight: 600; }}
QPushButton[role="link"]:hover {{ color: #7c3aed; }}
QLineEdit {{ background: #f8fafc; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px; color: {MUTED}; }}

QTreeWidget {{ background: {SURFACE}; border: none; outline: 0; }}
QTreeWidget::item {{ padding: 7px 2px; border-bottom: 1px solid #f1f5f9; }}
QTreeWidget::item:hover {{ background: #f8fafc; }}
QTreeWidget::item:selected {{ background: #e0e7ff; color: {TEXT}; }}
QTreeWidget::indicator {{ width: 14px; height: 14px; border: 2px solid #94a3b8; border-radius: 5px; background: white; }}
QTreeWidget::indicator:hover {{ border-color: #2563eb; }}
QTreeWidget::indicator:checked {{ background: {SUCCESS}; border-color: {SUCCESS}; image: url("{check.as_posix()}"); }}
QTreeWidget::indicator:indeterminate {{ background: #fde68a; border-color: {WARNING}; }}
QHeaderView::section {{ background: {SURFACE}; border: none; border-bottom: 2px solid #e0e7ff;
                        padding: 6px 4px; color: {MUTED}; font-weight: 700; }}

QTextBrowser {{ background: {SURFACE}; border: none; }}
QSplitter::handle {{ background: transparent; }}

QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ padding: 2px 8px; }}
QProgressBar {{ border: none; background: #e2e8f0; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {_gradient("#2563eb", "#db2777", False)}; border-radius: 3px; }}
#TaskProgress {{ max-height: 8px; border-radius: 4px; }}
#TaskProgress::chunk {{ background: {_gradient("#22c55e", "#06b6d4", False)}; border-radius: 4px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #cbd5e1; border-radius: 3px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #cbd5e1; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

{tones}
{chips}
{selected_chips}
"""

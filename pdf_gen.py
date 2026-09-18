"""
PDF generation for the three deliverables:
  1. برنامج الشعب      (section-schedule grids, colored by teacher)
  2. برنامج الأساتذة   (teacher-schedule grids, colored by section)
  3. قائمة الأساتذة    (teacher roster list)

Everything here is a pure function of (data, schedule, roster) so it can be
called from a GUI or from the command line without touching global state.

Rendering is done directly with ReportLab (a pure-Python PDF library) - no
headless browser / Chromium dependency, so the whole program can be bundled
into a single lightweight, portable executable (see BUILD.md). Arabic text
is reshaped and bidi-reordered by hand (arabic_reshaper + python-bidi)
before being handed to ReportLab, since ReportLab itself does not do
contextual Arabic shaping or right-to-left reordering. The bundled Amiri
font is embedded directly from assets/fonts so this does not depend on any
Arabic font being installed on the user's system.
"""

import colorsys
import math
import pathlib
from xml.sax.saxutils import escape as _xml_escape

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.graphics.shapes import Drawing, Wedge, String, Rect

import scheduler

ASSETS_DIR = pathlib.Path(__file__).parent / "assets" / "fonts"
PAGE_SIZE = landscape(A4)
MARGIN = 10 * mm

_fonts_ready = False


def _ensure_fonts():
    """Register the bundled Amiri font once; fall back to Helvetica (no
    Arabic glyphs, but still a valid PDF) if the font files are missing."""
    global _fonts_ready
    if _fonts_ready:
        return
    regular = ASSETS_DIR / "Amiri-Regular.ttf"
    bold = ASSETS_DIR / "Amiri-Bold.ttf"
    try:
        if regular.exists():
            pdfmetrics.registerFont(TTFont("Amiri", str(regular)))
        if bold.exists():
            pdfmetrics.registerFont(TTFont("Amiri-Bold", str(bold)))
    except Exception:
        pass
    _fonts_ready = True


def _font_name():
    _ensure_fonts()
    return "Amiri" if "Amiri" in pdfmetrics.getRegisteredFontNames() else "Helvetica"


def _font_bold_name():
    _ensure_fonts()
    return "Amiri-Bold" if "Amiri-Bold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"


def ar(text):
    """Reshape Arabic letters into their contextual joined forms and
    reorder the string into visual (left-to-right-drawable) order, so a
    plain ReportLab drawString/Paragraph shows it correctly. Non-Arabic
    text passes through essentially unchanged."""
    if text is None:
        return ""
    text = str(text)
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _ar_escaped(text):
    return _xml_escape(ar(text))


# ---------------------------------------------------------------- colors --

def _hsl_to_hex(h, s, l):
    h = (h % 360) / 360.0
    s = max(0.0, min(1.0, s / 100.0))
    l = max(0.0, min(1.0, l / 100.0))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def _gen_colors_pair(n):
    """Golden-ratio hue stepping -> visually distinct (bg, border) hex pairs."""
    bg, border = [], []
    golden = 0.61803398875
    hue = 0.15
    for _ in range(max(n, 0)):
        hue = (hue + golden) % 1.0
        bg.append(_hsl_to_hex(hue * 360, 68, 87))
        border.append(_hsl_to_hex(hue * 360, 55, 55))
    return bg, border


# ------------------------------------------------------------ paragraphs --

def _pstyle(size, bold=False, color="#222222", leading=None):
    return ParagraphStyle(
        name=f"s_{size}_{bold}_{color}",
        fontName=_font_bold_name() if bold else _font_name(),
        fontSize=size,
        leading=leading or round(size * 1.25, 1),
        alignment=TA_CENTER,
        textColor=colors.HexColor(color),
    )


def _p(text, size, bold=False, color="#222222"):
    return Paragraph(_ar_escaped(text), _pstyle(size, bold=bold, color=color))


def _cell_paragraph(line1, line2, size1=9.5, size2=7.7):
    """Two-line cell: bold subject/name line + a smaller secondary line."""
    style = ParagraphStyle(
        name="cell", fontName=_font_name(), fontSize=size1,
        leading=size1 + 1.5, alignment=TA_CENTER, textColor=colors.HexColor("#1a1a1a"),
    )
    txt = (
        f'<font name="{_font_bold_name()}" size="{size1}">{_ar_escaped(line1)}</font>'
        f'<br/><font size="{size2}" color="#333333">{_ar_escaped(line2)}</font>'
    )
    return Paragraph(txt, style)


# --------------------------------------------------------------- helpers --

def _new_doc(out_path, title):
    return SimpleDocTemplate(
        str(out_path), pagesize=PAGE_SIZE,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
        title=title,
    )


def _cover_flowables(title, subtitle, meta_line):
    flow = [Spacer(1, 55 * mm)]
    flow.append(_p(title, 30, bold=True, color="#1a1a1a"))
    flow.append(Spacer(1, 4 * mm))
    flow.append(_p(subtitle, 15, color="#555555"))
    flow.append(Spacer(1, 12 * mm))
    flow.append(_p(meta_line, 11, color="#777777"))
    flow.append(PageBreak())
    return flow


def _weekly_grid_table(title_text, days, periods_per_day, cell_at, color_lookup,
                        footer_note=None, warning_notes=None):
    """
    Build [title, table, optional footer] flowables for one weekly grid.

    `cell_at(period_idx, day_idx)` -> None, or (line1, line2, color_key).
    `color_lookup[color_key]` -> (bg_hex, border_hex).
    `warning_notes` -> optional list of soft-constraint-violation note
    strings (a teacher can have more than one, e.g. both "تفريغ حصص" and
    "يوم عطلة" unmet on the same schedule).

    Columns are laid out right-to-left (period/"corner" column rightmost,
    then day[0], day[1], ... progressing left) to match how an Arabic
    weekly timetable is read, since ReportLab tables are always laid out
    left-to-right internally.
    """
    ndays = len(days)
    avail_width = PAGE_SIZE[0] - 2 * MARGIN
    corner_w = avail_width * 0.075
    day_w = (avail_width - corner_w) / ndays
    col_widths = [day_w] * ndays + [corner_w]
    corner_col = ndays  # rightmost column index (0-based, left-to-right list)

    # Reserve extra room below the table when there are warning_notes (each
    # can wrap to 2-3 lines depending on how many days it lists), so they
    # land right under this teacher's grid instead of spilling onto an
    # otherwise-blank extra page.
    reserved = 30 * mm + (22 * mm * len(warning_notes) if warning_notes else 0)
    avail_height = PAGE_SIZE[1] - 2 * MARGIN - reserved
    header_h = 9 * mm
    row_h = max(min((avail_height - header_h) / periods_per_day, 22 * mm), 12 * mm)

    header_style = _pstyle(11, bold=True, color="#ffffff")
    header_row = [Paragraph(_ar_escaped(days[d]), header_style) for d in reversed(range(ndays))]
    header_row.append(Paragraph(_ar_escaped("الحصة"), header_style))

    pnum_style = _pstyle(11, bold=True, color="#222222")
    data_rows = [header_row]
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (corner_col, 1), (corner_col, -1), colors.HexColor("#eef2f7")),
    ]
    for p in range(periods_per_day):
        r = p + 1
        row = []
        for pos, d in enumerate(reversed(range(ndays))):
            entry = cell_at(p, d)
            if entry is None:
                row.append("")
                style_cmds.append(("BACKGROUND", (pos, r), (pos, r), colors.HexColor("#fafafa")))
            else:
                line1, line2, color_key = entry
                bg_hex, bd_hex = color_lookup[color_key]
                row.append(_cell_paragraph(line1, line2))
                style_cmds.append(("BACKGROUND", (pos, r), (pos, r), colors.HexColor(bg_hex)))
                for side in ("LINEABOVE", "LINEBELOW", "LINEBEFORE", "LINEAFTER"):
                    style_cmds.append((side, (pos, r), (pos, r), 1.3, colors.HexColor(bd_hex)))
        row.append(Paragraph(str(p + 1), pnum_style))
        data_rows.append(row)

    tbl = Table(data_rows, colWidths=col_widths, rowHeights=[header_h] + [row_h] * periods_per_day)
    tbl.setStyle(TableStyle(style_cmds))

    flow = [_p(title_text, 16, bold=True, color="#1a1a1a"), Spacer(1, 3 * mm), tbl]
    if footer_note:
        flow.append(Spacer(1, 2.5 * mm))
        flow.append(_p(footer_note, 10, color="#555555"))
    if warning_notes:
        # Soft-constraint violation notes (e.g. "تفريغ حصص" or "يوم عطلة"
        # couldn't be fully honored some days) - styled distinctly (amber,
        # bold) so they read as explanatory flags rather than routine info.
        for note in warning_notes:
            flow.append(Spacer(1, 1.5 * mm))
            flow.append(_p(note, 9.5, bold=True, color="#9a5b00"))
    flow.append(PageBreak())
    return flow


# --------------------------------------------------------- section/teacher --

def _prepare_schedule_info(data, section_sched):
    """
    One-time prep shared by both the section grids and the teacher grids
    (and by however many TEACHER-GRID VARIANTS get built - see
    generate_all_pdfs, which can now build the teacher program twice: once
    with the full detailed constraint notes, once with a short generic
    note) - the section list, each teacher's subjects, and both color
    palettes never depend on which notes variant is being rendered, so
    computing them again per variant would be pure waste.
    """
    section_ids = scheduler.build_section_ids(data)

    teacher_subjects = {}
    for sid in section_ids:
        cells = section_sched[f"{sid[0]}|{sid[1]}"]
        for c in cells:
            if c:
                teacher_subjects.setdefault(c["teacher"], set()).add(c["subject"])
    teacher_names = sorted(teacher_subjects.keys())

    t_bg, t_border = _gen_colors_pair(len(teacher_names))
    teacher_colors = {name: (t_bg[i], t_border[i]) for i, name in enumerate(teacher_names)}

    section_keys = [f"{t}-{s}" for (t, s) in section_ids]
    s_bg, s_border = _gen_colors_pair(len(section_keys))
    section_colors = {key: (s_bg[i], s_border[i]) for i, key in enumerate(section_keys)}

    return section_ids, teacher_names, teacher_subjects, teacher_colors, section_colors


def _build_section_flowables(data, section_sched, section_ids, teacher_colors):
    days = data["meta"]["days"]
    periods_per_day = data["meta"]["periods_per_day"]
    grade_labels = data["meta"]["grade_labels"]

    def slot_idx(day, period):
        return day * periods_per_day + period

    flow_sections = list(_cover_flowables(
        "برنامج الشعب الأسبوعي",
        f"{len(days)} أيام × {periods_per_day} حصص يومياً",
        f"{len(section_ids)} شعبة  —  كل خانة ملوّنة حسب الأستاذ المكلّف",
    ))
    for (track, s) in section_ids:
        cells = section_sched[f"{track}|{s}"]

        def cell_at(p, d, cells=cells):
            c = cells[slot_idx(d, p)]
            if c is None:
                return None
            return (c["subject"], c["teacher"], c["teacher"])

        title = f"{grade_labels[track]} — الشعبة {s}"
        flow_sections.extend(_weekly_grid_table(title, days, periods_per_day, cell_at, teacher_colors))

    return flow_sections


def _build_teacher_flowables(data, section_sched, section_ids, teacher_names, teacher_subjects,
                              section_colors, constraint_notes=None):
    days = data["meta"]["days"]
    periods_per_day = data["meta"]["periods_per_day"]
    nslots = len(days) * periods_per_day
    grade_labels = data["meta"]["grade_labels"]

    def slot_idx(day, period):
        return day * periods_per_day + period

    flow_teachers = list(_cover_flowables(
        "برنامج الأساتذة الأسبوعي",
        f"{len(days)} أيام × {periods_per_day} حصص يومياً",
        f"{len(teacher_names)} أستاذاً  —  كل خانة ملوّنة حسب الشعبة",
    ))
    for tname in teacher_names:
        grid = [None] * nslots
        for (track, s) in section_ids:
            cells = section_sched[f"{track}|{s}"]
            for idx in range(nslots):
                c = cells[idx]
                if c and c["teacher"] == tname:
                    grid[idx] = {"track": track, "section": s, "subject": c["subject"]}
        total = sum(1 for c in grid if c)

        def cell_at(p, d, grid=grid):
            c = grid[slot_idx(d, p)]
            if c is None:
                return None
            key = f"{c['track']}-{c['section']}"
            sec_label = f"{grade_labels[c['track']]} / ش{c['section']}"
            return (c["subject"], sec_label, key)

        subs = "، ".join(sorted(teacher_subjects[tname]))
        title = f"{tname} ({subs})"
        footer = f"إجمالي الحصص الأسبوعية: {total}"
        warnings = (constraint_notes or {}).get(tname)
        flow_teachers.extend(_weekly_grid_table(title, days, periods_per_day, cell_at, section_colors,
                                                  footer, warning_notes=warnings))

    return flow_teachers




# ---------------------------------------------------------------- roster --

def _build_list_flowables(rows, multi):
    flow = [
        _p("قائمة الأساتذة حسب برنامج الحصص", 22, bold=True, color="#1a1a1a"),
        Spacer(1, 2 * mm),
    ]
    n_unique = len(set(r["teacher"] for r in rows))
    subtitle = (
        f"{len(rows)} سطراً — {n_unique} أستاذاً فعلياً، مرتبة حسب المادة. "
        f"(*) يدرّس هذا الأستاذ أكثر من مادة"
    )
    flow.append(_p(subtitle, 11, color="#555555"))
    flow.append(Spacer(1, 5 * mm))

    # Column order matches an RTL reading direction: "#" is the rightmost
    # column, so it is placed LAST in this left-to-right ReportLab table.
    header_labels = ["الحصص", "الشعب", "الصفوف", "المادة", "اسم الأستاذ", "#"]
    header_style = _pstyle(11, bold=True, color="#ffffff")
    header_row = [Paragraph(_ar_escaped(h), header_style) for h in header_labels]

    name_style = _pstyle(10, bold=True, color="#1a1a1a")
    subj_style = _pstyle(10, bold=True, color="#1a1a1a")
    plain_style = _pstyle(9.5, color="#333333")
    center_style = _pstyle(10, color="#333333")

    data_rows = [header_row]
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    prev_subject = None
    for i, r in enumerate(rows, start=1):
        first_of_group = r["subject"] != prev_subject
        prev_subject = r["subject"]
        star = " *" if r["teacher"] in multi else ""
        row = [
            Paragraph(str(r["total"]), center_style),
            Paragraph(_ar_escaped(r["sections"]), plain_style),
            Paragraph(_ar_escaped(r["grades"]), plain_style),
            Paragraph(_ar_escaped(r["subject"]), subj_style),
            Paragraph(_ar_escaped(r["teacher"] + star), name_style),
            Paragraph(str(i), center_style),
        ]
        data_rows.append(row)
        r_idx = i
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, r_idx), (-1, r_idx), colors.HexColor("#f7f9fc")))
        if first_of_group:
            style_cmds.append(("LINEABOVE", (0, r_idx), (-1, r_idx), 1.4, colors.HexColor("#1F4E78")))

    avail_width = PAGE_SIZE[0] - 2 * MARGIN
    col_widths = [w * avail_width for w in (0.06, 0.34, 0.18, 0.18, 0.18, 0.06)]
    tbl = Table(data_rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    flow.append(tbl)

    if multi:
        flow.append(Spacer(1, 6 * mm))
        flow.append(_p("ملاحظة — أساتذة يدرّسون أكثر من مادة (المجموع الفعلي لنصابهم الأسبوعي):",
                        10.5, bold=True, color="#333333"))
        for name, info in multi.items():
            subs = "، ".join(info["subjects"])
            line = f"— {name}: {subs} — الإجمالي {info['total']} حصة/أسبوع"
            flow.append(_p(line, 10, color="#444444"))

    return flow


def _build_constraints_report_flowables(report_rows):
    """
    Build "تقرير الشروط الخاصة بجدولة الأساتذة" - a standalone report PDF
    (no weekly grids, unlike the other three) listing, one block per
    teacher, every scheduling constraint actually active for them in full
    detail: exact day names, exact counts, exact positions - not just
    which options are turned on. report_rows comes from
    scheduler.teacher_constraints_report(data), so this is independent of
    section_sched: it can be built even before the solver has run, and
    only lists teachers who have at least one constraint enabled.
    """
    flow = [
        _p("تقرير الشروط الخاصة بجدولة الأساتذة", 22, bold=True, color="#1a1a1a"),
        Spacer(1, 2 * mm),
    ]
    n_items = sum(len(r["items"]) for r in report_rows)
    subtitle = (
        f"{len(report_rows)} أستاذاً لديهم شروط جدولة مفعّلة ({n_items} شرطاً بالإجمالي) — "
        f"\"خاص\" يعني أن هذا الشرط مخصص لهذا الأستاذ تحديداً، و\"عام\" يعني أنه موروث من "
        f"الإعداد العام المطبَّق على كل الأساتذة."
    )
    flow.append(_p(subtitle, 10.5, color="#555555"))
    flow.append(Spacer(1, 6 * mm))

    header_style = _pstyle(10, bold=True, color="#ffffff")
    detail_style = _pstyle(10, color="#333333")
    source_style = _pstyle(9.5, color="#333333")
    title_style = _pstyle(10, bold=True, color="#1a1a1a")

    avail_width = PAGE_SIZE[0] - 2 * MARGIN
    # Same RTL convention as _build_list_flowables: the rightmost visual
    # column ("الشرط") is placed LAST in this left-to-right column list.
    col_widths = [w * avail_width for w in (0.60, 0.18, 0.22)]

    for r in report_rows:
        header_line = r["teacher"]
        if r["subjects"]:
            header_line += "  (" + "، ".join(r["subjects"]) + ")"
        flow.append(_p(header_line, 13, bold=True, color="#1F4E78"))
        flow.append(Spacer(1, 2 * mm))

        data_rows = [[
            Paragraph(_ar_escaped("التفاصيل"), header_style),
            Paragraph(_ar_escaped("المصدر"), header_style),
            Paragraph(_ar_escaped("الشرط"), header_style),
        ]]
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for i, item in enumerate(r["items"], start=1):
            data_rows.append([
                Paragraph(_ar_escaped(item["detail"]), detail_style),
                Paragraph(_ar_escaped(item["source"]), source_style),
                Paragraph(_ar_escaped(item["title"]), title_style),
            ])
            if i % 2 == 0:
                style_cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f7f9fc")))

        tbl = Table(data_rows, colWidths=col_widths)
        tbl.setStyle(TableStyle(style_cmds))
        flow.append(tbl)
        flow.append(Spacer(1, 6 * mm))

    return flow


# ------------------------------------------------------ fulfillment report --

_FULFILLMENT_COLORS = {
    "كامل": "#2e7d32",
    "جزئي": "#f9a825",
    "معدوم": "#c62828",
}
_FULFILLMENT_LABELS = {
    "كامل": "تحقق بالكامل",
    "جزئي": "تحقق جزئياً",
    "معدوم": "لم يتحقق إطلاقاً",
}


def _build_fulfillment_chart(counts):
    """
    A donut chart broken into scheduler.FULFILLMENT_CATEGORIES slices, plus
    a legend with counts and percentages - drawn by hand with
    reportlab.graphics primitives (Wedge/String/Rect) rather than a
    charting library or matplotlib, so this stays a zero-extra-dependency
    addition consistent with the rest of the module (see the module
    docstring). counts is scheduler.fulfillment_category_counts(...)'s
    {"كامل": n, "جزئي": n, "معدوم": n} dict.
    """
    width, height = 250 * mm, 95 * mm
    d = Drawing(width, height)
    total = sum(counts.values())

    cx, cy, r_outer, r_inner = 55 * mm, height / 2, 40 * mm, 23 * mm

    if total == 0:
        d.add(String(width / 2, cy, ar("لا يوجد أساتذة لديهم قيود مفعّلة"),
                      fontName=_font_name(), fontSize=11, fillColor=colors.HexColor("#777777"),
                      textAnchor="middle"))
        return d

    # Angles: reportlab's Wedge sweeps counter-clockwise from startangledegrees
    # to endangledegrees, so going clockwise around the circle (the usual
    # reading order for this kind of chart) means DECREASING the angle as
    # each slice is added, starting from 90° (12 o'clock).
    start = 90.0
    for cat in scheduler.FULFILLMENT_CATEGORIES:
        n = counts.get(cat, 0)
        if n <= 0:
            continue
        sweep = 360.0 * n / total
        end = start - sweep
        d.add(Wedge(cx, cy, r_outer, end, start, radius1=r_inner,
                     fillColor=colors.HexColor(_FULFILLMENT_COLORS[cat]),
                     strokeColor=colors.white, strokeWidth=1.5))
        start = end

    d.add(String(cx, cy + 3, str(total), fontName=_font_bold_name(), fontSize=22,
                  fillColor=colors.HexColor("#1a1a1a"), textAnchor="middle"))
    d.add(String(cx, cy - 13, ar("أستاذاً"), fontName=_font_name(), fontSize=10,
                  fillColor=colors.HexColor("#555555"), textAnchor="middle"))

    lx = 125 * mm
    ly = height - 18 * mm
    row_h = 24 * mm
    for cat in scheduler.FULFILLMENT_CATEGORIES:
        n = counts.get(cat, 0)
        pct = (100.0 * n / total) if total else 0.0
        d.add(Rect(lx, ly - 6 * mm, 9 * mm, 9 * mm,
                    fillColor=colors.HexColor(_FULFILLMENT_COLORS[cat]), strokeColor=None))
        label = f"{_FULFILLMENT_LABELS[cat]} — {n} ({pct:.1f}%)"
        d.add(String(lx + 13 * mm, ly - 2.5 * mm, ar(label), fontName=_font_bold_name(),
                      fontSize=13, fillColor=colors.HexColor("#1a1a1a"), textAnchor="start"))
        ly -= row_h

    return d


def _build_fulfillment_report_flowables(rows, counts):
    """
    Build "تقرير نسبة تحقق رغبات الأساتذة" - a standalone report PDF: a
    donut chart summarizing how many teachers had their scheduling
    preferences fully honored, partially honored, or not honored at all,
    followed by one detail row per teacher (worst-first) with their exact
    percentage, status, and every constraint they had active. rows/counts
    come from scheduler.teacher_fulfillment_report(...) and
    scheduler.fulfillment_category_counts(rows).
    """
    flow = [
        _p("تقرير نسبة تحقق رغبات الأساتذة في الجدولة", 20, bold=True, color="#1a1a1a"),
        Spacer(1, 2 * mm),
    ]
    total = sum(counts.values())
    subtitle = (
        f"{total} أستاذاً لديهم شروط جدولة مفعّلة - التصنيف حسب النسبة الفعلية التي "
        f"تحققت من طلب كل أستاذ بعد حل الجدول."
    )
    flow.append(_p(subtitle, 10.5, color="#555555"))
    flow.append(Spacer(1, 6 * mm))
    flow.append(_build_fulfillment_chart(counts))
    flow.append(Spacer(1, 8 * mm))

    # الأسوأ نسبة تحقق أولاً، حتى تظهر الحالات التي تحتاج تدخلاً في أعلى
    # الجدول مباشرة بدل دفنها بين عشرات الأساتذة الذين تحقق لهم كل شيء.
    sorted_rows = sorted(rows, key=lambda r: r["percent"])

    header_style = _pstyle(10, bold=True, color="#ffffff")
    name_style = _pstyle(10, bold=True, color="#1a1a1a")
    subj_style = _pstyle(9, color="#666666")
    items_style = _pstyle(9.5, color="#333333")
    pct_style = _pstyle(12, bold=True, color="#1a1a1a")

    # RTL column order: "الأستاذ" is the rightmost column, so it is placed
    # LAST in this left-to-right ReportLab column list (same convention as
    # _build_list_flowables / _build_constraints_report_flowables).
    header_labels = ["نسبة التحقق", "الحالة", "الشروط المفعّلة", "الأستاذ"]
    header_row = [Paragraph(_ar_escaped(h), header_style) for h in header_labels]

    data_rows = [header_row]
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for i, r in enumerate(sorted_rows, start=1):
        cat_color = _FULFILLMENT_COLORS[r["category"]]
        cat_style = _pstyle(10, bold=True, color=cat_color)
        items_text = "  |  ".join(f"{it['title']}: {it['detail']}" for it in r["items"])
        name_line = r["teacher"]
        if r["subjects"]:
            name_line += "\n" + "، ".join(r["subjects"])
        data_rows.append([
            Paragraph(_ar_escaped(f"{r['percent']:.1f}%"), pct_style),
            Paragraph(_ar_escaped(_FULFILLMENT_LABELS[r["category"]]), cat_style),
            Paragraph(_ar_escaped(items_text), items_style),
            _cell_paragraph(r["teacher"], "، ".join(r["subjects"]), size1=10, size2=8),
        ])
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f7f9fc")))

    avail_width = PAGE_SIZE[0] - 2 * MARGIN
    col_widths = [w * avail_width for w in (0.12, 0.14, 0.46, 0.28)]
    tbl = Table(data_rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    flow.append(tbl)

    return flow


# --------------------------------------------------------------- drivers --

def generate_all_pdfs(data, section_sched, out_dir, progress=None, constraint_notes=None,
                       constraint_fulfillment=None):
    """
    Build the PDFs into out_dir (created if needed):
      برنامج_الشعب.pdf, برنامج_الأساتذة.pdf, قائمة_الأساتذة.pdf
    Returns dict {name: path}.

    constraint_notes: optional {teacher_name: [note_text, ...]} (see
    scheduler.solve_timetable) - printed at the end of that teacher's page
    in برنامج_الأساتذة.pdf when a soft scheduling preference (e.g. "تفريغ
    حصص في نهاية اليوم" or "يوم عطلة") couldn't be fully honored on every
    day.

    When constraint_notes is non-empty, a FOURTH file is also produced:
    برنامج_الأساتذة_ملاحظات_عامة.pdf - the exact same weekly grids, but
    WITHOUT any constraint-violation notes at all (not even a generic one),
    since this file is meant to be handed to teachers directly rather than
    kept only by whoever built the schedule.

    constraint_fulfillment: optional {teacher_name: {"requested", "achieved"}}
    (see scheduler.solve_timetable) - when any teacher has at least one
    enabled scheduling constraint at all (regardless of whether it was
    fully honored), a further file is produced:
    تقرير_نسبة_تحقق_رغبات_الأساتذة.pdf - a donut chart plus a detail table
    showing, per teacher, what percentage of their requested preference
    the solved schedule actually delivered (100% / partial / 0%).

    NOTE: تقرير_الشروط_الخاصة_بجدولة_الأساتذة.pdf is NOT built here - it
    has its own independent entry point, generate_constraints_report_pdf()
    below, wired to its own separate button in the GUI, since (unlike the
    files above) it needs no solved schedule at all - only the
    scheduling_constraints settings.
    """
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _ensure_fonts()

    rows, multi, _unique_count = scheduler.teacher_roster(data)

    if progress:
        progress("جاري بناء صفحات الجداول...")
    section_ids, teacher_names, teacher_subjects, teacher_colors, section_colors = \
        _prepare_schedule_info(data, section_sched)
    flow_sections = _build_section_flowables(data, section_sched, section_ids, teacher_colors)
    flow_teachers = _build_teacher_flowables(
        data, section_sched, section_ids, teacher_names, teacher_subjects,
        section_colors, constraint_notes=constraint_notes)
    flow_list = _build_list_flowables(rows, multi)

    jobs = {
        "برنامج الشعب": (flow_sections, out_dir / "برنامج_الشعب.pdf"),
        "برنامج الأساتذة": (flow_teachers, out_dir / "برنامج_الأساتذة.pdf"),
        "قائمة الأساتذة": (flow_list, out_dir / "قائمة_الأساتذة.pdf"),
    }

    if constraint_notes:
        flow_teachers_generic = _build_teacher_flowables(
            data, section_sched, section_ids, teacher_names, teacher_subjects,
            section_colors, constraint_notes=None)
        jobs["برنامج الأساتذة (نسخة عامة بدون ملاحظات)"] = (
            flow_teachers_generic, out_dir / "برنامج_الأساتذة_ملاحظات_عامة.pdf")

    fulfillment_rows = scheduler.teacher_fulfillment_report(data, constraint_fulfillment or {})
    if fulfillment_rows:
        fulfillment_counts = scheduler.fulfillment_category_counts(fulfillment_rows)
        flow_fulfillment = _build_fulfillment_report_flowables(fulfillment_rows, fulfillment_counts)
        jobs["تقرير نسبة تحقق رغبات الأساتذة"] = (
            flow_fulfillment, out_dir / "تقرير_نسبة_تحقق_رغبات_الأساتذة.pdf")

    if progress:
        progress("جاري توليد ملفات PDF...")

    for name, (flow, pdf_path) in jobs.items():
        doc = _new_doc(pdf_path, title=name)
        doc.build(flow)
        if progress:
            progress(f"تم إنشاء: {pdf_path.name}")

    return {name: str(pdf_path) for name, (_, pdf_path) in jobs.items()}


def generate_constraints_report_pdf(data, out_dir, progress=None):
    """
    Build تقرير_الشروط_الخاصة_بجدولة_الأساتذة.pdf on its own, independent
    of generate_all_pdfs() and of the solver - it only reads
    data["scheduling_constraints"], not any solved schedule, so it has its
    own separate button in the GUI rather than being bundled with
    برنامج_الشعب/برنامج_الأساتذة. Returns the output path as a string.

    Raises RuntimeError (with a clear Arabic message) if no teacher has
    any scheduling constraint enabled at all, since there would be nothing
    to put in the report.
    """
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _ensure_fonts()

    if progress:
        progress("جاري جمع قيود الجدولة لكل أستاذ...")
    rows = scheduler.teacher_constraints_report(data)
    if not rows:
        raise RuntimeError(
            "لا يوجد أي أستاذ لديه قيد جدولة مفعّل حالياً (لا عبر الإعداد العام ولا "
            "عبر تخصيص خاص) - لا يوجد ما يُدرَج في التقرير. فعِّل قيداً واحداً على الأقل "
            "من تبويب \"قيود الجدولة\" أولاً."
        )

    flow = _build_constraints_report_flowables(rows)
    pdf_path = out_dir / "تقرير_الشروط_الخاصة_بجدولة_الأساتذة.pdf"

    if progress:
        progress("جاري توليد ملف PDF...")
    doc = _new_doc(pdf_path, title="تقرير الشروط الخاصة بجدولة الأساتذة")
    doc.build(flow)
    if progress:
        progress(f"تم إنشاء: {pdf_path.name}")

    return str(pdf_path)


def _build_complexity_report_flowables(rows):
    """
    Build "ترتيب الأساتذة حسب كثرة وتعقيد الشروط" - one row per teacher
    (already sorted most-to-least demanding by
    scheduler.teacher_constraint_complexity_report), with every one of
    their active constraints listed together in a single "الشروط المفعّلة"
    column (as asked - "ذكر الشروط في عمود") rather than spread across
    several columns.
    """
    flow = [
        _p("ترتيب الأساتذة حسب كثرة وتعقيد شروط الجدولة", 20, bold=True, color="#1a1a1a"),
        Spacer(1, 2 * mm),
    ]
    subtitle = (
        f"{len(rows)} أستاذاً لديهم شروط جدولة مفعّلة، مرتّبون من الأكثر تعقيداً إلى الأقل. "
        f"\"عدد الشروط\" هو عدد أنواع القيود المفعّلة له (من 1 إلى 4)، و\"درجة التعقيد\" رقم "
        f"تراكمي يزيد أيضاً حسب صعوبة كل قيد بعينه (كعدد أيام العطلة المطلوبة أو صرامة حد "
        f"نوافذ الفراغ) - وليس فقط عدد أنواع القيود."
    )
    flow.append(_p(subtitle, 10, color="#555555"))
    flow.append(Spacer(1, 6 * mm))

    header_style = _pstyle(10, bold=True, color="#ffffff")
    items_style = _pstyle(9.5, color="#333333")
    score_style = _pstyle(11, bold=True, color="#1a1a1a")
    count_style = _pstyle(11, color="#333333")
    rank_style = _pstyle(10, color="#333333")

    # RTL column order: "#" is the rightmost column, so it is placed LAST
    # in this left-to-right ReportLab column list (same convention as the
    # other report tables in this module).
    header_labels = ["الشروط المفعّلة", "درجة التعقيد", "عدد الشروط", "الأستاذ", "#"]
    header_row = [Paragraph(_ar_escaped(h), header_style) for h in header_labels]

    data_rows = [header_row]
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for i, r in enumerate(rows, start=1):
        items_text = "  |  ".join(f"{it['title']}: {it['detail']}" for it in r["items"])
        data_rows.append([
            Paragraph(_ar_escaped(items_text), items_style),
            Paragraph(_ar_escaped(f"{r['score']:.1f}"), score_style),
            Paragraph(str(r["count"]), count_style),
            _cell_paragraph(r["teacher"], "، ".join(r["subjects"]), size1=10, size2=8),
            Paragraph(str(i), rank_style),
        ])
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f7f9fc")))

    avail_width = PAGE_SIZE[0] - 2 * MARGIN
    col_widths = [w * avail_width for w in (0.40, 0.14, 0.12, 0.28, 0.06)]
    tbl = Table(data_rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    flow.append(tbl)

    return flow


def generate_complexity_report_pdf(data, out_dir, progress=None):
    """
    Build ترتيب_الأساتذة_حسب_تعقيد_الشروط.pdf on its own - independent of
    generate_all_pdfs() and of the solver, exactly like
    generate_constraints_report_pdf() above, since ranking teachers by how
    numerous/demanding their configured constraints are only needs
    data["scheduling_constraints"], not any solved schedule. Returns the
    output path as a string.

    Raises RuntimeError (with a clear Arabic message) if no teacher has
    any scheduling constraint enabled at all, since there would be
    nothing to rank.
    """
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _ensure_fonts()

    if progress:
        progress("جاري ترتيب الأساتذة حسب تعقيد شروطهم...")
    rows = scheduler.teacher_constraint_complexity_report(data)
    if not rows:
        raise RuntimeError(
            "لا يوجد أي أستاذ لديه قيد جدولة مفعّل حالياً (لا عبر الإعداد العام ولا "
            "عبر تخصيص خاص) - لا يوجد ما يُرتَّب. فعِّل قيداً واحداً على الأقل من تبويب "
            "\"قيود الجدولة\" أولاً."
        )

    flow = _build_complexity_report_flowables(rows)
    pdf_path = out_dir / "ترتيب_الأساتذة_حسب_تعقيد_الشروط.pdf"

    if progress:
        progress("جاري توليد ملف PDF...")
    doc = _new_doc(pdf_path, title="ترتيب الأساتذة حسب تعقيد الشروط")
    doc.build(flow)
    if progress:
        progress(f"تم إنشاء: {pdf_path.name}")

    return str(pdf_path)


def generate_fulfillment_report_pdf(data, out_dir, progress=None, warm_start=None):
    """
    Build تقرير_نسبة_تحقق_رغبات_الأساتذة.pdf on its own, from its own
    independent GUI button - decoupled from "توليد البرامج" (which builds
    برنامج_الشعب/برنامج_الأساتذة/قائمة_الأساتذة together, and only produces
    this same report as a bonus fifth file when it happens to run anyway).

    Unlike generate_constraints_report_pdf()/generate_complexity_report_pdf()
    above, this CANNOT skip the solver - the fulfillment percentage per
    teacher is only known after an actual weekly schedule has been solved
    (see scheduler.solve_timetable's constraint_fulfillment return value),
    so calling this still runs the full CP-SAT solve (same time budget, up
    to 5 minutes) even though none of برنامج_الشعب/برنامج_الأساتذة/
    قائمة_الأساتذة are written to disk afterward. warm_start is passed
    straight through to solve_timetable (see its docstring) so the GUI can
    seed this solve from a previously cached schedule exactly like the
    main "توليد البرامج" button does.

    Returns (pdf_path, section_sched) - NOT just the path like the two
    sibling functions above - because the caller (the GUI) can reuse
    section_sched as the warm_start for its NEXT generation of any kind,
    and solving it here just to throw it away would waste the very
    solve this function had to perform anyway.

    Raises RuntimeError (with a clear Arabic message, same as the other two
    report functions) if no teacher has any scheduling constraint enabled
    at all - checked BEFORE running the solver, so an admin who hasn't
    configured any preference yet gets an instant, cheap explanation
    instead of waiting minutes for an empty report.
    """
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _ensure_fonts()

    # Cheap up-front check, mirroring the other two report functions: don't
    # run a multi-minute solve just to discover afterward there was never
    # anything to report.
    if not scheduler.teacher_constraints_report(data):
        raise RuntimeError(
            "لا يوجد أي أستاذ لديه قيد جدولة مفعّل حالياً (لا عبر الإعداد العام ولا "
            "عبر تخصيص خاص) - لا يوجد ما يُقاس أو يُصنَّف. فعِّل قيداً واحداً على الأقل "
            "من تبويب \"قيود الجدولة\" أولاً."
        )

    if progress:
        progress("جاري حل الجدولة لقياس نسبة تحقق رغبات الأساتذة (قد يستغرق حتى 5 دقائق)...")
    _status_name, section_sched, _constraint_notes, constraint_fulfillment = scheduler.solve_timetable(
        data, progress=progress, warm_start=warm_start)

    fulfillment_rows = scheduler.teacher_fulfillment_report(data, constraint_fulfillment)
    if not fulfillment_rows:
        raise RuntimeError(
            "لم يُعثر على أي أستاذ لديه قيد جدولة مفعّل بعد حل الجدول - لا يوجد ما يُدرَج "
            "في التقرير."
        )

    fulfillment_counts = scheduler.fulfillment_category_counts(fulfillment_rows)
    flow = _build_fulfillment_report_flowables(fulfillment_rows, fulfillment_counts)
    pdf_path = out_dir / "تقرير_نسبة_تحقق_رغبات_الأساتذة.pdf"

    if progress:
        progress("جاري توليد ملف PDF...")
    doc = _new_doc(pdf_path, title="تقرير نسبة تحقق رغبات الأساتذة")
    doc.build(flow)
    if progress:
        progress(f"تم إنشاء: {pdf_path.name}")

    return str(pdf_path), section_sched

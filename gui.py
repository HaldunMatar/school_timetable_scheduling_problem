"""
Desktop GUI for the school-staffing / timetabling program, built with
ttkbootstrap for a modern, professional look (themed widgets, light/dark
toggle, KPI gauges on a live dashboard tab, toast notifications).

Lets the user:
  - manage the single, central roster of every teacher (data["teachers"])
    from the "قائمة الأساتذة" tab - add/rename/delete a teacher ONLY
    happens there (see TeacherRosterTab); deleting one still assigned to a
    subject is blocked, and renaming cascades everywhere automatically
  - edit the number of sections (شعب) per grade
  - edit, per subject: its name, category, weekly periods per grade, and
    which roster teachers are assigned to teach it (picked from the
    central roster, never typed free-hand here - the number of names
    assigned IS the number of teachers for that subject)
  - load/save the whole dataset as JSON (defaults to data/school_data_default.json,
    but can be opened from / saved to any path)
  - see a live dashboard of KPIs and data-quality warnings before generating
  - generate the three PDF deliverables (برنامج الشعب / برنامج الأساتذة / قائمة
    الأساتذة) from the current data, via scheduler.py + pdf_gen.py

Arabic text displays right-to-left correctly in Tk widgets as long as a
font with Arabic glyphs is available on the system (see README.md); the
generated PDFs bundle their own font so they render identically everywhere
regardless of what is installed on the user's machine.
"""

import copy
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog

import ttkbootstrap as tb
from ttkbootstrap.constants import *

import scheduler
import pdf_gen

# When running from source, everything lives next to this file as before.
# When frozen into a standalone executable (PyInstaller), bundled read-only
# resources (the default JSON template, fonts) are extracted to a temporary
# directory (sys._MEIPASS) that is wiped after the program exits - so any
# file the program WRITES (generated PDFs, saved school files) must instead
# live somewhere persistent: a "برنامج توزيع الأساتذة" folder under the
# user's Documents (or home, if there is no Documents folder), created on
# first run. This split only matters for a frozen build; running the
# in-source app behaves exactly as before (everything under this folder).
if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
    _docs = os.path.join(os.path.expanduser("~"), "Documents")
    _user_root = _docs if os.path.isdir(_docs) else os.path.expanduser("~")
    USER_DIR = os.path.join(_user_root, "برنامج توزيع الأساتذة")
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    USER_DIR = BUNDLE_DIR

APP_DIR = BUNDLE_DIR  # read-only: bundled default JSON template lives here
USER_DATA_DIR = os.path.join(USER_DIR, "data")  # writable: user's own saved files
DEFAULT_JSON_PATH = os.path.join(APP_DIR, "data", "school_data_default.json")
OUTPUT_DIR = os.path.join(USER_DIR, "output")

os.makedirs(USER_DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

ARABIC_FONT = ("Amiri", 12)
ARABIC_FONT_BOLD = ("Amiri", 12, "bold")
ARABIC_FONT_SMALL = ("Amiri", 10)
ARABIC_FONT_TITLE = ("Amiri", 18, "bold")
ARABIC_FONT_SUBTITLE = ("Amiri", 11)
ARABIC_FONT_STAT = ("Amiri", 26, "bold")

LIGHT_THEME = "bootstrap-light"
DARK_THEME = "bootstrap-dark"

CATEGORY_COLORS = {
    "light": {"تربوية": ("#eaf3fc", "#123456"), "شرعية": ("#eafbea", "#0f3d1a")},
    "dark": {"تربوية": ("#1c2b3a", "#cfe6ff"), "شرعية": ("#1c3a22", "#d3f5da")},
}


def load_default_data():
    with open(DEFAULT_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def ensure_scheduling_constraints(data):
    """
    Make sure data["scheduling_constraints"] has the full {"defaults": {...
    all 4 keys ...}, "per_teacher": {...}} shape, filling in anything
    missing (e.g. a JSON file saved before this feature existed) with the
    all-disabled defaults - without touching any values already present.
    Also upgrades any legacy single-day "day_off" dict (saved before
    multi-day support existed) to the current {"days": [...], "count": N}
    shape, and any legacy single-position "empty_periods" dict (saved
    before simultaneous start+end support existed) to the current
    {"start": {...}, "end": {...}, "days_mode", "days"} shape - for both the
    global defaults and every per-teacher override.
    """
    sc = data.setdefault("scheduling_constraints", {})
    defaults = sc.setdefault("defaults", {})
    for key, base in scheduler.DEFAULT_TEACHER_CONSTRAINTS.items():
        # deepcopy, not dict(base) - "empty_periods" now nests "start"/"end"
        # sub-dicts, and a shallow copy would leave defaults["empty_periods"]
        # sharing THOSE dict objects with the shared DEFAULT_TEACHER_
        # CONSTRAINTS constant, risking cross-file/cross-teacher corruption
        # if anything ever mutated them in place instead of replacing the
        # whole group (which is what this GUI's own editors always do, but
        # better not to rely on that here).
        defaults.setdefault(key, copy.deepcopy(base))
    scheduler._normalize_day_off_group(defaults.get("day_off"))
    defaults["empty_periods"] = scheduler._normalize_empty_periods_group(defaults.get("empty_periods"))
    per_teacher = sc.setdefault("per_teacher", {})
    for overrides in per_teacher.values():
        if "day_off" in overrides:
            scheduler._normalize_day_off_group(overrides["day_off"])
        if "empty_periods" in overrides:
            overrides["empty_periods"] = scheduler._normalize_empty_periods_group(
                overrides["empty_periods"])
    return sc


CONSTRAINT_GROUP_DEFS = [
    ("empty_periods", "تفريغ حصص في بداية/نهاية اليوم"),
    ("day_off", "يوم عطلة أسبوعي"),
    ("max_gap_windows", "الحد الأقصى لعدد نوافذ الفراغ في اليوم"),
    ("start_from_beginning", "إلزام البدء من أول حصة في اليوم"),
]

DAY_OFF_MODE_LABELS = [("يوم محدد", "specific"), ("يوم عشوائي (يختاره الحل)", "random")]
EMPTY_DAYS_MODE_LABELS = [("كل الأيام", "all"), ("أيام محددة", "specific")]


# ---------------------------------------------------------------- widgets --

class SubjectEditor(tb.Frame):
    """Right-hand panel: edit one subject's periods-per-grade and teacher names."""

    def __init__(self, master, on_change):
        super().__init__(master, padding=10)
        self.on_change = on_change
        self.data = None
        self.subject = None
        self.grade_order = []
        self.grade_labels = {}
        self.sections = {}
        self.period_vars = {}

        self.name_var = tk.StringVar()
        self.category_var = tk.StringVar()

        # Everything below lives inside a scrollable area, since this panel
        # (info + periods + names + manual assignments) can end up taller
        # than the fixed window - scrolling keeps every card reachable
        # instead of the bottom ones being cut off with no way to get to
        # them (see ScrollableFrame).
        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True)
        content = self.scroll.inner

        info_card = tb.Labelframe(content, text="معلومات المادة", padding=8, bootstyle=PRIMARY)
        info_card.pack(fill="x", pady=(0, 6))
        info_card.columnconfigure(0, weight=1)

        tb.Label(info_card, text="اسم المادة:", font=ARABIC_FONT_BOLD).grid(
            row=0, column=1, sticky="e", pady=4, padx=(8, 0))
        name_entry = tb.Entry(info_card, textvariable=self.name_var, font=ARABIC_FONT,
                               justify="right", width=40)
        name_entry.grid(row=0, column=0, sticky="ew", pady=4)
        name_entry.bind("<FocusOut>", self._push_name_category)

        tb.Label(info_card, text="التصنيف:", font=ARABIC_FONT_BOLD).grid(
            row=1, column=1, sticky="e", pady=4, padx=(8, 0))
        cat_combo = tb.Combobox(info_card, textvariable=self.category_var,
                                 values=["تربوية", "شرعية"], font=ARABIC_FONT,
                                 justify="right", state="readonly", width=15,
                                 bootstyle=PRIMARY)
        cat_combo.grid(row=1, column=0, sticky="w", pady=4)
        cat_combo.bind("<<ComboboxSelected>>", self._push_name_category)

        periods_card = tb.Labelframe(content, text="الحصص الأسبوعية لكل صف", padding=8,
                                      bootstyle=INFO)
        periods_card.pack(fill="x", pady=(0, 6))
        self.periods_frame = tb.Frame(periods_card)
        self.periods_frame.pack(fill="x")

        # ---- subject-level scheduling constraints (max consecutive/day,
        # max total/day per section) - independent of who teaches this
        # subject, unlike the per-teacher constraints in "تاب قيود
        # الجدولة" (see scheduler.ensure_subject_constraints /
        # effective_subject_constraints). Both disabled by default.
        constraints_card = tb.Labelframe(
            content, text="قيود جدولة خاصة بهذه المادة", padding=8, bootstyle=SECONDARY)
        constraints_card.pack(fill="x", pady=(0, 6))

        tb.Label(constraints_card, font=ARABIC_FONT_SMALL, bootstyle="secondary",
                 justify="right", anchor="e", wraplength=560,
                 text="قيدان اختياريان (معطّلان افتراضياً) يُطبَّقان على كل شعبة تُدرَّس فيها هذه "
                      "المادة، بصرف النظر عن الأستاذ - على عكس قيود \"تاب قيود الجدولة\" التي "
                      "تخص أستاذاً بعينه.").pack(anchor="e", pady=(0, 6), fill="x")

        mc_row = tb.Frame(constraints_card)
        mc_row.pack(fill="x", pady=2)
        self.max_consec_enabled_var = tk.BooleanVar(value=False)
        tb.Checkbutton(mc_row, text="حد أقصى لعدد الحصص المتتالية لهذه المادة في نفس اليوم",
                       variable=self.max_consec_enabled_var, bootstyle="round-toggle",
                       command=self._push_subject_constraints).pack(side="right")
        tb.Label(mc_row, text="الحد:", font=ARABIC_FONT_SMALL).pack(side="right", padx=(8, 4))
        self.max_consec_var = tk.StringVar(value="2")
        max_consec_sb = tb.Spinbox(mc_row, from_=0, to=20, textvariable=self.max_consec_var,
                                    width=4, justify="center", command=self._push_subject_constraints)
        max_consec_sb.pack(side="right")
        self.max_consec_var.trace_add("write", lambda *a: self._push_subject_constraints())

        md_row = tb.Frame(constraints_card)
        md_row.pack(fill="x", pady=2)
        self.max_daily_enabled_var = tk.BooleanVar(value=False)
        tb.Checkbutton(md_row, text="حد أقصى لعدد حصص هذه المادة يومياً لكل شعبة",
                       variable=self.max_daily_enabled_var, bootstyle="round-toggle",
                       command=self._push_subject_constraints).pack(side="right")
        tb.Label(md_row, text="الحد:", font=ARABIC_FONT_SMALL).pack(side="right", padx=(8, 4))
        self.max_daily_var = tk.StringVar(value="2")
        max_daily_sb = tb.Spinbox(md_row, from_=0, to=20, textvariable=self.max_daily_var,
                                   width=4, justify="center", command=self._push_subject_constraints)
        max_daily_sb.pack(side="right")
        self.max_daily_var.trace_add("write", lambda *a: self._push_subject_constraints())

        # "أسماء الأساتذة" and "إسناد إجباري لشعب معينة" sit side by side as
        # two columns (per explicit request) instead of stacked - a grid
        # inside its own row_container, nested inside the otherwise
        # pack()-based `content` column (grid and pack can coexist as long
        # as they are never both used on the direct children of the SAME
        # container). RTL reading order: "أسماء الأساتذة" is read first, so
        # it takes the rightmost grid column (column=1); "إسناد إجباري"
        # beside it in column=0.
        names_assign_row = tb.Frame(content)
        names_assign_row.pack(fill="both", pady=(0, 6))
        names_assign_row.columnconfigure(0, weight=1)
        names_assign_row.columnconfigure(1, weight=1)

        names_card = tb.Labelframe(names_assign_row, text="أسماء الأساتذة (عدد الأسماء = عدد الأساتذة)",
                                    padding=8, bootstyle=SUCCESS)
        names_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        tb.Label(names_card, font=ARABIC_FONT_SMALL, bootstyle="secondary", justify="right",
                 anchor="e", wraplength=280,
                 text="إضافة/حذف/تعديل الأساتذة أنفسهم تتم مركزياً من تبويب \"قائمة الأساتذة\" "
                      "فقط - من هنا تختار فقط من تلك القائمة المركزية من سيُسنَد لتدريس هذه "
                      "المادة تحديداً.").pack(anchor="e", pady=(0, 6), fill="x")

        # NOTE on ordering: pack() allocates space in the order widgets are
        # packed, so the fixed-height footer rows (total_label, then the
        # delete row, then the add-from-roster row) are packed FIRST with
        # side="bottom" - each subsequent side="bottom" pack stacks ABOVE
        # the one packed before it, so packing in this order reserves, from
        # the true bottom upward: total_label, delete row, add row - before
        # the expanding listbox area is packed last and takes whatever
        # remains above them. Packing the expand=True widget first would
        # let it claim all the space, leaving nothing for the footer rows.
        self.total_label = tb.Label(names_card, text="", font=ARABIC_FONT_SMALL,
                                     bootstyle="secondary")
        self.total_label.pack(side="bottom", anchor="e", pady=(6, 0))

        del_row = tb.Frame(names_card)
        del_row.pack(side="bottom", fill="x")
        b_del = tb.Button(del_row, text="حذف الاسم من هذه المادة", command=self._remove_name,
                           bootstyle="danger-outline")
        b_del.pack(side="right", padx=2)
        tb.ToolTip(b_del, text="إزالة الاسم المحدد من قائمة أساتذة هذه المادة فقط - يبقى "
                               "الأستاذ في القائمة المركزية ويمكن إسناده لمادة أخرى")

        add_row = tb.Frame(names_card)
        add_row.pack(side="bottom", fill="x", pady=(0, 4))
        self.add_teacher_var = tk.StringVar()
        self.add_teacher_combo = tb.Combobox(add_row, textvariable=self.add_teacher_var,
                                              state="readonly", font=ARABIC_FONT, width=20,
                                              values=[])
        self.add_teacher_combo.pack(side="right", padx=(4, 0))
        self.add_teacher_combo.bind("<<ComboboxSelected>>", self._on_add_teacher_selected)
        b_add = tb.Button(add_row, text="+ إضافة لهذه المادة", command=self._add_selected_teacher,
                           bootstyle=SUCCESS)
        b_add.pack(side="right", padx=2)
        tb.ToolTip(b_add, text="إسناد الأستاذ المختار (من القائمة المركزية) لتدريس هذه المادة")
        self.add_teacher_info_var = tk.StringVar()
        tb.Label(add_row, textvariable=self.add_teacher_info_var, font=ARABIC_FONT_SMALL,
                 bootstyle="info").pack(side="right", padx=(8, 4))

        names_container = tb.Frame(names_card)
        names_container.pack(fill="both", expand=True, pady=(0, 6))

        self.names_list = tb.Listbox(names_container, font=ARABIC_FONT, justify="right",
                                      height=6, exportselection=False)
        self.names_list.pack(side="left", fill="both", expand=True)
        self.names_list.bind("<<ListboxSelect>>", self._on_names_list_select)
        scrollbar = tb.Scrollbar(names_container, orient="vertical",
                                  command=self.names_list.yview, bootstyle="success-round")
        scrollbar.pack(side="left", fill="y")
        self.names_list.config(yscrollcommand=scrollbar.set)

        # ---- mandatory manual section assignment ------------------------
        # Placed in names_assign_row (see above) rather than `content`, so
        # it renders as the column beside "أسماء الأساتذة" instead of a
        # separate stacked card below it.
        assign_card = tb.Labelframe(names_assign_row, text="إسناد إجباري لشعب معينة", padding=8,
                                     bootstyle=WARNING)
        assign_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        tb.Label(assign_card, font=ARABIC_FONT_SMALL, bootstyle="secondary", justify="right",
                 anchor="e", wraplength=280,
                 text="أسند شعباً محددة من صف معيّن لأستاذ معيّن بشكل إلزامي، بدل التوزيع "
                      "التلقائي - ويمكن تكرار هذا لنفس الأستاذ على أكثر من صف. باقي الشعب "
                      "غير المُسندة يدوياً يبقى توزيعها تلقائياً كالمعتاد.").pack(
            anchor="e", pady=(0, 6), fill="x")

        picker_row = tb.Frame(assign_card)
        picker_row.pack(fill="x", pady=(0, 6))

        tb.Label(picker_row, text="الأستاذ:", font=ARABIC_FONT_SMALL).pack(side="right", padx=(4, 4))
        self.assign_teacher_var = tk.StringVar()
        self.assign_teacher_combo = tb.Combobox(picker_row, textvariable=self.assign_teacher_var,
                                                 state="readonly", font=ARABIC_FONT, width=16,
                                                 values=[])
        self.assign_teacher_combo.pack(side="right", padx=(0, 10))

        tb.Label(picker_row, text="الصف:", font=ARABIC_FONT_SMALL).pack(side="right", padx=(4, 4))
        self.assign_track_display = tk.StringVar()
        self.assign_track_combo = tb.Combobox(picker_row, textvariable=self.assign_track_display,
                                               state="readonly", font=ARABIC_FONT, width=12,
                                               values=[])
        self.assign_track_combo.pack(side="right")
        self.assign_track_combo.bind("<<ComboboxSelected>>", self._on_assign_track_selected)

        sections_row = tb.Frame(assign_card)
        sections_row.pack(fill="x", pady=(0, 6))
        tb.Label(sections_row, text="الشعب (اختر واحدة أو أكثر):", font=ARABIC_FONT_SMALL).pack(anchor="e")
        self.assign_sections_list = tb.Listbox(sections_row, font=ARABIC_FONT, justify="center",
                                                height=3, selectmode="multiple",
                                                exportselection=False)
        self.assign_sections_list.pack(fill="x", pady=(2, 0))

        add_row = tb.Frame(assign_card)
        add_row.pack(fill="x", pady=(0, 6))
        tb.Button(add_row, text="+ إضافة إسناد", command=self._add_manual_assignment,
                  bootstyle=WARNING).pack(side="right")

        self.assign_tree = tb.Treeview(assign_card, columns=("track", "sections"),
                                        show="tree headings", height=4, bootstyle=WARNING)
        self.assign_tree.heading("#0", text="الأستاذ")
        self.assign_tree.heading("track", text="الصف")
        self.assign_tree.heading("sections", text="الشعب")
        self.assign_tree.column("#0", width=140, anchor="e")
        self.assign_tree.column("track", width=90, anchor="center")
        self.assign_tree.column("sections", width=140, anchor="center")
        self.assign_tree.pack(fill="x", pady=(0, 4))

        tb.Button(assign_card, text="حذف الإسناد المحدد", command=self._remove_manual_assignment,
                  bootstyle="danger-outline").pack(anchor="w")

    def load_subject(self, subject, grade_order, grade_labels, sections, data):
        self.subject = subject
        self.grade_order = grade_order
        self.grade_labels = grade_labels
        self.sections = sections
        self.data = data

        self.name_var.set(subject["name"])
        self.category_var.set(subject.get("category", "تربوية"))

        # Guarded by _subj_constraints_loading so the StringVar write-traces
        # below (bound to _push_subject_constraints) don't fire WHILE we are
        # merely re-displaying this subject's already-persisted values -
        # same pattern as ConstraintGroupFrame's self._loading.
        self._subj_constraints_loading = True
        sub_cfg = scheduler.ensure_subject_constraints(subject)
        mc_cfg = sub_cfg["max_consecutive_per_day"]
        md_cfg = sub_cfg["max_daily_per_section"]
        self.max_consec_enabled_var.set(bool(mc_cfg.get("enabled", False)))
        self.max_consec_var.set(str(int(mc_cfg.get("max", 2) or 0)))
        self.max_daily_enabled_var.set(bool(md_cfg.get("enabled", False)))
        self.max_daily_var.set(str(int(md_cfg.get("max", 2) or 0)))
        self._subj_constraints_loading = False

        for w in self.periods_frame.winfo_children():
            w.destroy()
        self.period_vars = {}
        self.section_count_labels = {}
        self.section_total_labels = {}
        # grand_total_label itself was just destroyed above along with every
        # other periods_frame child - clear the reference now (not just at
        # the end, once the new one is built). Otherwise the per-grade loop
        # below calls _update_grade_total() -> _update_grand_total(), which
        # would try to .config() this already-destroyed widget from the
        # PREVIOUS subject before the new one exists, raising
        # "invalid command name ..." (TclError) when switching subjects.
        self.grand_total_label = None

        # Row captions on the right (the "corner" of the grid, one column
        # past the last grade column since grades are laid out right-to-
        # left starting at the highest column index - see below).
        #
        # Tkinter grid() only accepts non-negative column indices, so the
        # grand-total column (which must sit past the *last* grade, i.e.
        # further left than column 0) can't literally be column -1/-2.
        # Instead every grade/caption column is shifted right by
        # GRAND_TOTAL_COLS, freeing columns 0 and 1 for the grand total -
        # making it the true leftmost (= last, in RTL reading order)
        # column, exactly as if it were column -1/-2.
        GRAND_TOTAL_COLS = 2
        caption_col = len(grade_order) + GRAND_TOTAL_COLS
        captions = [
            (0, "الصف"),
            (1, "حصص/شعبة"),
            (2, "عدد الشعب"),
            (3, "الإجمالي"),
        ]
        for row, text in captions:
            tb.Label(self.periods_frame, text=text, font=ARABIC_FONT_SMALL,
                     bootstyle="secondary").grid(row=row, column=caption_col, padx=(10, 3), sticky="e")

        for i, g in enumerate(grade_order):
            col = len(grade_order) - 1 - i + GRAND_TOTAL_COLS
            lbl = tb.Label(self.periods_frame, text=grade_labels.get(g, g), font=ARABIC_FONT_SMALL)
            lbl.grid(row=0, column=col, padx=3, pady=2)
            var = tk.StringVar(value=str(subject["periods"].get(g, 0)))
            ent = tb.Entry(self.periods_frame, textvariable=var, width=5, justify="center",
                            font=ARABIC_FONT, bootstyle=INFO)
            ent.grid(row=1, column=col, padx=3, pady=2)
            ent.bind("<FocusOut>", lambda e, gg=g: self._push_period(gg))
            ent.bind("<Return>", lambda e, gg=g: self._push_period(gg))
            self.period_vars[g] = var

            # Row 2: how many sections this grade currently has (read-only,
            # informational - edited from the "عدد الشعب لكل صف" bar).
            sec_lbl = tb.Label(self.periods_frame, text=str(int(sections.get(g, 0) or 0)),
                                font=ARABIC_FONT_SMALL, bootstyle="secondary")
            sec_lbl.grid(row=2, column=col, padx=3, pady=(0, 2))
            self.section_count_labels[g] = sec_lbl

            # Row 3: computed automatically = periods/section x number of
            # sections - the total weekly periods this subject needs in
            # this grade across ALL of its sections, updated live as the
            # periods entry above changes (no need to save/reselect first).
            total_lbl = tb.Label(self.periods_frame, text="0", font=ARABIC_FONT_SMALL,
                                  bootstyle="info")
            total_lbl.grid(row=3, column=col, padx=3, pady=(0, 2))
            self.section_total_labels[g] = total_lbl
            var.trace_add("write", lambda *a, gg=g: self._update_grade_total(gg))
            self._update_grade_total(g)

        # Grand total: sits in columns 0-1, i.e. past (to the left of) the
        # last grade's column - the true last position in this RTL layout -
        # the sum of every grade's row-3 total, so the overall weekly
        # periods this subject needs across the whole school is visible
        # without adding the per-grade numbers up by hand.
        tb.Separator(self.periods_frame, orient="vertical").grid(
            row=0, column=1, rowspan=4, sticky="ns", padx=(4, 10))
        tb.Label(self.periods_frame, text="المجموع الكلي", font=ARABIC_FONT_SMALL,
                 bootstyle="primary").grid(row=0, column=0, padx=3, pady=2)
        self.grand_total_label = tb.Label(self.periods_frame, text="0",
                                           font=ARABIC_FONT_BOLD, bootstyle="primary")
        self.grand_total_label.grid(row=3, column=0, padx=3, pady=(0, 2))
        self._update_grand_total()

        self.names_list.delete(0, tk.END)
        for name in subject["names"]:
            self.names_list.insert(tk.END, name)

        self._update_total()
        self._refresh_add_teacher_combo()
        self._refresh_assign_teacher_values()
        self._refresh_assign_track_values()
        self.assign_sections_list.delete(0, tk.END)
        self._refresh_assign_tree()

    def _push_subject_constraints(self):
        if self.subject is None or getattr(self, "_subj_constraints_loading", False):
            return
        try:
            max_consec = max(0, int(self.max_consec_var.get()))
        except ValueError:
            max_consec = 0
        try:
            max_daily = max(0, int(self.max_daily_var.get()))
        except ValueError:
            max_daily = 0
        cfg = self.subject.setdefault("constraints", {})
        cfg["max_consecutive_per_day"] = {
            "enabled": bool(self.max_consec_enabled_var.get()), "max": max_consec,
        }
        cfg["max_daily_per_section"] = {
            "enabled": bool(self.max_daily_enabled_var.get()), "max": max_daily,
        }
        self.on_change()

    def _push_name_category(self, event=None):
        if self.subject is None:
            return
        new_name = self.name_var.get().strip()
        if new_name:
            self.subject["name"] = new_name
        self.subject["category"] = self.category_var.get()
        self.on_change()

    def _push_period(self, grade):
        if self.subject is None:
            return
        raw = self.period_vars[grade].get().strip()
        try:
            val = float(raw) if raw else 0.0
        except ValueError:
            messagebox.showerror("خطأ", f"قيمة غير صالحة للحصص: {raw}")
            self.period_vars[grade].set(str(self.subject["periods"].get(grade, 0)))
            return
        self.subject["periods"][grade] = val
        self._update_total()
        self._refresh_assign_track_values()
        self.on_change()

    def _update_grade_total(self, grade):
        """
        Row 3 of the periods grid: (periods/section entered for this grade)
        x (how many sections this grade has) = total weekly periods this
        subject needs in this grade across all its sections. Recomputed
        live off the entry's current text as the user types - it does NOT
        wait for _push_period to commit the value, so it stays accurate
        even before the field loses focus.
        """
        lbl = self.section_total_labels.get(grade)
        if lbl is None:
            return
        raw = self.period_vars[grade].get().strip()
        try:
            periods = float(raw) if raw else 0.0
        except ValueError:
            lbl.config(text="—")
            return
        count = int(self.sections.get(grade, 0) or 0)
        total = periods * count
        lbl.config(text=str(int(total)) if total == int(total) else f"{total:g}")
        self._update_grand_total()

    def _update_grand_total(self):
        """
        Sum of every grade's row-3 total: the overall weekly periods this
        subject needs across the whole school. Guarded to no-op while
        load_subject() is still mid-construction (not every grade's entry
        exists yet) - it is called again once construction finishes.
        """
        lbl = getattr(self, "grand_total_label", None)
        if lbl is None or self.subject is None:
            return
        total = 0.0
        for g in self.grade_order:
            var = self.period_vars.get(g)
            if var is None:
                return
            raw = var.get().strip()
            try:
                periods = float(raw) if raw else 0.0
            except ValueError:
                periods = 0.0
            total += periods * int(self.sections.get(g, 0) or 0)
        lbl.config(text=str(int(total)) if total == int(total) else f"{total:g}")

    def refresh_section_counts(self, sections):
        """
        Called when the number of sections for a grade changes elsewhere
        (the "عدد الشعب لكل صف" bar) while this subject is loaded, so row 2,
        the row-3 totals, and the grand total stay correct without needing
        to reselect the subject. Does not touch the periods entries
        themselves.
        """
        self.sections = sections
        if self.subject is None:
            return
        for g, lbl in self.section_count_labels.items():
            lbl.config(text=str(int(sections.get(g, 0) or 0)))
            self._update_grade_total(g)

    def _update_total(self):
        if self.subject is None:
            self.total_label.config(text="")
            return
        n = len(self.subject["names"])
        self.total_label.config(text=f"عدد الأساتذة الحالي المشتق من الأسماء: {n}")

    def _refresh_add_teacher_combo(self):
        """
        Populate the "add to this subject" combo with roster teachers who
        aren't already assigned here (adding a name is now a pick-from-the-
        central-roster action, never free text - see the top note in
        names_card and the "قائمة الأساتذة" tab).
        """
        if self.subject is None or self.data is None:
            self.add_teacher_combo.configure(values=[])
            self.add_teacher_var.set("")
            self.add_teacher_info_var.set("")
            return
        roster = sorted(self.data.get("teachers", []))
        available = [n for n in roster if n not in self.subject["names"]]
        self.add_teacher_combo.configure(values=available)
        if self.add_teacher_var.get() not in available:
            self.add_teacher_var.set("")
            self.add_teacher_info_var.set("")

    def _on_add_teacher_selected(self, event=None):
        self._show_teacher_info(self.add_teacher_var.get())

    def _on_names_list_select(self, event=None):
        sel = self.names_list.curselection()
        if not sel:
            return
        self._show_teacher_info(self.names_list.get(sel[0]))

    def _show_teacher_info(self, name):
        """
        Live side-info: this teacher's current total weekly periods across
        every subject they're already assigned to, shown whenever a name is
        highlighted (either in the "add" combo, before committing, or in
        the already-assigned list below) - lets the user see how loaded a
        teacher already is before adding another subject onto them.
        """
        if not name or self.data is None:
            self.add_teacher_info_var.set("")
            return
        current = scheduler.teacher_current_periods(self.data, name)
        current_txt = str(int(current)) if current == int(current) else f"{current:g}"
        self.add_teacher_info_var.set(f"الحصص الحالية لـ «{name}»: {current_txt}")

    def _add_selected_teacher(self):
        if self.subject is None:
            return
        name = self.add_teacher_var.get().strip()
        if not name:
            messagebox.showinfo("تنبيه", "اختر أستاذاً من القائمة المركزية أولاً.")
            return
        if name in self.subject["names"]:
            return
        self.subject["names"].append(name)
        self.names_list.insert(tk.END, name)
        self._update_total()
        self._refresh_assign_teacher_values()
        self._refresh_add_teacher_combo()
        self.on_change()

    def _remove_name(self):
        if self.subject is None:
            return
        sel = self.names_list.curselection()
        if not sel:
            messagebox.showinfo("تنبيه", "اختر اسماً أولاً.")
            return
        idx = sel[0]
        removed_name = self.subject["names"][idx]
        if messagebox.askyesno("تأكيد", f"إزالة \"{removed_name}\" من قائمة أساتذة هذه المادة؟\n"
                                         f"(يبقى في القائمة المركزية للأساتذة)"):
            del self.subject["names"][idx]
            self.names_list.delete(idx)
            self._update_total()
            # Any manual (forced) assignments for the removed teacher no
            # longer make sense - drop them too rather than leaving a
            # dangling reference to a name that no longer exists here.
            manual = self.subject.get("manual_assignments")
            if manual:
                manual[:] = [r for r in manual if r.get("teacher") != removed_name]
            self._refresh_assign_teacher_values()
            self._refresh_add_teacher_combo()
            self._refresh_assign_tree()
            self.add_teacher_info_var.set("")
            self.on_change()

    # ------------------------------------------------ manual assignments
    def _refresh_assign_teacher_values(self):
        if self.subject is None:
            self.assign_teacher_combo.configure(values=[])
            return
        self.assign_teacher_combo.configure(values=list(self.subject["names"]))
        if self.assign_teacher_var.get() not in self.subject["names"]:
            self.assign_teacher_var.set("")

    def _refresh_assign_track_values(self):
        if self.subject is None:
            self.assign_track_combo.configure(values=[])
            return
        vals = []
        for g in self.grade_order:
            periods = float(self.subject["periods"].get(g, 0) or 0)
            if periods > 0:
                vals.append(self.grade_labels.get(g, g))
        self.assign_track_combo.configure(values=vals)
        if self.assign_track_display.get() not in vals:
            self.assign_track_display.set("")
            self.assign_sections_list.delete(0, tk.END)

    def _assign_track_code(self):
        label = self.assign_track_display.get()
        if not label:
            return None
        for g in self.grade_order:
            if self.grade_labels.get(g, g) == label:
                return g
        return None

    def _on_assign_track_selected(self, event=None):
        self._refresh_assign_sections_values()

    def _refresh_assign_sections_values(self):
        self.assign_sections_list.delete(0, tk.END)
        track = self._assign_track_code()
        if track is None:
            return
        count = int(self.sections.get(track, 0) or 0)
        for s in range(1, count + 1):
            self.assign_sections_list.insert(tk.END, str(s))

    def _add_manual_assignment(self):
        if self.subject is None:
            return
        teacher = self.assign_teacher_var.get().strip()
        track = self._assign_track_code()
        sel = self.assign_sections_list.curselection()
        if not teacher or not track or not sel:
            messagebox.showinfo("تنبيه", "اختر الأستاذ والصف وشعبة واحدة على الأقل.")
            return
        new_sections = sorted(int(self.assign_sections_list.get(i)) for i in sel)

        manual = self.subject.setdefault("manual_assignments", [])
        for row in manual:
            if row.get("track") != track or row.get("teacher") == teacher:
                continue
            overlap = sorted(set(row.get("sections", [])) & set(new_sections))
            if overlap:
                messagebox.showerror(
                    "تعارض",
                    f"الشعبة/الشعب {overlap} من صف {self.grade_labels.get(track, track)} "
                    f"مُسندة بالفعل للأستاذ {row['teacher']}.")
                return

        existing = next((r for r in manual if r.get("teacher") == teacher and r.get("track") == track),
                         None)
        if existing is not None:
            existing["sections"] = sorted(set(existing.get("sections", [])) | set(new_sections))
        else:
            manual.append({"teacher": teacher, "track": track, "sections": new_sections})

        self._refresh_assign_tree()
        self.on_change()

    def _refresh_assign_tree(self):
        self.assign_tree.delete(*self.assign_tree.get_children())
        if self.subject is None:
            return
        for i, row in enumerate(self.subject.get("manual_assignments", [])):
            track = row.get("track")
            label = self.grade_labels.get(track, track)
            sections_str = "، ".join(str(s) for s in sorted(row.get("sections", [])))
            self.assign_tree.insert("", "end", iid=str(i), text=row.get("teacher", ""),
                                     values=(label, sections_str))

    def _remove_manual_assignment(self):
        if self.subject is None:
            return
        sel = self.assign_tree.selection()
        if not sel:
            messagebox.showinfo("تنبيه", "اختر إسناداً من القائمة أولاً.")
            return
        idx = int(sel[0])
        manual = self.subject.get("manual_assignments", [])
        if messagebox.askyesno("تأكيد", "حذف هذا الإسناد؟"):
            del manual[idx]
            self._refresh_assign_tree()
            self.on_change()


class SectionsEditor(tb.Labelframe):
    """Editor for the number of sections (شعب) per grade."""

    def __init__(self, master, data, on_change):
        super().__init__(master, text="عدد الشعب لكل صف", padding=8, bootstyle=SECONDARY)
        self.data = data
        self.on_change = on_change
        self.vars = {}

        cols = data["meta"]["grade_order"]
        labels = data["meta"]["grade_labels"]
        for i, g in enumerate(cols):
            col = len(cols) - 1 - i
            tb.Label(self, text=labels.get(g, g), font=ARABIC_FONT_SMALL).grid(
                row=0, column=col, padx=6)
            var = tk.StringVar(value=str(data["sections"].get(g, 0)))
            ent = tb.Entry(self, textvariable=var, width=4, justify="center",
                            font=ARABIC_FONT, bootstyle=SECONDARY)
            ent.grid(row=1, column=col, padx=6, pady=(2, 0))
            ent.bind("<FocusOut>", lambda e, gg=g: self._push(gg))
            ent.bind("<Return>", lambda e, gg=g: self._push(gg))
            self.vars[g] = var

    def _push(self, grade):
        raw = self.vars[grade].get().strip()
        try:
            val = int(raw)
            if val < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("خطأ", f"عدد شعب غير صالح: {raw}")
            self.vars[grade].set(str(self.data["sections"].get(grade, 0)))
            return
        self.data["sections"][grade] = val
        self.on_change()


class DashboardTab(tb.Frame):
    """Read-only live overview: KPIs, capacity gauge, and data-quality warnings."""

    def __init__(self, master):
        super().__init__(master, padding=10)
        self.stat_vars = {}

        stats_row = tb.Frame(self)
        stats_row.pack(fill="x", pady=(0, 8))
        self.cards = {}
        specs = [
            ("subjects", "عدد المواد", SECONDARY),
            ("sections", "عدد الشعب الإجمالي", INFO),
            ("teachers", "عدد الأساتذة الفعليين", SUCCESS),
            ("multi", "أساتذة بأكثر من مادة", WARNING),
        ]
        for key, label, style in specs:
            card = tb.Labelframe(stats_row, text=label, padding=6, bootstyle=style)
            card.pack(side="right", fill="both", expand=True, padx=5)
            val_label = tb.Label(card, text="0", font=ARABIC_FONT_STAT, anchor="center",
                                  bootstyle=style)
            val_label.pack(fill="both", expand=True)
            self.cards[key] = val_label

        gauge_row = tb.Frame(self)
        gauge_row.pack(fill="x", pady=(0, 8))
        self.meter = tb.Meter(
            gauge_row, amount_used=0, amount_total=100, subtext="من السعة الأسبوعية الكلية",
            text_right="%", bootstyle=INFO, meter_size=130, stripe_thickness=6,
        )
        self.meter.pack(side="right", padx=10)

        legend = tb.Frame(gauge_row)
        legend.pack(side="right", fill="both", expand=True, padx=10)
        tb.Label(legend, text="نسبة استخدام السعة الأسبوعية", font=ARABIC_FONT_BOLD).pack(anchor="e")
        self.capacity_detail = tb.Label(legend, text="", font=ARABIC_FONT_SMALL, justify="right")
        self.capacity_detail.pack(anchor="e", pady=(4, 0))

        warn_card = tb.Labelframe(self, text="تنبيهات جودة البيانات", padding=6,
                                   bootstyle=DANGER)
        warn_card.pack(fill="both", expand=True)
        self.warn_text = tk.Text(warn_card, height=5, font=ARABIC_FONT, wrap="word",
                                  state="disabled", bg="#fff5f5", fg="#7a1f1f",
                                  relief="flat")
        self.warn_text.pack(fill="both", expand=True)
        self.warn_text.tag_configure("right", justify="right")

    def refresh(self, data):
        cols = data["meta"]["grade_order"]
        days = data["meta"]["days"]
        periods_per_day = data["meta"]["periods_per_day"]
        nslots = len(days) * periods_per_day

        total_subjects = len(data["subjects"])
        total_sections = sum(int(v) for v in data["sections"].values())

        warnings = []

        no_teacher_subjects = [s["name"] for s in data["subjects"] if not s["names"]]
        if no_teacher_subjects:
            warnings.append(
                "مواد بلا أساتذة معيّنين (سيولّد البرنامج أساتذة افتراضيين تلقائياً حسب "
                "النصاب عند التوليد، ما لم تُدخِل أسماء حقيقية): " + "، ".join(no_teacher_subjects))

        incomplete = scheduler.incomplete_grades(data)
        if incomplete:
            parts = []
            for grade, label, total, needed in incomplete:
                total_i = int(round(total))
                if total_i < needed:
                    parts.append(f"{label} (أُدخل {total_i} من أصل {needed}، ناقص {needed - total_i})")
                else:
                    parts.append(f"{label} (أُدخل {total_i}، أكثر من {needed} بمقدار {total_i - needed})")
            warnings.append(
                "صفوف لم تكتمل حصصها الأسبوعية بعد (ستبقى الحصص الناقصة فارغة في الجدول "
                "الناتج ما لم تُكمل إدخال حصص هذه الصفوف): " + "، ".join(parts))

        try:
            slots = scheduler.build_all_slots(data)
        except Exception as exc:
            self._set_cards(total_subjects, total_sections, 0, 0)
            self.meter.configure(amount_used=0)
            self.capacity_detail.config(text="تعذّر الحساب.")
            self._set_warnings([f"خطأ أثناء حساب البيانات: {exc}"])
            return

        per_teacher_total = {}
        for slot in slots:
            per_teacher_total[slot["name"]] = per_teacher_total.get(slot["name"], 0) + slot["total"]

        overloaded = {}
        for t, p in per_teacher_total.items():
            cap = scheduler.teacher_capacity(data, t)
            if p > cap:
                overloaded[t] = (p, cap)
        if overloaded:
            # cap here is simply the number of slots that exist in the week
            # (تفريغ حصص/يوم عطلة no longer reserve anything - see
            # scheduler.teacher_capacity) - so this only fires when a
            # teacher's total load is a literal impossibility, not because
            # of any of their optional scheduling preferences.
            details = "، ".join(
                f"{t} ({int(p)} حصة > {cap} حصة في الأسبوع)"
                for t, (p, cap) in overloaded.items()
            )
            warnings.append("أساتذة يحتاجون حصصاً أكثر من عدد حصص الأسبوع بالكامل: " + details)

        try:
            scheduler.validate_teacher_constraints(data)
        except RuntimeError as exc:
            warnings.append(str(exc))

        try:
            scheduler.validate_manual_assignments(data)
        except RuntimeError as exc:
            warnings.append(str(exc))

        try:
            rows, multi, unique_count = scheduler.teacher_roster(data)
        except Exception as exc:
            rows, multi, unique_count = [], {}, len(per_teacher_total)
            warnings.append(f"تعذّر بناء قائمة الأساتذة: {exc}")

        total_periods_needed = sum(slot["total"] for slot in slots)
        total_capacity = nslots * total_sections if total_sections else 0
        pct = (total_periods_needed / total_capacity * 100) if total_capacity else 0

        self._set_cards(total_subjects, total_sections, unique_count, len(multi))
        self.meter.configure(amount_used=round(pct))
        self.capacity_detail.config(
            text=f"{int(total_periods_needed)} حصة مطلوبة من أصل {total_capacity} حصة متاحة "
                 f"({total_sections} شعبة × {nslots} حصة/أسبوع)")

        if not warnings:
            warnings.append("لا توجد تنبيهات — البيانات متّسقة وجاهزة للتوليد.")
        self._set_warnings(warnings)

    def _set_cards(self, subjects, sections, teachers, multi):
        self.cards["subjects"].config(text=str(subjects))
        self.cards["sections"].config(text=str(sections))
        self.cards["teachers"].config(text=str(teachers))
        self.cards["multi"].config(text=str(multi))

    def _set_warnings(self, lines):
        self.warn_text.config(state="normal")
        self.warn_text.delete("1.0", tk.END)
        for line in lines:
            self.warn_text.insert(tk.END, "• " + line + "\n\n", "right")
        self.warn_text.config(state="disabled")


class TeacherRosterTab(tb.Frame):
    """
    The single, centrally-managed master list of every teacher the program
    knows about. Adding, deleting, and renaming a teacher happen ONLY here
    (never by typing a brand-new name inside a subject editor - see
    SubjectEditor's names_card, which only ever picks an existing name from
    this roster): this way every part of the file always agrees on one
    canonical spelling per real person.

    - Deleting a teacher still assigned to any subject is blocked with a
      clear message naming which subjects, rather than silently orphaning
      them (see scheduler.remove_teacher_from_roster).
    - Renaming cascades everywhere at once: every subject's names list and
      manual_assignments rows, plus any per-teacher scheduling-constraint
      override (see scheduler.rename_teacher_in_roster).
    - "الحصص الحالية" is this teacher's live total weekly periods across
      every subject they're already assigned to (scheduler.
      teacher_current_periods) - the same figure shown as side-info when
      picking a teacher inside a subject editor.
    """

    def __init__(self, master, on_change):
        super().__init__(master, padding=10)
        self.data = None
        self.on_change = on_change

        tb.Label(self, font=ARABIC_FONT_SMALL, bootstyle="secondary", justify="right",
                 anchor="e", wraplength=1100,
                 text="القائمة المركزية لكل الأساتذة الذين يعرفهم البرنامج - الإضافة والحذف "
                      "وتعديل الاسم تتم من هنا فقط. عند إسناد أستاذ لتدريس مادة (من محرر "
                      "المادة في تبويب \"البيانات\") تختار من هذه القائمة، ولا يمكن كتابة اسم "
                      "أستاذ جديد مباشرة من هناك. \"الحصص الحالية\" هي إجمالي حصص الأستاذ "
                      "الأسبوعية من كل المواد المُسنَد إليها حتى هذه اللحظة.").pack(
            anchor="e", pady=(0, 8), fill="x")

        btns = tb.Frame(self)
        btns.pack(fill="x", pady=(0, 6))
        tb.Button(btns, text="+ إضافة أستاذ جديد", command=self._add_teacher,
                  bootstyle=SUCCESS).pack(side="right", padx=2)
        tb.Button(btns, text="تعديل الاسم", command=self._rename_teacher,
                  bootstyle="info-outline").pack(side="right", padx=2)
        tb.Button(btns, text="حذف الأستاذ", command=self._delete_teacher,
                  bootstyle="danger-outline").pack(side="right", padx=2)
        tb.Button(btns, text="⟲ تحديث", command=self.refresh,
                  bootstyle="secondary-outline").pack(side="left", padx=2)

        columns = ("periods", "subjects")
        self.tree = tb.Treeview(self, columns=columns, show="tree headings", height=18,
                                 bootstyle=SUCCESS)
        self.tree.heading("#0", text="اسم الأستاذ")
        self.tree.heading("periods", text="الحصص الحالية/الأسبوع")
        self.tree.heading("subjects", text="المواد المُسنَد إليها")
        self.tree.column("#0", width=220, anchor="e")
        self.tree.column("periods", width=150, anchor="center")
        self.tree.column("subjects", width=420, anchor="e")
        self.tree.pack(fill="both", expand=True)

    def set_data(self, data):
        self.data = data
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        if self.data is None:
            return
        scheduler.ensure_teacher_roster(self.data)
        subjects_by_teacher = {}
        for subject in self.data["subjects"]:
            for n in subject["names"]:
                subjects_by_teacher.setdefault(n, []).append(subject["name"])
        for name in sorted(self.data.get("teachers", [])):
            periods = scheduler.teacher_current_periods(self.data, name)
            periods_txt = str(int(periods)) if periods == int(periods) else f"{periods:g}"
            subj_txt = "، ".join(subjects_by_teacher.get(name, [])) or "— (غير مُسنَد بعد)"
            self.tree.insert("", "end", iid=name, text=name, values=(periods_txt, subj_txt))

    def _selected_name(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("تنبيه", "اختر أستاذاً من القائمة أولاً.")
            return None
        return sel[0]

    def _add_teacher(self):
        if self.data is None:
            return
        name = simpledialog.askstring("أستاذ جديد", "اسم الأستاذ الجديد:", parent=self)
        if not name:
            return
        try:
            scheduler.add_teacher_to_roster(self.data, name)
        except ValueError as exc:
            messagebox.showerror("خطأ", str(exc))
            return
        self.refresh()
        self.on_change()

    def _rename_teacher(self):
        if self.data is None:
            return
        name = self._selected_name()
        if not name:
            return
        new_name = simpledialog.askstring("تعديل الاسم", "الاسم الجديد:",
                                           initialvalue=name, parent=self)
        if not new_name:
            return
        try:
            scheduler.rename_teacher_in_roster(self.data, name, new_name)
        except ValueError as exc:
            messagebox.showerror("خطأ", str(exc))
            return
        self.refresh()
        self.on_change()

    def _delete_teacher(self):
        if self.data is None:
            return
        name = self._selected_name()
        if not name:
            return
        if not messagebox.askyesno("تأكيد", f"حذف الأستاذ \"{name}\" من القائمة المركزية نهائياً؟"):
            return
        try:
            scheduler.remove_teacher_from_roster(self.data, name)
        except RuntimeError as exc:
            messagebox.showerror("لا يمكن الحذف", str(exc))
            return
        self.refresh()
        self.on_change()


class ScrollableFrame(tb.Frame):
    """
    A vertically-scrollable container: put widgets inside `.inner` (a Frame)
    instead of the ScrollableFrame itself. Used for panels (like the
    scheduling-constraints tab) that can end up taller than the fixed
    window - a mouse-wheel-friendly scrollbar appears instead of the
    content being cut off with no way to reach it.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.vbar = tb.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                  bootstyle="round")
        self.inner = tb.Frame(self.canvas)

        self._window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda e: self._bind_mousewheel())
        self.canvas.bind("<Leave>", lambda e: self._unbind_mousewheel())

    def _on_inner_configure(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        # Keep the inner frame exactly as wide as the visible canvas, so its
        # children (labelframes etc.) can fill horizontally like normal.
        self.canvas.itemconfig(self._window_id, width=event.width)

    def _bind_mousewheel(self):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_mousewheel(self):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        if event.num == 4:
            self.canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(3, "units")
        else:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)) * 3, "units")


class ConstraintGroupFrame(tb.Labelframe):
    """
    One editable card for a single scheduling-constraint group (empty
    periods / day off / max gap windows / start from beginning).

    Used in two modes:
      - overridable=False - the global-defaults editor: always active,
        writes straight into scheduling_constraints.defaults[key].
      - overridable=True - a per-teacher override editor: an extra
        "تخصيص لهذا الأستاذ" toggle controls whether an override entry
        exists for this teacher at all. While off, the fields just display
        the current EFFECTIVE (inherited) values, disabled; switching it on
        starts persisting an override seeded from those displayed values.
    """

    def __init__(self, master, title, key, get_group, set_group, overridable, on_change):
        super().__init__(master, text=title, padding=8, bootstyle=SECONDARY)
        self.key = key
        self.days = []
        self.periods_per_day = 7
        self.get_group = get_group
        self.set_group = set_group
        self.overridable = overridable
        self.on_change = on_change
        self._loading = True
        self._interactive = []

        top_row = tb.Frame(self)
        top_row.pack(fill="x")

        self.override_var = tk.BooleanVar(value=False)
        if overridable:
            ov_cb = tb.Checkbutton(top_row, text="تخصيص لهذا الأستاذ", variable=self.override_var,
                                    bootstyle="round-toggle", command=self._on_override_toggle)
            ov_cb.pack(side="right", padx=(0, 14))

        self.enabled_var = tk.BooleanVar(value=False)
        self.enabled_cb = tb.Checkbutton(top_row, text="مفعّل", variable=self.enabled_var,
                                          bootstyle="round-toggle", command=self._push)
        self.enabled_cb.pack(side="right")
        self._interactive.append(self.enabled_cb)

        self.fields = tb.Frame(self)
        self.fields.pack(fill="x", pady=(8, 0))

        if key == "empty_periods":
            # "بداية اليوم" و"نهاية اليوم" مستقلان تماماً - كل منهما يُفعَّل
            # وله عدده الخاص بصورة منفصلة، وقد يكونا مفعَّلين معاً في آنٍ
            # واحد (تفريغ أول حصة وآخر حصتين من نفس اليوم مثلاً).
            start_row = tb.Frame(self.fields)
            start_row.pack(fill="x", pady=2)
            self.start_enabled_var = tk.BooleanVar(value=False)
            start_cb = tb.Checkbutton(start_row, text="تفريغ بداية اليوم",
                                       variable=self.start_enabled_var,
                                       bootstyle="round-toggle", command=self._push)
            start_cb.pack(side="right")
            self._interactive.append(start_cb)
            tb.Label(start_row, text="عدد الحصص:", font=ARABIC_FONT_SMALL).pack(
                side="right", padx=(4, 4))
            self.start_count_var = tk.StringVar(value="1")
            start_count_sb = tb.Spinbox(start_row, from_=0, to=20, textvariable=self.start_count_var,
                                         width=4, justify="center", command=self._push)
            start_count_sb.pack(side="right", padx=(0, 16))
            self.start_count_var.trace_add("write", lambda *a: self._push())
            self._interactive.append(start_count_sb)

            end_row = tb.Frame(self.fields)
            end_row.pack(fill="x", pady=2)
            self.end_enabled_var = tk.BooleanVar(value=False)
            end_cb = tb.Checkbutton(end_row, text="تفريغ نهاية اليوم",
                                     variable=self.end_enabled_var,
                                     bootstyle="round-toggle", command=self._push)
            end_cb.pack(side="right")
            self._interactive.append(end_cb)
            tb.Label(end_row, text="عدد الحصص:", font=ARABIC_FONT_SMALL).pack(
                side="right", padx=(4, 4))
            self.end_count_var = tk.StringVar(value="1")
            end_count_sb = tb.Spinbox(end_row, from_=0, to=20, textvariable=self.end_count_var,
                                       width=4, justify="center", command=self._push)
            end_count_sb.pack(side="right", padx=(0, 16))
            self.end_count_var.trace_add("write", lambda *a: self._push())
            self._interactive.append(end_count_sb)

            # نطاق الأيام: كل الأيام (الافتراضي، وسلوك كل الملفات القديمة)،
            # أو أيام محددة يختارها المستخدم فقط.
            scope_row = tb.Frame(self.fields)
            scope_row.pack(fill="x", pady=(6, 2))
            tb.Label(scope_row, text="نطاق الأيام:", font=ARABIC_FONT_SMALL).pack(side="right")
            self.ep_days_mode_display = tk.StringVar(value=EMPTY_DAYS_MODE_LABELS[0][0])
            ep_mode_combo = tb.Combobox(scope_row, textvariable=self.ep_days_mode_display, width=14,
                                         state="readonly",
                                         values=[lbl for lbl, _ in EMPTY_DAYS_MODE_LABELS])
            ep_mode_combo.pack(side="right", padx=4)
            ep_mode_combo.bind(
                "<<ComboboxSelected>>", lambda e: (self._sync_empty_periods_state(), self._push()))
            self._interactive.append(ep_mode_combo)

            ep_specific_box = tb.Frame(self.fields)
            ep_specific_box.pack(fill="x", pady=(2, 0))
            tb.Label(ep_specific_box, text="الأيام (اختر واحداً أو أكثر):", font=ARABIC_FONT_SMALL).pack(
                anchor="e")
            self.ep_days_list = tb.Listbox(ep_specific_box, font=ARABIC_FONT_SMALL, justify="center",
                                            selectmode="multiple", exportselection=False,
                                            height=5, width=11)
            self.ep_days_list.pack(anchor="e")
            self.ep_days_list.bind("<<ListboxSelect>>", lambda e: self._push())
            self._interactive.append(self.ep_days_list)

        elif key == "day_off":
            tb.Label(self.fields, text="النمط:", font=ARABIC_FONT_SMALL).pack(side="right")
            self.mode_display = tk.StringVar(value=DAY_OFF_MODE_LABELS[0][0])
            mode_combo = tb.Combobox(self.fields, textvariable=self.mode_display, width=20,
                                      state="readonly", values=[lbl for lbl, _ in DAY_OFF_MODE_LABELS])
            mode_combo.pack(side="right", padx=4)
            mode_combo.bind("<<ComboboxSelected>>", lambda e: (self._sync_day_off_state(), self._push()))
            self._interactive.append(mode_combo)

            # "specific" mode: pick one or more exact days off directly via
            # a multi-select list - the NUMBER of days selected IS the
            # count, no separate field needed for this mode.
            specific_box = tb.Frame(self.fields)
            specific_box.pack(side="right", padx=(4, 12))
            tb.Label(specific_box, text="الأيام (اختر واحداً أو أكثر):", font=ARABIC_FONT_SMALL).pack(anchor="e")
            self.days_list = tb.Listbox(specific_box, font=ARABIC_FONT_SMALL, justify="center",
                                         selectmode="multiple", exportselection=False,
                                         height=5, width=11)
            self.days_list.pack()
            self.days_list.bind("<<ListboxSelect>>", lambda e: self._push())
            self._interactive.append(self.days_list)

            # "random" mode: how many days the solver should pick
            # automatically each week (no specific days to choose here).
            random_box = tb.Frame(self.fields)
            random_box.pack(side="right", padx=(4, 4))
            tb.Label(random_box, text="عدد أيام العطلة:", font=ARABIC_FONT_SMALL).pack(anchor="e")
            self.day_off_count_var = tk.StringVar(value="1")
            self.day_off_count_sb = tb.Spinbox(random_box, from_=1, to=7, textvariable=self.day_off_count_var,
                                                width=4, justify="center", command=self._push)
            self.day_off_count_sb.pack()
            self.day_off_count_var.trace_add("write", lambda *a: self._push())
            self._interactive.append(self.day_off_count_sb)

        elif key == "max_gap_windows":
            tb.Label(self.fields, text="الحد الأقصى لعدد النوافذ في اليوم:", font=ARABIC_FONT_SMALL).pack(side="right")
            self.max_var = tk.StringVar(value="1")
            max_sb = tb.Spinbox(self.fields, from_=0, to=20, textvariable=self.max_var,
                                 width=4, justify="center", command=self._push)
            max_sb.pack(side="right", padx=4)
            self.max_var.trace_add("write", lambda *a: self._push())
            self._interactive.append(max_sb)

        # start_from_beginning has no extra fields beyond the "مفعّل" toggle.

        if overridable:
            self._set_state("disabled")
        self._loading = False

    # ------------------------------------------------------------- config
    def set_days(self, days, periods_per_day):
        self.days = list(days)
        self.periods_per_day = periods_per_day
        for listbox_name in ("days_list", "ep_days_list"):
            listbox = getattr(self, listbox_name, None)
            if listbox is None:
                continue
            # Tk's Listbox silently ignores insert/delete/selection changes
            # while its state is "disabled" - and for a per-teacher override
            # card (overridable=True), this method can run BEFORE the user
            # has ever turned "تخصيص لهذا الأستاذ" on, i.e. while the list
            # is still sitting disabled from __init__'s initial
            # self._set_state("disabled"). Force it briefly to "normal" so
            # the list actually gets (re)populated, then restore whatever
            # state it had - exactly the same pattern as
            # _apply_group_to_widgets below, and for the same reason.
            prev_state = str(listbox.cget("state"))
            listbox.configure(state="normal")
            selected_before = {listbox.get(i) for i in listbox.curselection()}
            listbox.delete(0, tk.END)
            for d in self.days:
                listbox.insert(tk.END, d)
            for i, d in enumerate(self.days):
                if d in selected_before:
                    listbox.selection_set(i)
            listbox.configure(state=prev_state)
        if hasattr(self, "day_off_count_sb"):
            self.day_off_count_sb.configure(to=max(1, len(self.days) or 7))

    # --------------------------------------------------------- label maps
    @staticmethod
    def _label_to_code(mapping, label):
        for lbl, code in mapping:
            if lbl == label:
                return code
        return mapping[0][1]

    @staticmethod
    def _code_to_label(mapping, code):
        for lbl, c in mapping:
            if c == code:
                return lbl
        return mapping[0][0]

    # ------------------------------------------------------------- state
    def _set_state(self, state):
        for w in self._interactive:
            try:
                w.configure(state=state)
            except tk.TclError:
                pass
        if self.key == "day_off" and state == "normal":
            self._sync_day_off_state()
        if self.key == "empty_periods" and state == "normal":
            self._sync_empty_periods_state()

    def _sync_day_off_state(self):
        """
        Only the widget matching the currently-selected mode is actually
        editable - the days list for "specific", the count spinbox for
        "random" - the other one stays disabled (but still shows whatever
        value it holds, exactly like the rest of this frame's fields).
        """
        mode_code = self._label_to_code(DAY_OFF_MODE_LABELS, self.mode_display.get())
        specific = mode_code == "specific"
        try:
            self.days_list.configure(state="normal" if specific else "disabled")
        except tk.TclError:
            pass
        try:
            self.day_off_count_sb.configure(state="normal" if not specific else "disabled")
        except tk.TclError:
            pass

    def _sync_empty_periods_state(self):
        """
        The days-list is only actually editable in "أيام محددة" (specific)
        mode - in "كل الأيام" (all) mode it stays disabled (but still shows
        whatever selection it holds), same pattern as _sync_day_off_state.
        """
        mode_code = self._label_to_code(EMPTY_DAYS_MODE_LABELS, self.ep_days_mode_display.get())
        specific = mode_code == "specific"
        try:
            self.ep_days_list.configure(state="normal" if specific else "disabled")
        except tk.TclError:
            pass

    # --------------------------------------------------------- load/push
    def load(self, group, has_override=False):
        """Display `group` (an effective, fully-resolved dict for this key)."""
        self._loading = True
        if self.overridable:
            self.override_var.set(has_override)
        self._apply_group_to_widgets(group)
        state = "normal" if (not self.overridable or has_override) else "disabled"
        self._set_state(state)
        self._loading = False

    def _apply_group_to_widgets(self, group):
        self.enabled_var.set(bool(group.get("enabled", False)))
        if self.key == "empty_periods":
            start = group.get("start") or {}
            end = group.get("end") or {}
            self.start_enabled_var.set(bool(start.get("enabled", False)))
            self.start_count_var.set(str(int(start.get("count", 1) or 0)))
            self.end_enabled_var.set(bool(end.get("enabled", False)))
            self.end_count_var.set(str(int(end.get("count", 1) or 0)))
            self.ep_days_mode_display.set(
                self._code_to_label(EMPTY_DAYS_MODE_LABELS, group.get("days_mode", "all")))
            chosen_days = set(group.get("days") or [])
            # Same "force to normal, then restore" dance as day_off's days
            # list just below - see that comment for why.
            prev_state = str(self.ep_days_list.cget("state"))
            self.ep_days_list.configure(state="normal")
            self.ep_days_list.selection_clear(0, tk.END)
            for i, d in enumerate(self.days):
                if d in chosen_days:
                    self.ep_days_list.selection_set(i)
            self.ep_days_list.configure(state=prev_state)
        elif self.key == "day_off":
            self.mode_display.set(self._code_to_label(DAY_OFF_MODE_LABELS, group.get("mode", "specific")))
            chosen_days = set(group.get("days") or [])
            # Tk's Listbox silently ignores selection_clear/selection_set
            # while its state is "disabled" (e.g. left disabled from the
            # OTHER mode, or from this card's "تخصيص لهذا الأستاذ" toggle
            # being off) - force it briefly to "normal" so the selection
            # actually takes, then let _set_state() (called right after
            # this, from load()) put it back in whatever state is correct.
            prev_state = str(self.days_list.cget("state"))
            self.days_list.configure(state="normal")
            self.days_list.selection_clear(0, tk.END)
            for i, d in enumerate(self.days):
                if d in chosen_days:
                    self.days_list.selection_set(i)
            self.days_list.configure(state=prev_state)
            try:
                count = max(1, int(group.get("count", 1) or 1))
            except (TypeError, ValueError):
                count = 1
            self.day_off_count_var.set(str(count))
        elif self.key == "max_gap_windows":
            self.max_var.set(str(int(group.get("max", 1) or 0)))

    def _collect_group(self):
        g = {"enabled": bool(self.enabled_var.get())}
        if self.key == "empty_periods":
            try:
                start_count = max(0, int(self.start_count_var.get()))
            except ValueError:
                start_count = 0
            try:
                end_count = max(0, int(self.end_count_var.get()))
            except ValueError:
                end_count = 0
            g["start"] = {"enabled": bool(self.start_enabled_var.get()), "count": start_count}
            g["end"] = {"enabled": bool(self.end_enabled_var.get()), "count": end_count}
            g["days_mode"] = self._label_to_code(EMPTY_DAYS_MODE_LABELS, self.ep_days_mode_display.get())
            sel = self.ep_days_list.curselection()
            g["days"] = [self.days[i] for i in sel if i < len(self.days)]
        elif self.key == "day_off":
            g["mode"] = self._label_to_code(DAY_OFF_MODE_LABELS, self.mode_display.get())
            sel = self.days_list.curselection()
            g["days"] = [self.days[i] for i in sel if i < len(self.days)]
            try:
                g["count"] = max(1, int(self.day_off_count_var.get()))
            except ValueError:
                g["count"] = 1
        elif self.key == "max_gap_windows":
            try:
                g["max"] = max(0, int(self.max_var.get()))
            except ValueError:
                g["max"] = 0
        return g

    def _push(self):
        if self._loading:
            return
        self.set_group(self.key, self._collect_group())
        self.on_change()

    def _on_override_toggle(self):
        if self._loading:
            return
        if self.override_var.get():
            # Turning ON: keep the currently-displayed (inherited) values as
            # the starting point of the override, then enable editing.
            self._set_state("normal")
            self._push()
        else:
            # Turning OFF: drop the override, then re-display whatever the
            # plain effective (default-only) value now is. Guard with
            # _loading so redisplaying (e.g. count_var.set(...), which has a
            # write-trace bound to _push) doesn't immediately re-create the
            # override we just removed.
            self.set_group(self.key, None)
            self._loading = True
            self._apply_group_to_widgets(self.get_group(self.key))
            self._loading = False
            self._set_state("disabled")
            self.on_change()


class ConstraintsTab(tb.Frame):
    """
    Tab for the four scheduling-constraint options: a global-defaults panel
    (applies to every teacher with no override) plus a per-teacher override
    panel (pick a teacher, then customize just the options that need it).
    """

    def __init__(self, master, on_change):
        super().__init__(master, padding=10)
        self.data = None
        self.on_change = on_change
        self.current_teacher = None

        intro = tb.Label(
            self, font=ARABIC_FONT_SMALL, bootstyle="secondary", justify="right", anchor="e",
            text="اضبط قيوداً على توزيع حصص الأستاذ (تفريغ فترات، يوم عطلة، حد لعدد الفراغات، "
                 "إلزام البدء من أول اليوم) — إما كإعداد عام لكل الأساتذة، أو مخصص لأستاذ واحد بعينه.",
            wraplength=1100,
        )
        intro.pack(anchor="e", pady=(0, 10), fill="x")

        scroll_area = ScrollableFrame(self)
        scroll_area.pack(fill="both", expand=True)

        outer = tb.Panedwindow(scroll_area.inner, orient="horizontal")
        outer.pack(fill="both", expand=True)

        # ---- global defaults --------------------------------------------
        defaults_pane = tb.Frame(outer, padding=(0, 0, 8, 0))
        outer.add(defaults_pane, weight=1)
        tb.Label(defaults_pane, text="الإعدادات الافتراضية العامة", font=ARABIC_FONT_BOLD).pack(
            anchor="e", pady=(0, 8))

        self.default_frames = {}
        for key, title in CONSTRAINT_GROUP_DEFS:
            f = ConstraintGroupFrame(
                defaults_pane, title, key,
                get_group=self._get_default_group, set_group=self._set_default_group,
                overridable=False, on_change=self._on_any_change,
            )
            f.pack(fill="x", pady=4)
            self.default_frames[key] = f

        # ---- per-teacher override ----------------------------------------
        teacher_pane = tb.Frame(outer, padding=(8, 0, 0, 0))
        outer.add(teacher_pane, weight=1)
        tb.Label(teacher_pane, text="تخصيص لأستاذ معيّن", font=ARABIC_FONT_BOLD).pack(anchor="e", pady=(0, 8))

        picker_row = tb.Frame(teacher_pane)
        picker_row.pack(fill="x", pady=(0, 8))
        tb.Label(picker_row, text="الأستاذ:", font=ARABIC_FONT_SMALL).pack(side="right", padx=(4, 4))
        self.teacher_var = tk.StringVar()
        self.teacher_combo = tb.Combobox(picker_row, textvariable=self.teacher_var, state="readonly",
                                          font=ARABIC_FONT, values=[])
        self.teacher_combo.pack(side="right", fill="x", expand=True)
        self.teacher_combo.bind("<<ComboboxSelected>>", self._on_teacher_selected)

        # Live side-info for whichever teacher is currently picked above:
        # their current total weekly نصاب (exactly what solve_timetable
        # would try to fit into the week), then a second line listing every
        # subject/grade/section combination that makes up that total - so
        # the person setting a constraint (e.g. "يوم عطلة") can see at a
        # glance whether this teacher's real load can actually absorb it,
        # without leaving this tab to go check the "البيانات" tab.
        self.teacher_load_var = tk.StringVar()
        tb.Label(teacher_pane, textvariable=self.teacher_load_var, font=ARABIC_FONT_BOLD,
                 bootstyle="info", justify="right", anchor="e", wraplength=480).pack(
            anchor="e", fill="x", pady=(0, 2))
        self.teacher_subjects_var = tk.StringVar()
        tb.Label(teacher_pane, textvariable=self.teacher_subjects_var, font=ARABIC_FONT_SMALL,
                 bootstyle="secondary", justify="right", anchor="e", wraplength=480).pack(
            anchor="e", fill="x", pady=(0, 8))

        self.teacher_frames = {}
        for key, title in CONSTRAINT_GROUP_DEFS:
            f = ConstraintGroupFrame(
                teacher_pane, title, key,
                get_group=self._get_teacher_group, set_group=self._set_teacher_group,
                overridable=True, on_change=self._on_any_change,
            )
            f.pack(fill="x", pady=4)
            self.teacher_frames[key] = f

    # ------------------------------------------------------------- wiring
    def set_data(self, data):
        self.data = data
        ensure_scheduling_constraints(self.data)
        days = self.data["meta"]["days"]
        ppd = self.data["meta"]["periods_per_day"]
        for f in list(self.default_frames.values()) + list(self.teacher_frames.values()):
            f.set_days(days, ppd)

        for key, _ in CONSTRAINT_GROUP_DEFS:
            self.default_frames[key].load(self._get_default_group(key))

        names = scheduler.get_all_teacher_names(self.data)
        self.teacher_combo.configure(values=names)
        if names:
            if self.current_teacher not in names:
                self.current_teacher = names[0]
            self.teacher_var.set(self.current_teacher)
            self._refresh_teacher_frames()
        else:
            self.current_teacher = None
            self.teacher_var.set("")
            for key, _ in CONSTRAINT_GROUP_DEFS:
                self.teacher_frames[key].load(dict(scheduler.DEFAULT_TEACHER_CONSTRAINTS[key]))
                self.teacher_frames[key]._set_state("disabled")
            self.teacher_load_var.set("")
            self.teacher_subjects_var.set("")

    def _on_teacher_selected(self, event=None):
        self.current_teacher = self.teacher_var.get()
        self._refresh_teacher_frames()

    def _refresh_teacher_frames(self):
        if not self.current_teacher or self.data is None:
            return
        eff = scheduler.effective_constraints(self.data, self.current_teacher)
        overrides = self.data["scheduling_constraints"]["per_teacher"].get(self.current_teacher, {})
        for key, _ in CONSTRAINT_GROUP_DEFS:
            self.teacher_frames[key].load(eff[key], has_override=key in overrides)
        self._refresh_teacher_info()

    def _refresh_teacher_info(self):
        """
        Fill in the نصاب line and the subjects/grades/sections line for
        self.current_teacher - see the comment above teacher_load_var in
        __init__ for why this lives here.
        """
        name = self.current_teacher
        current = scheduler.teacher_current_periods(self.data, name)
        current_txt = str(int(current)) if current == int(current) else f"{current:g}"
        self.teacher_load_var.set(f"النصاب الحصصي الحالي لـ «{name}»: {current_txt} حصة أسبوعياً")

        rows, _multi, _n = scheduler.teacher_roster(self.data)
        own_rows = [r for r in rows if r["teacher"] == name]
        if own_rows:
            parts = [f"{r['subject']} ({r['sections']})" for r in own_rows]
            self.teacher_subjects_var.set("يُدرِّس: " + " ؛ ".join(parts))
        else:
            self.teacher_subjects_var.set(f"«{name}» لا يُدرِّس أي مادة حالياً.")

    # ---- global defaults get/set --------------------------------------
    def _get_default_group(self, key):
        return self.data["scheduling_constraints"]["defaults"][key]

    def _set_default_group(self, key, group):
        self.data["scheduling_constraints"]["defaults"][key] = group

    # ---- per-teacher get/set -------------------------------------------
    def _get_teacher_group(self, key):
        if not self.current_teacher:
            return dict(scheduler.DEFAULT_TEACHER_CONSTRAINTS[key])
        return scheduler.effective_constraints(self.data, self.current_teacher)[key]

    def _set_teacher_group(self, key, group):
        if not self.current_teacher:
            return
        per_teacher = self.data["scheduling_constraints"]["per_teacher"]
        if group is None:
            if self.current_teacher in per_teacher:
                per_teacher[self.current_teacher].pop(key, None)
                if not per_teacher[self.current_teacher]:
                    del per_teacher[self.current_teacher]
        else:
            per_teacher.setdefault(self.current_teacher, {})[key] = group

    def _on_any_change(self):
        self.on_change()


class NewSchoolDialog(tb.Toplevel):
    """
    Modal "مدرسة جديدة" wizard: collects the handful of school-wide
    settings a brand-new (empty) dataset needs before the user starts
    entering subjects - which days are school days, how many periods are
    in a day, and the نصاب (default weekly load per teacher) used later to
    size auto-generated placeholder teachers for any subject left without
    real names.
    """

    def __init__(self, master, on_confirm):
        super().__init__(master=master, title="مدرسة جديدة")
        self.on_confirm = on_confirm
        self.resizable(False, False)

        body = tb.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        tb.Label(body, text="إعدادات المدرسة الجديدة", font=ARABIC_FONT_BOLD).pack(anchor="e", pady=(0, 10))

        days_card = tb.Labelframe(body, text="أيام الدراسة", padding=8, bootstyle=PRIMARY)
        days_card.pack(fill="x", pady=(0, 8))
        self.day_vars = {}
        days_row = tb.Frame(days_card)
        days_row.pack(fill="x")
        for day in scheduler.ALL_WEEKDAYS:
            var = tk.BooleanVar(value=day in scheduler.DEFAULT_DAYS)
            cb = tb.Checkbutton(days_row, text=day, variable=var, bootstyle="round-toggle")
            cb.pack(side="right", padx=6, pady=2)
            self.day_vars[day] = var

        settings_card = tb.Frame(body)
        settings_card.pack(fill="x", pady=(0, 8))

        tb.Label(settings_card, text="عدد الحصص اليومية:", font=ARABIC_FONT_SMALL).grid(
            row=0, column=1, sticky="e", padx=(8, 0), pady=4)
        self.periods_var = tk.StringVar(value="7")
        tb.Entry(settings_card, textvariable=self.periods_var, width=6, justify="center",
                 font=ARABIC_FONT).grid(row=0, column=0, sticky="w", pady=4)

        tb.Label(settings_card, text="نصاب الأستاذ الافتراضي (حصة/أسبوع):", font=ARABIC_FONT_SMALL).grid(
            row=1, column=1, sticky="e", padx=(8, 0), pady=4)
        self.nisab_var = tk.StringVar(value="24")
        tb.Entry(settings_card, textvariable=self.nisab_var, width=6, justify="center",
                 font=ARABIC_FONT).grid(row=1, column=0, sticky="w", pady=4)

        tb.Label(body, font=ARABIC_FONT_SMALL, bootstyle="secondary", justify="right", anchor="e",
                 wraplength=380,
                 text="النصاب يُستخدم لاحقاً لتحديد عدد الأساتذة الافتراضيين الذين يولّدهم "
                      "البرنامج تلقائياً لأي مادة لم تُدخِل لها أسماء أساتذة كافية.").pack(
            anchor="e", pady=(0, 10), fill="x")

        btn_row = tb.Frame(body)
        btn_row.pack(fill="x")
        tb.Button(btn_row, text="إنشاء", command=self._confirm, bootstyle=SUCCESS).pack(side="right", padx=4)
        tb.Button(btn_row, text="إلغاء", command=self.destroy, bootstyle="secondary-outline").pack(
            side="right", padx=4)

    def _confirm(self):
        days = [d for d in scheduler.ALL_WEEKDAYS if self.day_vars[d].get()]
        if not days:
            messagebox.showerror("خطأ", "اختر يوماً واحداً على الأقل.")
            return
        try:
            periods_per_day = int(self.periods_var.get().strip())
            if periods_per_day <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("خطأ", "عدد الحصص اليومية يجب أن يكون رقماً صحيحاً أكبر من صفر.")
            return
        try:
            nisab = float(self.nisab_var.get().strip())
            if nisab <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("خطأ", "النصاب يجب أن يكون رقماً أكبر من صفر.")
            return

        # keep days in their natural weekday order regardless of click order
        ordered_days = [d for d in scheduler.ALL_WEEKDAYS if d in days]
        self.destroy()
        self.on_confirm(ordered_days, periods_per_day, nisab)


# -------------------------------------------------------------------- App --

class App(tb.Window):
    def __init__(self):
        super().__init__(
            title="برنامج توزيع الأساتذة والحصص",
            light_theme=LIGHT_THEME, dark_theme=DARK_THEME,
            size=(1200, 860),
            minsize=(900, 560),
            resizable=(True, True),
        )

        self.data = None
        self.current_path = DEFAULT_JSON_PATH
        self.dirty = False
        self._selected_idx = None
        # Last successful schedule (section_sched), used to warm-start the
        # solver on the NEXT "توليد البرامج" click (see scheduler.solve_
        # timetable's warm_start param) - lets a re-generation after a
        # small tweak converge from a known-good arrangement instead of
        # starting cold. Cleared whenever a different data file is loaded
        # (see _load_data) since warm-starting from an unrelated school's
        # schedule has nothing useful to offer.
        self._last_schedule_result = None

        self._build_menu()
        self._build_layout()
        # The unsaved-changes check on "ملف > خروج" only ever ran when the
        # user picked that exact menu item - closing the window the normal
        # way (the native close button/traffic light, Cmd+Q, Alt+F4) went
        # straight through with no warning at all, silently discarding any
        # unsaved edit (e.g. a manual assignment just added). Route the
        # window's own close button through the same check.
        self.protocol("WM_DELETE_WINDOW", self._on_exit)
        # Keep the window at its configured size regardless of how much
        # room the packed content would naturally like to occupy - the
        # scrollable areas (subject list, teacher names) shrink instead of
        # the window overflowing the user's screen.
        self.update_idletasks()
        self.pack_propagate(False)
        # Force the "البيانات" tab's narrow-subjects/wide-editor split (see
        # the comment above self._data_split in _build_layout) - deferred
        # with after() since a Panedwindow's sashpos() only takes effect
        # once the widget has an actual on-screen size, which isn't
        # guaranteed yet at this exact point even after update_idletasks().
        self.after(0, lambda: self._data_split.sashpos(0, 230))
        self._load_data(DEFAULT_JSON_PATH, is_default=True)

    # ---------------------------------------------------------------- menu
    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="مدرسة جديدة...", command=self._new_school)
        file_menu.add_command(label="فتح ملف JSON...", command=self._open_file)
        # Disabled on purpose - see the matching toolbar button and
        # _restore_default() for why. state="disabled" greys it out too.
        file_menu.add_command(label="استعادة البيانات الافتراضية (معطّلة)",
                               command=self._restore_default, state="disabled")
        file_menu.add_separator()
        file_menu.add_command(label="حفظ", command=self._save_file)
        file_menu.add_command(label="حفظ باسم...", command=self._save_file_as)
        file_menu.add_separator()
        file_menu.add_command(label="خروج", command=self._on_exit)
        menubar.add_cascade(label="ملف", menu=file_menu)

        subj_menu = tk.Menu(menubar, tearoff=0)
        subj_menu.add_command(label="إضافة مادة جديدة", command=self._add_subject)
        subj_menu.add_command(label="حذف المادة المحددة", command=self._remove_subject)
        menubar.add_cascade(label="المواد", menu=subj_menu)

        gen_menu = tk.Menu(menubar, tearoff=0)
        gen_menu.add_command(label="توليد البرامج (PDF)", command=self._generate)
        gen_menu.add_command(label="فتح مجلد المخرجات", command=self._open_output_dir)
        menubar.add_cascade(label="توليد", menu=gen_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="تبديل المظهر (فاتح/داكن)", command=self._toggle_theme)
        menubar.add_cascade(label="عرض", menu=view_menu)

        self.config(menu=menubar)

    # -------------------------------------------------------------- layout
    def _build_layout(self):
        # ---- header --------------------------------------------------
        header = tb.Frame(self, padding=(16, 6))
        header.pack(side="top", fill="x")

        title_box = tb.Frame(header)
        title_box.pack(side="right")
        tb.Label(title_box, text="برنامج توزيع الأساتذة والحصص", font=ARABIC_FONT_TITLE).pack(anchor="e")
        tb.Label(title_box, text="إدارة الخطة الدراسية وتوليد الجداول والقوائم",
                 font=ARABIC_FONT_SUBTITLE, bootstyle="secondary").pack(anchor="e")

        self.theme_btn = tb.Button(header, text="🌙 المظهر الداكن", bootstyle="outline-secondary",
                                    command=self._toggle_theme)
        self.theme_btn.pack(side="left", padx=4)
        tb.ToolTip(self.theme_btn, text="التبديل بين المظهر الفاتح والداكن")

        tb.Separator(self).pack(side="top", fill="x")

        # ---- toolbar ---------------------------------------------------
        toolbar = tb.Frame(self, padding=(16, 4))
        toolbar.pack(side="top", fill="x")

        def tool_button(text, command, style, tip):
            b = tb.Button(toolbar, text=text, command=command, bootstyle=style)
            b.pack(side="right", padx=3)
            tb.ToolTip(b, text=tip)
            return b

        tool_button("🆕 مدرسة جديدة", self._new_school, "outline-warning",
                    "إنشاء ملف بيانات فارغ لمدرسة جديدة (يطلب أيام الدراسة وعدد الحصص والنصاب)")
        tool_button("📂 فتح", self._open_file, "outline-primary", "فتح ملف بيانات JSON آخر")
        tool_button("💾 حفظ", self._save_file, "outline-primary", "حفظ التعديلات الحالية")
        # Disabled on purpose (per explicit request): this button used to
        # silently reload the sample default data over whatever the user
        # currently has open (no confirmation at all when there were no
        # unsaved edits yet), which is exactly the kind of accidental-click
        # data loss a real school file must never risk. Kept visible but
        # inert rather than removed, so nothing else in the toolbar shifts
        # position; see _restore_default() below for the matching no-op.
        restore_default_btn = tool_button(
            "↺ استعادة الافتراضي", self._restore_default, "outline-secondary",
            "معطّل عمداً لمنع استبدال بياناتك الحالية بالخطأ")
        restore_default_btn.config(state="disabled")
        tb.Separator(toolbar, orient="vertical").pack(side="right", fill="y", padx=8)
        tool_button("+ مادة جديدة", self._add_subject, "outline-success", "إضافة مادة جديدة")
        tool_button("حذف المادة", self._remove_subject, "outline-danger", "حذف المادة المحددة")
        tb.Separator(toolbar, orient="vertical").pack(side="right", fill="y", padx=8)
        tool_button("👥 توليد الأساتذة الناقصين", self._fill_missing_teachers, "outline-info",
                    "إنشاء أساتذة افتراضيين الآن لأي مادة ينقصها أساتذة كافون، حسب النصاب المعتمد")

        self.path_label = tb.Label(toolbar, text="", font=ARABIC_FONT_SMALL, bootstyle="secondary")
        self.path_label.pack(side="left")

        # ---- sections editor --------------------------------------------
        self.sections_container = tb.Frame(self, padding=(16, 4))
        self.sections_container.pack(side="top", fill="x")

        # ---- bottom bar (packed BEFORE the expanding notebook below, so it
        # reserves its slice of the window first - pack() allocates space in
        # packing order, and an expand=True widget packed earlier would
        # otherwise claim the entire remaining cavity, leaving nothing for
        # widgets packed after it even with side="bottom") ------------------
        bottom = tb.Frame(self, padding=(16, 6))
        bottom.pack(side="bottom", fill="x")
        self.generate_btn = tb.Button(bottom, text="⚙ توليد البرامج الثلاثة (PDF)",
                                       command=self._generate, bootstyle=SUCCESS)
        self.generate_btn.pack(side="right", padx=4, ipadx=10, ipady=4)

        self.constraints_report_btn = tb.Button(
            bottom, text="📋 تقرير شروط الأساتذة (PDF)",
            command=self._generate_constraints_report, bootstyle="info-outline")
        self.constraints_report_btn.pack(side="right", padx=4, ipadx=6, ipady=4)
        tb.ToolTip(self.constraints_report_btn,
                   text="توليد تقرير مستقل بكل قيود الجدولة المفعّلة لكل أستاذ بالتفصيل - "
                        "لا يحتاج تشغيل الحلّ، ومستقل تماماً عن زر توليد برامج الشعب والأساتذة")

        self.complexity_report_btn = tb.Button(
            bottom, text="🔢 ترتيب حسب تعقيد الشروط (PDF)",
            command=self._generate_complexity_report, bootstyle="info-outline")
        self.complexity_report_btn.pack(side="right", padx=4, ipadx=6, ipady=4)
        tb.ToolTip(self.complexity_report_btn,
                   text="ترتيب الأساتذة من الأكثر إلى الأقل حسب عدد وصعوبة قيودهم المفعّلة، "
                        "مع ذكرها جميعاً في عمود واحد - لا يحتاج تشغيل الحلّ أيضاً")

        self.fulfillment_report_btn = tb.Button(
            bottom, text="📊 تقرير نسبة تحقق الرغبات (PDF)",
            command=self._generate_fulfillment_report, bootstyle="info-outline")
        self.fulfillment_report_btn.pack(side="right", padx=4, ipadx=6, ipady=4)
        tb.ToolTip(self.fulfillment_report_btn,
                   text="تقرير مستقل يصنّف كل أستاذ لديه قيد جدولة إلى تحقيق كامل/جزئي/معدوم "
                        "لرغبته، مع مخطط دائري - بخلاف الزرّين السابقين هذا يحتاج تشغيل الحلّ "
                        "فعلياً (حتى 5 دقائق) لأن النسبة لا تُعرف إلا بعد حل جدول كامل، لكنه "
                        "لا يولّد برامج الشعب/الأساتذة نفسها")

        tb.Button(bottom, text="فتح مجلد المخرجات", command=self._open_output_dir,
                  bootstyle="secondary-outline").pack(side="right", padx=4)

        self.progress = tb.Progressbar(bottom, mode="indeterminate", bootstyle="success-striped",
                                        length=160)
        self.progress.pack(side="right", padx=10)

        self.status_var = tk.StringVar(value="جاهز.")
        tb.Label(bottom, textvariable=self.status_var, font=ARABIC_FONT).pack(side="left")

        # ---- notebook (data / dashboard) --------------------------------
        self.notebook = tb.Notebook(self, padding=(8, 4))
        self.notebook.pack(side="top", fill="both", expand=True, padx=16, pady=5)

        data_tab = tb.Frame(self.notebook)
        self.notebook.add(data_tab, text="  البيانات  ")

        # Subjects list (left) kept deliberately narrow, and the subject
        # detail/editor pane (right - periods, teacher names, manual
        # assignment) given most of the width, per explicit request - a
        # ttk.Panedwindow's INITIAL split follows its children's natural
        # requested size, not the `weight` argument alone (weight only
        # governs how extra space is redistributed on a later resize), so
        # the narrow/wide split here is additionally forced explicitly via
        # sashpos() once the window is drawn - see self._data_split below.
        main = tb.Panedwindow(data_tab, orient="horizontal")
        main.pack(fill="both", expand=True)
        self._data_split = main

        left = tb.Frame(main, padding=(0, 0, 8, 0))
        main.add(left, weight=1)

        tb.Label(left, text="المواد", font=ARABIC_FONT_BOLD).pack(anchor="e", pady=(0, 6))
        columns = ("category", "count")
        self.tree = tb.Treeview(left, columns=columns, show="tree headings", height=12,
                                 bootstyle=PRIMARY)
        self.tree.heading("#0", text="المادة")
        self.tree.heading("category", text="التصنيف")
        self.tree.heading("count", text="عدد الأساتذة")
        self.tree.column("#0", width=160, anchor="e")
        self.tree.column("category", width=70, anchor="center")
        self.tree.column("count", width=90, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_subject)

        right = tb.Frame(main)
        main.add(right, weight=5)
        self.editor = SubjectEditor(right, on_change=self._mark_dirty_and_refresh_tree_row)
        self.editor.pack(fill="both", expand=True)

        dash_tab = tb.Frame(self.notebook)
        self.notebook.add(dash_tab, text="  لوحة المعلومات  ")
        self.dashboard = DashboardTab(dash_tab)
        self.dashboard.pack(fill="both", expand=True)

        constraints_tab = tb.Frame(self.notebook)
        self.notebook.add(constraints_tab, text="  قيود الجدولة  ")
        self.constraints_tab = ConstraintsTab(constraints_tab, on_change=self._mark_dirty)
        self.constraints_tab.pack(fill="both", expand=True)

        roster_tab = tb.Frame(self.notebook)
        self.notebook.add(roster_tab, text="  قائمة الأساتذة  ")
        self.teacher_roster_tab = TeacherRosterTab(roster_tab, on_change=self._on_teacher_roster_change)
        self.teacher_roster_tab.pack(fill="both", expand=True)

        log_tab = tb.Frame(self.notebook, padding=8)
        self.notebook.add(log_tab, text="  سجل العمليات  ")
        log_inner = tb.Frame(log_tab)
        log_inner.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_inner, font=("Courier New", 10),
                                 state="disabled", bg="#1e1e1e", fg="#d7d7d7",
                                 insertbackground="#d7d7d7", relief="flat")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = tb.Scrollbar(log_inner, orient="vertical", command=self.log_text.yview,
                                   bootstyle="round")
        log_scroll.pack(side="left", fill="y")
        self.log_text.config(yscrollcommand=log_scroll.set)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ------------------------------------------------------------- logging
    def _log(self, message):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")
        self.update_idletasks()

    # --------------------------------------------------------------- theme
    def _toggle_theme(self):
        mode = self.style.toggle_theme()
        if mode == "dark":
            self.theme_btn.config(text="☀ المظهر الفاتح")
        else:
            self.theme_btn.config(text="🌙 المظهر الداكن")
        self._configure_category_tags()

    def _configure_category_tags(self):
        mode = self.style.theme_mode
        palette = CATEGORY_COLORS.get(mode, CATEGORY_COLORS["light"])
        for category, (bg, fg) in palette.items():
            self.tree.tag_configure(f"cat::{category}", background=bg, foreground=fg)

    # ------------------------------------------------------------- tabs
    def _on_tab_changed(self, event=None):
        current = self.notebook.select()
        if not current:
            return
        text = self.notebook.tab(current, "text").strip()
        if text == "لوحة المعلومات":
            self.dashboard.refresh(self.data)
        elif text == "قيود الجدولة":
            self.constraints_tab.set_data(self.data)
        elif text == "قائمة الأساتذة":
            self.teacher_roster_tab.set_data(self.data)

    # ---------------------------------------------------------- data load
    def _load_data(self, path, is_default=False):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            messagebox.showerror("خطأ", f"تعذّر فتح الملف:\n{exc}")
            return
        ensure_scheduling_constraints(data)
        scheduler.ensure_teacher_roster(data)
        self.data = data
        self.current_path = path
        self.dirty = False
        self._last_schedule_result = None
        label = "البيانات الافتراضية" if is_default else path
        self.path_label.config(text=f"الملف الحالي: {label}")
        self._rebuild_sections_editor()
        self._rebuild_tree()
        self._log(f"تم تحميل البيانات من: {path}")
        self.dashboard.refresh(self.data)
        self.constraints_tab.set_data(self.data)
        self.teacher_roster_tab.set_data(self.data)

    def _rebuild_sections_editor(self):
        for w in self.sections_container.winfo_children():
            w.destroy()
        editor = SectionsEditor(self.sections_container, self.data, on_change=self._on_sections_changed)
        editor.pack(fill="x")

    def _on_sections_changed(self):
        self._mark_dirty()
        # Keep the subject editor's per-grade section-count/total rows (see
        # SubjectEditor.refresh_section_counts) in sync immediately, rather
        # than only reflecting the change after the subject is reselected.
        self.editor.refresh_section_counts(self.data["sections"])

    def _rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._configure_category_tags()
        for i, subject in enumerate(self.data["subjects"]):
            category = subject.get("category", "")
            self.tree.insert("", "end", iid=str(i), text=subject["name"],
                              values=(category, len(subject["names"])),
                              tags=(f"cat::{category}",))

    def _refresh_tree_row(self, idx):
        subject = self.data["subjects"][idx]
        category = subject.get("category", "")
        self.tree.item(str(idx), text=subject["name"],
                        values=(category, len(subject["names"])),
                        tags=(f"cat::{category}",))

    # --------------------------------------------------------- subject ops
    def _on_select_subject(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        subject = self.data["subjects"][idx]
        self.editor.load_subject(subject, self.data["meta"]["grade_order"], self.data["meta"]["grade_labels"],
                                  self.data["sections"], self.data)
        self._selected_idx = idx

    def _mark_dirty(self):
        self.dirty = True

    def _mark_dirty_and_refresh_tree_row(self):
        self.dirty = True
        if self._selected_idx is not None:
            self._refresh_tree_row(self._selected_idx)

    def _on_teacher_roster_change(self):
        """
        Wired as the TeacherRosterTab's on_change: an add/rename/delete
        there can change teacher names that appear elsewhere (a subject's
        currently-loaded names list, per-teacher scheduling-constraint
        overrides) or the counts shown in the subjects tree/dashboard, so
        refresh every view that could be showing stale data instead of only
        the roster tab itself.
        """
        self._mark_dirty()
        self._rebuild_tree()
        if self._selected_idx is not None:
            self.editor.load_subject(self.data["subjects"][self._selected_idx],
                                      self.data["meta"]["grade_order"], self.data["meta"]["grade_labels"],
                                      self.data["sections"], self.data)
        self.dashboard.refresh(self.data)
        self.constraints_tab.set_data(self.data)

    def _add_subject(self):
        name = simpledialog.askstring("مادة جديدة", "اسم المادة الجديدة:", parent=self)
        if not name:
            return
        cols = self.data["meta"]["grade_order"]
        new_subject = {
            "name": name.strip(),
            "category": "تربوية",
            "periods": {g: 0 for g in cols},
            "names": [],
        }
        self.data["subjects"].append(new_subject)
        self._rebuild_tree()
        self._mark_dirty()
        new_idx = len(self.data["subjects"]) - 1
        self.tree.selection_set(str(new_idx))
        self.tree.see(str(new_idx))
        self._on_select_subject()

    def _remove_subject(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("تنبيه", "اختر مادة أولاً.")
            return
        idx = int(sel[0])
        subject = self.data["subjects"][idx]
        if messagebox.askyesno("تأكيد", f"حذف المادة: {subject['name']}؟"):
            del self.data["subjects"][idx]
            self._selected_idx = None
            self._rebuild_tree()
            self._mark_dirty()

    # ------------------------------------------------------------- new school
    def _new_school(self):
        if self.dirty and not messagebox.askyesno(
                "تنبيه", "لديك تعديلات غير محفوظة على الملف الحالي. المتابعة لإنشاء مدرسة جديدة دون حفظها؟"):
            return
        dialog = NewSchoolDialog(self, on_confirm=self._create_new_school_file)
        dialog.grab_set()

    def _create_new_school_file(self, days, periods_per_day, nisab):
        path = filedialog.asksaveasfilename(
            title="حفظ ملف المدرسة الجديدة", initialdir=USER_DATA_DIR,
            defaultextension=".json", filetypes=[("JSON files", "*.json")])
        if not path:
            return
        data = scheduler.new_blank_data(days, periods_per_day, nisab)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            messagebox.showerror("خطأ", f"تعذّر إنشاء الملف:\n{exc}")
            return
        self._load_data(path)
        self._log(f"تم إنشاء ملف مدرسة جديدة: {path}")
        messagebox.showinfo(
            "تم الإنشاء",
            "تم إنشاء ملف المدرسة الجديدة بنجاح.\n"
            "أضف الآن المواد وعدد حصصها وأسماء الأساتذة من تبويب \"البيانات\"، "
            "وعدّل عدد الشعب لكل صف من الشريط أعلاه حسب حاجتك."
        )

    def _fill_missing_teachers(self):
        added = scheduler.ensure_min_teachers(self.data)
        if not added:
            messagebox.showinfo("لا حاجة", "كل المواد لديها أساتذة كافون بالفعل - لم يُضَف شيء.")
            return
        self._mark_dirty()
        self._rebuild_tree()
        if self._selected_idx is not None:
            self.editor.load_subject(self.data["subjects"][self._selected_idx],
                                      self.data["meta"]["grade_order"], self.data["meta"]["grade_labels"],
                                      self.data["sections"], self.data)
        self.teacher_roster_tab.refresh()
        lines = [f"- {name}: {', '.join(names)}" for name, names in added]
        self._log("تم توليد أساتذة افتراضيين:\n" + "\n".join(lines))
        messagebox.showinfo(
            "تم التوليد",
            "تمت إضافة أساتذة افتراضيين للمواد التالية (راجع سجل العمليات للتفاصيل):\n" +
            "، ".join(name for name, _ in added)
        )

    # ------------------------------------------------------------- file io
    def _open_file(self):
        if self.dirty and not messagebox.askyesno(
                "تنبيه", "لديك تعديلات غير محفوظة. هل تريد المتابعة دون حفظها؟"):
            return
        path = filedialog.askopenfilename(
            title="فتح ملف بيانات", initialdir=USER_DATA_DIR,
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if path:
            self._load_data(path)

    def _restore_default(self):
        # Disabled on purpose (explicit, important request): this used to
        # silently overwrite the currently-open school data with the sample
        # default file - with NO confirmation at all when there were no
        # unsaved edits yet (self.dirty == False), which made one accidental
        # click on a real school's file destructive. The toolbar button and
        # the menu item are both greyed out (state="disabled") so this can't
        # normally be reached anymore, but this early return makes it a
        # guaranteed no-op even if something still calls it directly - it
        # must never touch self.data. The automatic load of the default file
        # on first startup (see App.__init__) is untouched and still works
        # exactly as before.
        return

    def _save_file(self):
        self._write_json(self.current_path)

    def _save_file_as(self):
        path = filedialog.asksaveasfilename(
            title="حفظ باسم", initialdir=USER_DATA_DIR,
            defaultextension=".json", filetypes=[("JSON files", "*.json")])
        if path:
            self._write_json(path)
            self.current_path = path
            self.path_label.config(text=f"الملف الحالي: {path}")

    def _write_json(self, path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            self.dirty = False
            self._log(f"تم الحفظ في: {path}")
            self.status_var.set("تم الحفظ.")
        except Exception as exc:
            messagebox.showerror("خطأ", f"تعذّر الحفظ:\n{exc}")

    # ------------------------------------------------------------ generate
    def _generate(self):
        # Fill in any placeholder teachers on the LIVE data first (not just
        # the copy handed to the solver below) so they persist into the
        # editable dataset and the "أسماء الأساتذة" list afterward, instead
        # of only existing ephemerally for this one generation run.
        added = scheduler.ensure_min_teachers(self.data)
        if added:
            self._mark_dirty()
            self._rebuild_tree()
            if self._selected_idx is not None:
                self.editor.load_subject(self.data["subjects"][self._selected_idx],
                                          self.data["meta"]["grade_order"], self.data["meta"]["grade_labels"],
                                          self.data["sections"], self.data)
            self.teacher_roster_tab.refresh()
            lines = [f"- {name}: {', '.join(names)}" for name, names in added]
            self._log("تم توليد أساتذة افتراضيين قبل التوليد:\n" + "\n".join(lines))

        data_copy = copy.deepcopy(self.data)
        self.status_var.set("جاري التوليد...")
        self.generate_btn.config(state="disabled")
        # Also block the fulfillment-report button (it runs its own CP-SAT
        # solve) so two solves never race each other over the same CPU
        # workers at once.
        self.fulfillment_report_btn.config(state="disabled")
        self.progress.start(12)
        self._log("بدء توليد البرامج...")
        thread = threading.Thread(target=self._generate_worker, args=(data_copy,), daemon=True)
        thread.start()

    def _generate_worker(self, data):
        def progress(msg):
            self.after(0, self._log, msg)

        # Warm-start from the previous successful run, if any (see
        # scheduler.solve_timetable's warm_start param) - harmless to pass
        # even if this generation's data has since changed somewhat, since
        # any assignment that no longer matches a live requirement is just
        # ignored by the solver.
        warm_start = self._last_schedule_result
        try:
            status_name, sched, constraint_notes, constraint_fulfillment = scheduler.solve_timetable(
                data, progress=progress, warm_start=warm_start)
            paths = pdf_gen.generate_all_pdfs(data, sched, OUTPUT_DIR, progress=progress,
                                               constraint_notes=constraint_notes,
                                               constraint_fulfillment=constraint_fulfillment)
        except Exception as exc:
            self.after(0, self._generation_failed, str(exc))
            return
        self.after(0, self._generation_done, paths, sched)

    def _generation_failed(self, message):
        self.progress.stop()
        self.generate_btn.config(state="normal")
        self.fulfillment_report_btn.config(state="normal")
        self._log(f"فشل التوليد: {message}")
        self.status_var.set("فشل التوليد.")
        tb.ToastNotification(title="فشل التوليد", message=message, bootstyle="danger",
                              duration=6000).show_toast()
        messagebox.showerror("خطأ في التوليد", message)

    def _generation_done(self, paths, sched=None):
        self.progress.stop()
        self.generate_btn.config(state="normal")
        self.fulfillment_report_btn.config(state="normal")
        if sched is not None:
            self._last_schedule_result = sched
        self._log("اكتمل التوليد بنجاح:")
        for name, path in paths.items():
            self._log(f"  - {name}: {path}")
        self.status_var.set("اكتمل التوليد بنجاح.")
        count_words = {1: "الملف", 2: "الملفين", 3: "الملفات الثلاثة", 4: "الملفات الأربعة",
                       5: "الملفات الخمسة", 6: "الملفات الستة"}
        files_phrase = count_words.get(len(paths), f"الملفات ({len(paths)})")
        done_msg = f"تم توليد {files_phrase} بنجاح."
        tb.ToastNotification(title="اكتمل التوليد", message=done_msg,
                              bootstyle="success", duration=5000).show_toast()
        if messagebox.askyesno("اكتمل", f"{done_msg} هل تريد فتح مجلد المخرجات؟"):
            self._open_output_dir()

    # ------------------------------------------------- constraints report
    def _generate_constraints_report(self):
        """
        Build تقرير_الشروط_الخاصة_بجدولة_الأساتذة.pdf on its own - fully
        independent of the main "توليد البرامج" button/thread above: it
        needs no solved schedule, only the current scheduling_constraints
        settings, so it runs directly (no worker thread) since building it
        is fast even for a large roster.
        """
        data_copy = copy.deepcopy(self.data)
        try:
            path = pdf_gen.generate_constraints_report_pdf(data_copy, OUTPUT_DIR)
        except RuntimeError as exc:
            messagebox.showinfo("لا توجد قيود لعرضها", str(exc))
            return
        except Exception as exc:
            self._log(f"فشل توليد تقرير الشروط: {exc}")
            messagebox.showerror("خطأ في التوليد", f"تعذّر توليد تقرير الشروط:\n{exc}")
            return

        self._log(f"تم توليد تقرير الشروط: {path}")
        self.status_var.set("تم توليد تقرير الشروط بنجاح.")
        tb.ToastNotification(title="اكتمل التوليد",
                              message="تم توليد تقرير الشروط الخاصة بجدولة الأساتذة بنجاح.",
                              bootstyle="success", duration=5000).show_toast()
        if messagebox.askyesno("اكتمل", "تم توليد التقرير بنجاح. هل تريد فتح مجلد المخرجات؟"):
            self._open_output_dir()

    def _generate_complexity_report(self):
        """
        Build ترتيب_الأساتذة_حسب_تعقيد_الشروط.pdf on its own - same
        independence rationale as _generate_constraints_report above (no
        solved schedule needed, so no worker thread).
        """
        data_copy = copy.deepcopy(self.data)
        try:
            path = pdf_gen.generate_complexity_report_pdf(data_copy, OUTPUT_DIR)
        except RuntimeError as exc:
            messagebox.showinfo("لا توجد قيود لعرضها", str(exc))
            return
        except Exception as exc:
            self._log(f"فشل توليد ترتيب التعقيد: {exc}")
            messagebox.showerror("خطأ في التوليد", f"تعذّر توليد ترتيب التعقيد:\n{exc}")
            return

        self._log(f"تم توليد ترتيب تعقيد الشروط: {path}")
        self.status_var.set("تم توليد ترتيب تعقيد الشروط بنجاح.")
        tb.ToastNotification(title="اكتمل التوليد",
                              message="تم توليد ترتيب الأساتذة حسب تعقيد الشروط بنجاح.",
                              bootstyle="success", duration=5000).show_toast()
        if messagebox.askyesno("اكتمل", "تم توليد الترتيب بنجاح. هل تريد فتح مجلد المخرجات؟"):
            self._open_output_dir()

    # ------------------------------------------------ fulfillment report
    def _generate_fulfillment_report(self):
        """
        Build تقرير_نسبة_تحقق_رغبات_الأساتذة.pdf on its own, independent of
        "توليد البرامج" - unlike the two report buttons above, this one
        DOES need an actual solved schedule (see
        pdf_gen.generate_fulfillment_report_pdf), so it runs in its own
        background thread with the same progress bar/disabled-buttons
        treatment as the main generate button, and is mutually exclusive
        with it (see the fulfillment_report_btn disable in _generate).
        """
        data_copy = copy.deepcopy(self.data)
        self.status_var.set("جاري قياس نسبة تحقق الرغبات...")
        self.generate_btn.config(state="disabled")
        self.fulfillment_report_btn.config(state="disabled")
        self.progress.start(12)
        self._log("بدء توليد تقرير نسبة تحقق الرغبات...")
        thread = threading.Thread(
            target=self._fulfillment_report_worker, args=(data_copy,), daemon=True)
        thread.start()

    def _fulfillment_report_worker(self, data):
        def progress(msg):
            self.after(0, self._log, msg)

        warm_start = self._last_schedule_result
        try:
            path, sched = pdf_gen.generate_fulfillment_report_pdf(
                data, OUTPUT_DIR, progress=progress, warm_start=warm_start)
        except RuntimeError as exc:
            self.after(0, self._fulfillment_report_failed, str(exc), True)
            return
        except Exception as exc:
            self.after(0, self._fulfillment_report_failed, str(exc), False)
            return
        self.after(0, self._fulfillment_report_done, path, sched)

    def _fulfillment_report_failed(self, message, is_info):
        self.progress.stop()
        self.generate_btn.config(state="normal")
        self.fulfillment_report_btn.config(state="normal")
        self.status_var.set("فشل توليد تقرير نسبة التحقق.")
        if is_info:
            # "no constraints configured at all" - an expected, non-error
            # outcome, same treatment as the other two report buttons.
            self._log(f"تقرير نسبة التحقق: {message}")
            messagebox.showinfo("لا توجد قيود لعرضها", message)
        else:
            self._log(f"فشل توليد تقرير نسبة التحقق: {message}")
            tb.ToastNotification(title="فشل التوليد", message=message, bootstyle="danger",
                                  duration=6000).show_toast()
            messagebox.showerror("خطأ في التوليد", message)

    def _fulfillment_report_done(self, path, sched):
        self.progress.stop()
        self.generate_btn.config(state="normal")
        self.fulfillment_report_btn.config(state="normal")
        self._last_schedule_result = sched
        self._log(f"تم توليد تقرير نسبة تحقق الرغبات: {path}")
        self.status_var.set("تم توليد تقرير نسبة تحقق الرغبات بنجاح.")
        tb.ToastNotification(title="اكتمل التوليد",
                              message="تم توليد تقرير نسبة تحقق رغبات الأساتذة بنجاح.",
                              bootstyle="success", duration=5000).show_toast()
        if messagebox.askyesno("اكتمل", "تم توليد التقرير بنجاح. هل تريد فتح مجلد المخرجات؟"):
            self._open_output_dir()

    def _open_output_dir(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(OUTPUT_DIR)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", OUTPUT_DIR])
            else:
                subprocess.Popen(["xdg-open", OUTPUT_DIR])
        except Exception:
            messagebox.showinfo("مجلد المخرجات", OUTPUT_DIR)

    def _on_exit(self):
        if self.dirty and not messagebox.askyesno(
                "تنبيه", "لديك تعديلات غير محفوظة. هل تريد الخروج دون حفظها؟"):
            return
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

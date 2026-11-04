"""Audio Ripper — Tk GUI.

Paste a YouTube URL, pick a range on the waveform and export it as WAV.

Views
-----
* Overview strip: the whole video. The bright "window" box marks the part
  shown in the zoomed view below. Drag the box to move it, drag its left or
  right edge to expand / narrow it, or click anywhere to jump there.
* Zoomed view: drag to select a range, drag the selection edges to adjust it,
  drag inside the selection to move it, click to place the play cursor.
  Scroll up/down to zoom around the mouse, scroll sideways (or Shift+scroll)
  to pan.
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import core

APP_NAME = "Audio Ripper"
IS_MAC = sys.platform == "darwin"
MOD = "Command" if IS_MAC else "Control"
MOD_LABEL = "Cmd" if IS_MAC else "Ctrl"

# Colours
BG = "#17181c"
BG_SEL = "#22364a"
WAVE = "#4f6f8f"
WAVE_SEL = "#63d0ff"
GRID = "#2a2c33"
TEXT = "#9aa0aa"
EDGE = "#ffffff"
CURSOR = "#ffd24a"
PLAYHEAD = "#ff5c5c"
VIEWBOX = "#ffd24a"
CENTER = "#30323a"

EDGE_GRAB_PX = 7
MIN_VIEW_FRAMES = 64
RULER_H = 22
PLAY_NOSEL_LIMIT = 300.0  # seconds played from cursor when nothing is selected

SAMPLE_RATES = ["Original", "44100", "48000", "96000", "22050"]
CHANNEL_OPTS = ["Original", "Stereo", "Mono"]

_NICE_STEPS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
               1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]


class AudioRipperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.workdir = core.make_workdir()
        self.player = core.Player(self.workdir)
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_flag = threading.Event()
        self.busy = False
        self.load_dir: Optional[str] = None

        self.audio: Optional[core.AudioData] = None
        self.view_start = 0.0
        self.view_end = 1.0
        self.sel_start: Optional[float] = None
        self.sel_end: Optional[float] = None
        self.cursor = 0.0

        self.play_from = 0.0
        self.play_to = 0.0
        self.play_t0 = 0.0
        self.playhead_item = None

        self._drag = None          # main view drag state
        self._ov_drag = None       # overview drag state
        self._redraw_pending = False

        self._build_ui()
        self._bind_keys()
        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._update_controls()
        self.request_redraw()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = self.root
        root.title(APP_NAME)
        root.geometry("1100x640")
        root.minsize(760, 480)

        self._build_menu()

        outer = ttk.Frame(root, padding=(12, 10, 12, 8))
        outer.pack(fill="both", expand=True)

        # --- URL row
        url_row = ttk.Frame(outer)
        url_row.pack(fill="x")
        ttk.Label(url_row, text="YouTube URL:").pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.url_entry.bind("<Return>", lambda e: self.load_url())
        self.paste_btn = ttk.Button(url_row, text="Paste & Load", command=self.paste_and_load)
        self.paste_btn.pack(side="left", padx=(0, 6))
        self.load_btn = ttk.Button(url_row, text="Load", command=self.load_url)
        self.load_btn.pack(side="left")
        self.file_btn = ttk.Button(url_row, text="Open File…", command=self.open_file)
        self.file_btn.pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(url_row, text="Cancel", command=self.cancel_load)
        self.cancel_btn.pack(side="left", padx=(6, 0))

        # --- Title
        self.title_var = tk.StringVar(value="No audio loaded")
        ttk.Label(outer, textvariable=self.title_var,
                  font=("TkDefaultFont", 13, "bold")).pack(fill="x", pady=(10, 4))

        # --- Overview (whole video)
        ov_label = ttk.Frame(outer)
        ov_label.pack(fill="x")
        ttk.Label(ov_label, text="Whole video — drag the yellow window to move it, "
                                 "drag its edges to expand or narrow it").pack(side="left")
        self.ov = tk.Canvas(outer, height=64, bg=BG, highlightthickness=0, bd=0)
        self.ov.pack(fill="x", pady=(2, 8))

        # --- Main zoomed view
        mv_label = ttk.Frame(outer)
        mv_label.pack(fill="x")
        ttk.Label(mv_label, text="Zoomed view — drag to select, drag edges to adjust, "
                                 "scroll to zoom").pack(side="left")
        self.view_info_var = tk.StringVar()
        ttk.Label(mv_label, textvariable=self.view_info_var).pack(side="right")
        self.wv = tk.Canvas(outer, height=260, bg=BG, highlightthickness=0, bd=0)
        self.wv.pack(fill="both", expand=True, pady=(2, 6))

        # --- Zoom buttons
        zoom_row = ttk.Frame(outer)
        zoom_row.pack(fill="x", pady=(0, 6))
        self.zoom_btns = [
            ttk.Button(zoom_row, text="Zoom In (+)", command=lambda: self.zoom(0.5)),
            ttk.Button(zoom_row, text="Zoom Out (−)", command=lambda: self.zoom(2.0)),
            ttk.Button(zoom_row, text="Zoom to Selection (Z)", command=self.zoom_selection),
            ttk.Button(zoom_row, text="Show Whole Video (0)", command=self.show_all),
        ]
        for b in self.zoom_btns:
            b.pack(side="left", padx=(0, 6))
        self.selall_btn = ttk.Button(zoom_row, text="Select All (A)", command=self.select_all)
        self.selall_btn.pack(side="left", padx=(0, 6))
        self.clearsel_btn = ttk.Button(zoom_row, text="Clear Selection (Esc)",
                                       command=self.clear_selection)
        self.clearsel_btn.pack(side="left")
        self.cursor_var = tk.StringVar(value="Cursor: 0:00.000")
        ttk.Label(zoom_row, textvariable=self.cursor_var).pack(side="right")

        # --- Selection + playback
        sel_row = ttk.Frame(outer)
        sel_row.pack(fill="x", pady=(0, 6))
        ttk.Label(sel_row, text="Start:").pack(side="left")
        self.start_var = tk.StringVar()
        self.start_entry = ttk.Entry(sel_row, textvariable=self.start_var, width=13)
        self.start_entry.pack(side="left", padx=(4, 4))
        self.set_start_btn = ttk.Button(sel_row, text="◀ at Cursor ( [ )", width=15,
                                        command=self.set_start_at_cursor)
        self.set_start_btn.pack(side="left", padx=(0, 12))
        ttk.Label(sel_row, text="End:").pack(side="left")
        self.end_var = tk.StringVar()
        self.end_entry = ttk.Entry(sel_row, textvariable=self.end_var, width=13)
        self.end_entry.pack(side="left", padx=(4, 4))
        self.set_end_btn = ttk.Button(sel_row, text="at Cursor ( ] ) ▶", width=15,
                                      command=self.set_end_at_cursor)
        self.set_end_btn.pack(side="left", padx=(0, 12))
        self.len_var = tk.StringVar(value="Length: —")
        ttk.Label(sel_row, textvariable=self.len_var).pack(side="left")
        for ent in (self.start_entry, self.end_entry):
            ent.bind("<Return>", self._entries_changed)
            ent.bind("<FocusOut>", self._entries_changed)

        self.stop_btn = ttk.Button(sel_row, text="■ Stop", command=self.stop)
        self.stop_btn.pack(side="right")
        self.play_btn = ttk.Button(sel_row, text="▶ Play (Space)", command=self.toggle_play)
        self.play_btn.pack(side="right", padx=(0, 6))

        # --- Export
        exp_row = ttk.Frame(outer)
        exp_row.pack(fill="x", pady=(2, 6))
        ttk.Label(exp_row, text="WAV sample rate:").pack(side="left")
        self.rate_var = tk.StringVar(value="Original")
        ttk.Combobox(exp_row, textvariable=self.rate_var, values=SAMPLE_RATES,
                     width=9, state="readonly").pack(side="left", padx=(4, 12))
        ttk.Label(exp_row, text="Channels:").pack(side="left")
        self.ch_var = tk.StringVar(value="Original")
        ttk.Combobox(exp_row, textvariable=self.ch_var, values=CHANNEL_OPTS,
                     width=9, state="readonly").pack(side="left", padx=(4, 12))
        self.fade_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(exp_row, text="Short fade in/out (10 ms, avoids clicks)",
                        variable=self.fade_var).pack(side="left")
        self.export_btn = ttk.Button(exp_row, text=f"Export WAV…  ({MOD_LABEL}+E)",
                                     command=self.export)
        self.export_btn.pack(side="right")

        # --- Status bar
        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(4, 0))
        self.status_var = tk.StringVar(value="Copy a YouTube link, then click Paste & Load.")
        ttk.Label(status, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(status, length=220, mode="determinate", maximum=1.0)
        self.progress.pack(side="right")

        # Canvas events
        self.wv.bind("<Configure>", lambda e: self.request_redraw())
        self.ov.bind("<Configure>", lambda e: self.request_redraw())
        self.wv.bind("<ButtonPress-1>", self._wv_press)
        self.wv.bind("<B1-Motion>", self._wv_motion)
        self.wv.bind("<ButtonRelease-1>", self._wv_release)
        self.wv.bind("<Motion>", self._wv_hover)
        self.ov.bind("<ButtonPress-1>", self._ov_press)
        self.ov.bind("<B1-Motion>", self._ov_motion)
        self.ov.bind("<ButtonRelease-1>", lambda e: setattr(self, "_ov_drag", None))
        self.ov.bind("<Motion>", self._ov_hover)
        for c in (self.wv, self.ov):
            c.bind("<MouseWheel>", self._wheel)
            c.bind("<Shift-MouseWheel>", self._hwheel)
            c.bind("<Button-4>", lambda e: self._wheel(e, 1))
            c.bind("<Button-5>", lambda e: self._wheel(e, -1))
            c.bind("<Shift-Button-4>", lambda e: self._hwheel(e, 1))
            c.bind("<Shift-Button-5>", lambda e: self._hwheel(e, -1))

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        filem = tk.Menu(menubar, tearoff=0)
        filem.add_command(label="Load URL", command=self.load_url)
        filem.add_command(label="Open Audio/Video File…", accelerator=f"{MOD_LABEL}+O",
                          command=self.open_file)
        filem.add_separator()
        filem.add_command(label="Export Selection as WAV…", accelerator=f"{MOD_LABEL}+E",
                          command=self.export)
        if not IS_MAC:
            filem.add_separator()
            filem.add_command(label="Quit", command=self.on_close)
        menubar.add_cascade(label="File", menu=filem)

        editm = tk.Menu(menubar, tearoff=0)
        editm.add_command(label="Cut", accelerator=f"{MOD_LABEL}+X",
                          command=lambda: self._edit_action("cut"))
        editm.add_command(label="Copy", accelerator=f"{MOD_LABEL}+C",
                          command=lambda: self._edit_action("copy"))
        editm.add_command(label="Paste", accelerator=f"{MOD_LABEL}+V",
                          command=lambda: self._edit_action("paste"))
        editm.add_command(label="Select All", accelerator=f"{MOD_LABEL}+A",
                          command=lambda: self._edit_action("selectall"))
        editm.add_separator()
        editm.add_command(label="Paste Link & Load", command=self.paste_and_load)
        menubar.add_cascade(label="Edit", menu=editm)

        viewm = tk.Menu(menubar, tearoff=0)
        viewm.add_command(label="Zoom In", accelerator="+", command=lambda: self.zoom(0.5))
        viewm.add_command(label="Zoom Out", accelerator="−", command=lambda: self.zoom(2.0))
        viewm.add_command(label="Zoom to Selection", accelerator="Z", command=self.zoom_selection)
        viewm.add_command(label="Show Whole Video", accelerator="0", command=self.show_all)
        menubar.add_cascade(label="View", menu=viewm)

        helpm = tk.Menu(menubar, tearoff=0)
        helpm.add_command(label="How to use", command=self.show_help)
        menubar.add_cascade(label="Help", menu=helpm)
        self.root.config(menu=menubar)
        if IS_MAC:
            try:
                self.root.createcommand("tk::mac::Quit", self.on_close)
                self.root.createcommand("tk::mac::ShowHelp", self.show_help)
            except tk.TclError:
                pass

    def _bind_keys(self):
        r = self.root
        r.bind_all(f"<{MOD}-e>", lambda e: self.export())
        r.bind_all(f"<{MOD}-o>", lambda e: self.open_file())
        keys = {
            "<space>": self.toggle_play,
            "<plus>": lambda: self.zoom(0.5),
            "<equal>": lambda: self.zoom(0.5),
            "<minus>": lambda: self.zoom(2.0),
            "<Key-0>": self.show_all,
            "<z>": self.zoom_selection,
            "<a>": self.select_all,
            "<Escape>": self.clear_selection,
            "<bracketleft>": self.set_start_at_cursor,
            "<bracketright>": self.set_end_at_cursor,
            "<i>": self.set_start_at_cursor,
            "<o>": self.set_end_at_cursor,
            "<Left>": lambda: self.pan(-0.1),
            "<Right>": lambda: self.pan(0.1),
        }
        for seq, fn in keys.items():
            r.bind(seq, lambda e, fn=fn: self._key(e, fn))

        # Clipboard shortcuts + right-click menu for every text field. Done
        # explicitly because Tk's defaults are unreliable on some macOS
        # setups (e.g. non-US keyboard layouts) and Tk has no context menu.
        self._ctx_menu = tk.Menu(self.root, tearoff=0)
        self._ctx_menu.add_command(label="Cut", command=lambda: self._edit_action("cut"))
        self._ctx_menu.add_command(label="Copy", command=lambda: self._edit_action("copy"))
        self._ctx_menu.add_command(label="Paste", command=lambda: self._edit_action("paste"))
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Select All",
                                   command=lambda: self._edit_action("selectall"))
        for ent in (self.url_entry, self.start_entry, self.end_entry):
            for key, action in (("v", "paste"), ("c", "copy"), ("x", "cut"), ("a", "selectall")):
                for k in (key, key.upper()):
                    ent.bind(f"<{MOD}-{k}>",
                             lambda e, a=action, w=ent: self._edit_action(a, w) or "break")
            right_clicks = ["<Button-3>"]
            if IS_MAC:
                right_clicks += ["<Button-2>", "<Control-Button-1>"]
            for seq in right_clicks:
                ent.bind(seq, self._show_context_menu)

    # Modifier bits that mean "this is a shortcut, not a plain key".
    _SHORTCUT_MASK = (0x4 | 0x8 | 0x10) if IS_MAC else (0x4 | 0x8)

    def _key(self, event, fn):
        if event.state & self._SHORTCUT_MASK:
            return None  # Cmd/Ctrl/Option combos are handled elsewhere
        # Don't steal keys while typing in a text field.
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            if event.keysym == "Escape":
                self.root.focus_set()
            return None
        fn()
        return "break"

    # ---------------------------------------------------------- clipboard
    def _clipboard_text(self) -> str:
        try:
            return self.root.clipboard_get()
        except tk.TclError:
            pass
        if IS_MAC:  # fall back to the system pasteboard
            try:
                import subprocess
                return subprocess.run(["/usr/bin/pbpaste"], capture_output=True,
                                      text=True, timeout=2).stdout
            except Exception:  # noqa: BLE001
                pass
        return ""

    def _edit_action(self, action: str, widget=None):
        """Cut / copy / paste / select-all on a text field.

        Paste is debounced so a menu shortcut and a key binding firing for
        the same key press only paste once.
        """
        w = widget or self.root.focus_get()
        if not isinstance(w, (tk.Entry, ttk.Entry)):
            if action == "paste":  # nothing focused: paste into the URL box
                w = self.url_entry
            else:
                return None
        has_sel = bool(w.selection_present())
        if action == "selectall":
            w.selection_range(0, "end")
            w.icursor("end")
        elif action in ("copy", "cut") and has_sel:
            text = w.selection_get()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            if action == "cut":
                w.delete("sel.first", "sel.last")
        elif action == "paste":
            now = time.monotonic()
            if now - getattr(self, "_last_paste", 0.0) < 0.25:
                return None
            self._last_paste = now
            text = self._clipboard_text()
            if w is self.url_entry:
                text = text.strip()
            text = text.replace("\r", " ").replace("\n", " ")
            if not text:
                self.root.bell()
                return None
            if has_sel:
                w.delete("sel.first", "sel.last")
            w.insert("insert", text)
            w.focus_set()
        return None

    def _show_context_menu(self, event):
        event.widget.focus_set()
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()
        return "break"

    def paste_and_load(self):
        if self.busy:
            return
        text = self._clipboard_text().strip()
        if not text:
            messagebox.showinfo(APP_NAME, "The clipboard is empty.\n\nCopy a YouTube link "
                                          "first (in your browser: select the address bar, "
                                          "then press Cmd+C).")
            return
        self.url_var.set(text.split()[0])
        self.url_entry.icursor("end")
        self.load_url()

    # -------------------------------------------------------------- helpers
    def set_status(self, msg: str, frac: Optional[float] = None):
        self.status_var.set(msg)
        if frac is None:
            if self.busy:
                if str(self.progress.cget("mode")) != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(15)
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
        else:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(value=max(0.0, min(1.0, frac)))

    def _update_controls(self):
        have = self.audio is not None and not self.busy
        sel = have and self.sel_start is not None
        st = lambda ok: ("!disabled" if ok else "disabled")  # noqa: E731
        self.load_btn.state([st(not self.busy)])
        self.paste_btn.state([st(not self.busy)])
        self.file_btn.state([st(not self.busy)])
        self.cancel_btn.state([st(self.busy)])
        for b in self.zoom_btns + [self.selall_btn, self.play_btn, self.stop_btn,
                                   self.set_start_btn, self.set_end_btn]:
            b.state([st(have)])
        self.zoom_btns[2].state([st(sel)])
        self.clearsel_btn.state([st(sel)])
        self.export_btn.state([st(sel)])

    def _sync_entries(self):
        if self.sel_start is None:
            self.start_var.set("")
            self.end_var.set("")
            self.len_var.set("Length: —")
        else:
            self.start_var.set(core.format_time(self.sel_start))
            self.end_var.set(core.format_time(self.sel_end))
            self.len_var.set("Length: " + core.format_time(self.sel_end - self.sel_start))
        self.cursor_var.set("Cursor: " + core.format_time(self.cursor))
        self._update_controls()

    def _entries_changed(self, _event=None):
        if self.audio is None:
            return
        try:
            s = core.parse_time(self.start_var.get())
            e = core.parse_time(self.end_var.get())
        except ValueError:
            if self.start_var.get().strip() or self.end_var.get().strip():
                self.root.bell()
            self._sync_entries()
            return
        d = self.audio.duration
        s, e = min(max(0.0, s), d), min(max(0.0, e), d)
        if e < s:
            s, e = e, s
        if e - s <= 0:
            self.root.bell()
            self._sync_entries()
            return
        self.set_selection(s, e)
        # Make sure the new selection is visible.
        if s < self.view_start or e > self.view_end:
            self.zoom_selection()

    def set_selection(self, s: Optional[float], e: Optional[float]):
        if s is None or e is None:
            self.sel_start = self.sel_end = None
        else:
            d = self.audio.duration
            s, e = sorted((min(max(0.0, s), d), min(max(0.0, e), d)))
            self.sel_start, self.sel_end = s, e
        self._sync_entries()
        self.request_redraw()

    # coordinate mapping for the zoomed view
    def _t2x(self, t: float) -> float:
        w = self.wv.winfo_width()
        return (t - self.view_start) / (self.view_end - self.view_start) * w

    def _x2t(self, x: float) -> float:
        w = max(1, self.wv.winfo_width())
        t = self.view_start + x / w * (self.view_end - self.view_start)
        return min(max(0.0, t), self.audio.duration if self.audio else 0.0)

    def _ov_t2x(self, t: float) -> float:
        return t / self.audio.duration * self.ov.winfo_width()

    def _ov_x2t(self, x: float) -> float:
        return min(max(0.0, x / max(1, self.ov.winfo_width()) * self.audio.duration),
                   self.audio.duration)

    def _min_span(self) -> float:
        return MIN_VIEW_FRAMES / float(self.audio.rate)

    def set_view(self, start: float, end: float):
        if self.audio is None:
            return
        d = self.audio.duration
        span = max(self._min_span(), min(d, end - start))
        start = min(max(0.0, start), d - span)
        self.view_start, self.view_end = start, start + span
        self.request_redraw()

    def zoom(self, factor: float, anchor: Optional[float] = None):
        if self.audio is None:
            return
        span = self.view_end - self.view_start
        if anchor is None:
            if self.view_start <= self.cursor <= self.view_end:
                anchor = self.cursor
            else:
                anchor = (self.view_start + self.view_end) / 2
        new_span = span * factor
        rel = (anchor - self.view_start) / span if span else 0.5
        start = anchor - rel * new_span
        self.set_view(start, start + new_span)

    def pan(self, frac: float):
        if self.audio is None:
            return
        span = self.view_end - self.view_start
        self.set_view(self.view_start + frac * span, self.view_end + frac * span)

    def show_all(self):
        if self.audio is not None:
            self.set_view(0.0, self.audio.duration)

    def zoom_selection(self):
        if self.audio is None or self.sel_start is None:
            return
        span = self.sel_end - self.sel_start
        pad = max(span * 0.05, self._min_span() / 2)
        self.set_view(self.sel_start - pad, self.sel_end + pad)

    def select_all(self):
        if self.audio is not None:
            self.set_selection(0.0, self.audio.duration)

    def clear_selection(self):
        if self.audio is not None:
            self.set_selection(None, None)

    def set_start_at_cursor(self):
        if self.audio is None:
            return
        c = self._current_position()
        end = self.sel_end if (self.sel_end is not None and self.sel_end > c) else self.audio.duration
        self.set_selection(c, end)

    def set_end_at_cursor(self):
        if self.audio is None:
            return
        c = self._current_position()
        start = self.sel_start if (self.sel_start is not None and self.sel_start < c) else 0.0
        self.set_selection(start, c)

    def _current_position(self) -> float:
        """The playhead while playing, otherwise the cursor."""
        if self.player.is_playing():
            return min(self.play_to, self.play_from + (time.monotonic() - self.play_t0))
        return self.cursor

    # ------------------------------------------------------------- drawing
    def request_redraw(self):
        if not self._redraw_pending:
            self._redraw_pending = True
            self.root.after_idle(self._redraw)

    def _redraw(self):
        self._redraw_pending = False
        self._draw_overview()
        self._draw_main()

    def _draw_placeholder(self, c: tk.Canvas, text: str):
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        c.create_text(w / 2, h / 2, text=text, fill=TEXT, font=("TkDefaultFont", 13))

    def _wave_polygons(self, c: tk.Canvas, lo, hi, top: float, h: float,
                       x_sel0: Optional[float], x_sel1: Optional[float]):
        """Draw min/max bars as filled polygons, highlighting the selection."""
        mid = top + h / 2
        amp = h / 2 * 0.95
        n = len(lo)

        def poly(i0, i1, colour):
            if i1 - i0 < 1:
                return
            xs = range(i0, i1)
            upper = []
            lower = []
            for i in xs:
                y_hi = mid - hi[i] * amp
                y_lo = mid - lo[i] * amp
                if y_lo - y_hi < 1:  # keep silent parts visible as a thin line
                    y_hi -= 0.5
                    y_lo += 0.5
                upper += [i, y_hi, i + 1, y_hi]
                lower += [i + 1, y_lo, i, y_lo]
            # reverse lower outline pairwise
            pts = upper + [v for j in range(len(lower) - 2, -1, -2) for v in lower[j:j + 2]]
            c.create_polygon(pts, fill=colour, outline="")

        if x_sel0 is None:
            poly(0, n, WAVE)
            return
        a = int(max(0, min(n, round(x_sel0))))
        b = int(max(0, min(n, round(x_sel1))))
        poly(0, a, WAVE)
        poly(a, b, WAVE_SEL)
        poly(b, n, WAVE)

    def _draw_overview(self):
        c = self.ov
        if self.audio is None:
            self._draw_placeholder(c, "")
            return
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 4 or h < 4:
            return
        d = self.audio.duration
        xs0 = xs1 = None
        if self.sel_start is not None:
            xs0, xs1 = self._ov_t2x(self.sel_start), self._ov_t2x(self.sel_end)
            c.create_rectangle(xs0, 0, max(xs0 + 1, xs1), h, fill=BG_SEL, outline="")
        _, lo, hi = self.audio.peaks(0.0, d, w)
        self._wave_polygons(c, lo, hi, 2, h - 4, xs0, xs1)
        # play cursor
        cx = self._ov_t2x(self.cursor)
        c.create_line(cx, 0, cx, h, fill=CURSOR)
        # view window
        x0, x1 = self._ov_t2x(self.view_start), self._ov_t2x(self.view_end)
        if x1 - x0 < 8:
            m = (x0 + x1) / 2
            x0, x1 = m - 4, m + 4
        c.create_rectangle(x0, 1, x1, h - 1, outline=VIEWBOX, width=2)
        c.create_rectangle(x0 - 1, h / 2 - 8, x0 + 3, h / 2 + 8, fill=VIEWBOX, outline="")
        c.create_rectangle(x1 - 3, h / 2 - 8, x1 + 1, h / 2 + 8, fill=VIEWBOX, outline="")
        c.create_text(4, 3, anchor="nw", text="0:00", fill=TEXT, font=("TkDefaultFont", 9))
        c.create_text(w - 4, 3, anchor="ne", text=core.format_time(d, 0), fill=TEXT,
                      font=("TkDefaultFont", 9))
        self.ov_playhead = c.create_line(-10, 0, -10, h, fill=PLAYHEAD, width=2)

    def _draw_main(self):
        c = self.wv
        if self.audio is None:
            self._draw_placeholder(c, "Copy a YouTube link in your browser, then click  Paste & Load\n"
                                      "(or paste it into the URL box with ⌘V / right-click → Paste,\n"
                                      "or use Open File… for a local audio/video file)")
            self.view_info_var.set("")
            return
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 4 or h < RULER_H + 10:
            return
        vs, ve = self.view_start, self.view_end
        span = ve - vs
        top = RULER_H
        wh = h - RULER_H

        # selection background
        xs0 = xs1 = None
        if self.sel_start is not None:
            xs0, xs1 = self._t2x(self.sel_start), self._t2x(self.sel_end)
            c.create_rectangle(max(-2, xs0), top, min(w + 2, max(xs0 + 1, xs1)), h,
                               fill=BG_SEL, outline="")

        # ruler + grid
        c.create_rectangle(0, 0, w, RULER_H, fill="#101114", outline="")
        step = next((s for s in _NICE_STEPS if s / span * w >= 90), _NICE_STEPS[-1])
        decimals = 3 if step < 0.01 else 2 if step < 0.1 else 1 if step < 1 else 0
        t = (int(vs / step)) * step
        while t <= ve + step:
            x = self._t2x(t)
            if 0 <= x <= w:
                c.create_line(x, top, x, h, fill=GRID)
                c.create_line(x, RULER_H - 6, x, RULER_H, fill=TEXT)
                c.create_text(x + 3, 3, anchor="nw", text=core.format_time(t, decimals),
                              fill=TEXT, font=("TkDefaultFont", 10))
            t += step
        c.create_line(0, top + wh / 2, w, top + wh / 2, fill=CENTER)

        # waveform
        kind, a, b = self.audio.peaks(vs, ve, w)
        if kind == "bars":
            self._wave_polygons(c, a, b, top + 4, wh - 8, xs0, xs1)
        else:
            mid = top + wh / 2
            amp = (wh - 8) / 2 * 0.95
            pts = []
            for x, v in zip(a.tolist(), b.tolist()):
                pts += [x, mid - v * amp]
            if len(pts) >= 4:
                c.create_line(pts, fill=WAVE_SEL, width=1.5)
            if len(pts) // 2 < w / 6:  # show individual samples when very zoomed in
                for x, v in zip(a.tolist(), b.tolist()):
                    y = mid - v * amp
                    c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=WAVE_SEL, outline="")

        # selection edges + handles
        if xs0 is not None:
            for x in (xs0, xs1):
                if -5 <= x <= w + 5:
                    c.create_line(x, top, x, h, fill=EDGE, width=2)
                    c.create_polygon(x - 7, top, x + 7, top, x, top + 10, fill=EDGE, outline="")
                    c.create_polygon(x - 7, h, x + 7, h, x, h - 10, fill=EDGE, outline="")

        # cursor
        cx = self._t2x(self.cursor)
        if 0 <= cx <= w:
            c.create_line(cx, top, cx, h, fill=CURSOR, dash=(4, 3))
        self.playhead_item = c.create_line(-10, top, -10, h, fill=PLAYHEAD, width=2)
        self._update_playhead()

        self.view_info_var.set(f"Showing {core.format_time(vs)} – {core.format_time(ve)}"
                               f"  ({core.format_time(span)} of "
                               f"{core.format_time(self.audio.duration, 0)})")

    # --------------------------------------------------- main view mouse
    def _hit(self, x: float) -> str:
        if self.sel_start is not None:
            x0, x1 = self._t2x(self.sel_start), self._t2x(self.sel_end)
            # When the selection is tiny prefer the closer edge.
            d0, d1 = abs(x - x0), abs(x - x1)
            if min(d0, d1) <= EDGE_GRAB_PX:
                return "start" if d0 < d1 or (d0 == d1 and x < x0) else "end"
            if x0 < x < x1:
                return "inside"
        return "empty"

    def _wv_hover(self, e):
        if self.audio is None:
            return
        hit = self._hit(e.x)
        cur = {"start": "sb_h_double_arrow", "end": "sb_h_double_arrow",
               "inside": "fleur"}.get(hit, "xterm")
        if self.wv.cget("cursor") != cur:
            self.wv.configure(cursor=cur)

    def _wv_press(self, e):
        if self.audio is None:
            return
        self.wv.focus_set()
        hit = self._hit(e.x)
        t = self._x2t(e.x)
        self._drag = {"mode": hit, "x0": e.x, "t0": t, "moved": False,
                      "sel": (self.sel_start, self.sel_end)}

    def _wv_motion(self, e):
        d = self._drag
        if d is None or self.audio is None:
            return
        if not d["moved"] and abs(e.x - d["x0"]) < 3:
            return
        d["moved"] = True
        t = self._x2t(e.x)
        mode = d["mode"]
        if mode == "start":
            if t > self.sel_end:
                d["mode"] = "end"
                self.set_selection(self.sel_end, t)
            else:
                self.set_selection(t, self.sel_end)
        elif mode == "end":
            if t < self.sel_start:
                d["mode"] = "start"
                self.set_selection(t, self.sel_start)
            else:
                self.set_selection(self.sel_start, t)
        elif mode == "inside":
            s0, e0 = d["sel"]
            delta = t - d["t0"]
            delta = max(-s0, min(self.audio.duration - e0, delta))
            self.set_selection(s0 + delta, e0 + delta)
        else:  # new selection
            self.set_selection(min(d["t0"], t), max(d["t0"], t))

    def _wv_release(self, e):
        d = self._drag
        self._drag = None
        if d is None or self.audio is None:
            return
        if not d["moved"]:
            self.cursor = d["t0"]
            self._sync_entries()
            self.request_redraw()
        elif self.sel_start is not None and self.sel_end - self.sel_start <= 0:
            self.set_selection(None, None)

    # ----------------------------------------------------- overview mouse
    def _ov_hit(self, x: float) -> str:
        x0, x1 = self._ov_t2x(self.view_start), self._ov_t2x(self.view_end)
        if x1 - x0 >= 2 * EDGE_GRAB_PX + 4:
            if abs(x - x0) <= EDGE_GRAB_PX:
                return "left"
            if abs(x - x1) <= EDGE_GRAB_PX:
                return "right"
        else:
            if x0 - EDGE_GRAB_PX <= x < x0 - 2:
                return "left"
            if x1 + 2 < x <= x1 + EDGE_GRAB_PX:
                return "right"
        if x0 - 4 <= x <= x1 + 4:
            return "move"
        return "outside"

    def _ov_hover(self, e):
        if self.audio is None:
            return
        hit = self._ov_hit(e.x)
        cur = {"left": "sb_h_double_arrow", "right": "sb_h_double_arrow",
               "move": "fleur"}.get(hit, "hand2")
        if self.ov.cget("cursor") != cur:
            self.ov.configure(cursor=cur)

    def _ov_press(self, e):
        if self.audio is None:
            return
        hit = self._ov_hit(e.x)
        t = self._ov_x2t(e.x)
        if hit == "outside":
            span = self.view_end - self.view_start
            self.set_view(t - span / 2, t + span / 2)
            hit = "move"
        self._ov_drag = {"mode": hit, "t0": t, "view": (self.view_start, self.view_end)}

    def _ov_motion(self, e):
        d = self._ov_drag
        if d is None or self.audio is None:
            return
        t = self._ov_x2t(e.x)
        vs, ve = d["view"]
        dur = self.audio.duration
        ms = self._min_span()
        if d["mode"] == "move":
            delta = t - d["t0"]
            delta = max(-vs, min(dur - ve, delta))
            self.set_view(vs + delta, ve + delta)
        elif d["mode"] == "left":
            self.set_view(min(t, ve - ms), ve)
        elif d["mode"] == "right":
            self.set_view(vs, max(t, vs + ms))

    # ---------------------------------------------------------- scrolling
    @staticmethod
    def _wheel_units(e) -> float:
        if IS_MAC:
            return float(e.delta)
        return e.delta / 120.0

    def _wheel(self, e, units: Optional[float] = None):
        if self.audio is None:
            return
        u = self._wheel_units(e) if units is None else units
        if u == 0:
            return
        if e.widget is self.wv:
            anchor = self._x2t(e.x)
        else:
            anchor = self._ov_x2t(e.x)
        self.zoom(0.85 ** u, anchor)

    def _hwheel(self, e, units: Optional[float] = None):
        if self.audio is None:
            return
        u = self._wheel_units(e) if units is None else units
        self.pan(-0.05 * u)

    # ------------------------------------------------------------ loading
    def load_url(self):
        if self.busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo(APP_NAME, "Paste a YouTube video link into the URL box first.")
            self.url_entry.focus_set()
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        self._start_job(self._job_url, url)

    def open_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Open audio or video file",
            filetypes=[("Audio / video", "*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.opus "
                                         "*.webm *.mp4 *.mov *.mkv *.aiff *.aif"),
                       ("All files", "*.*")])
        if path:
            self._start_job(self._job_file, path)

    def cancel_load(self):
        self.cancel_flag.set()
        self.set_status("Cancelling…")

    def _start_job(self, fn, arg):
        self.stop()
        self.busy = True
        self.cancel_flag.clear()
        self._update_controls()
        self.set_status("Starting…")
        new_dir = os.path.join(self.workdir, "load_%d" % int(time.time() * 1000))
        os.makedirs(new_dir, exist_ok=True)
        threading.Thread(target=self._run_job, args=(fn, arg, new_dir), daemon=True).start()

    def _run_job(self, fn, arg, load_dir):
        try:
            audio = fn(arg, load_dir)
            self.queue.put(("loaded", audio, load_dir))
        except core.Cancelled:
            self.queue.put(("cancelled", load_dir))
        except Exception as exc:  # noqa: BLE001
            if self.cancel_flag.is_set():
                self.queue.put(("cancelled", load_dir))
            else:
                self.queue.put(("error", _friendly_error(exc), traceback.format_exc(), load_dir))

    def _progress(self, msg, frac):
        self.queue.put(("progress", msg, frac))

    def _job_url(self, url, load_dir):
        path, info = core.download_audio(url, load_dir, self._progress,
                                         self.cancel_flag.is_set)
        if self.cancel_flag.is_set():
            raise core.Cancelled()
        title = info.get("title") or ""
        return core.decode(path, load_dir, title=title, progress=self._progress)

    def _job_file(self, path, load_dir):
        return core.decode(path, load_dir, progress=self._progress)

    def _poll_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    self.set_status(item[1], item[2])
                elif kind == "loaded":
                    self._on_loaded(item[1], item[2])
                elif kind == "cancelled":
                    self.busy = False
                    _rmtree(item[1])
                    self.set_status("Cancelled.")
                    self._update_controls()
                elif kind == "error":
                    self.busy = False
                    _rmtree(item[3])
                    self.set_status("Failed: " + item[1].splitlines()[0][:150])
                    self._update_controls()
                    messagebox.showerror(APP_NAME, item[1])
                elif kind == "exported":
                    self.busy = False
                    self._update_controls()
                    self.set_status(f"Exported {os.path.basename(item[1])}")
                elif kind == "export_error":
                    self.busy = False
                    self._update_controls()
                    self.set_status("Export failed.")
                    messagebox.showerror(APP_NAME, "Export failed:\n\n" + item[1])
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _on_loaded(self, audio: core.AudioData, load_dir: str):
        old_dir = self.load_dir
        self.audio = audio
        self.load_dir = load_dir
        if old_dir and old_dir != load_dir:
            _rmtree(old_dir)
        self.busy = False
        self.title_var.set(f"{audio.title}   ·   {core.format_time(audio.duration, 0)}   ·   "
                           f"{audio.rate} Hz {'stereo' if audio.channels == 2 else 'mono'}")
        self.cursor = 0.0
        self.sel_start = self.sel_end = None
        self.view_start, self.view_end = 0.0, audio.duration
        self._sync_entries()
        self.set_status("Ready — drag on the waveform to choose the part to rip.")
        self.request_redraw()

    # ----------------------------------------------------------- playback
    def toggle_play(self):
        if self.player.is_playing():
            self.stop()
        else:
            self.play()

    def play(self):
        if self.audio is None or self.busy:
            return
        if self.sel_start is not None:
            start, end = self.sel_start, self.sel_end
            if self.sel_start < self.cursor < self.sel_end:
                start = self.cursor
        else:
            start = self.cursor
            end = min(self.audio.duration, start + PLAY_NOSEL_LIMIT)
        if end - start <= 0.01:
            start = self.sel_start if self.sel_start is not None else 0.0
        try:
            self.player.play(self.audio, start, end)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Could not play audio:\n\n{exc}")
            return
        self.play_from, self.play_to = start, end
        self.play_t0 = time.monotonic()
        self.play_btn.configure(text="❚❚ Pause (Space)")
        self._tick()

    def stop(self):
        was_playing = self.player.is_playing()
        if was_playing:
            # leave the cursor where playback stopped
            self.cursor = self._current_position()
        self.player.stop()
        self.play_btn.configure(text="▶ Play (Space)")
        if was_playing:
            self._sync_entries()
            self.request_redraw()

    def _tick(self):
        if self.player.is_playing():
            self._update_playhead()
            self.root.after(30, self._tick)
        else:
            self.play_btn.configure(text="▶ Play (Space)")
            self._update_playhead(hide=True)

    def _update_playhead(self, hide: bool = False):
        if self.audio is None:
            return
        playing = self.player.is_playing() and not hide
        pos = self._current_position() if playing else -1
        h = self.wv.winfo_height()
        if self.playhead_item is not None:
            x = self._t2x(pos) if playing else -10
            self.wv.coords(self.playhead_item, x, RULER_H, x, h)
        if getattr(self, "ov_playhead", None) is not None:
            x = self._ov_t2x(pos) if playing else -10
            self.ov.coords(self.ov_playhead, x, 0, x, self.ov.winfo_height())

    # ------------------------------------------------------------- export
    def export(self):
        if self.audio is None or self.busy:
            return
        if self.sel_start is None:
            messagebox.showinfo(APP_NAME, "Select the part you want to rip first:\n"
                                          "drag across the waveform, or type Start/End times.")
            return
        self._entries_changed()
        s, e = self.sel_start, self.sel_end

        def stamp(t):
            return core.format_time(t, 1).replace(":", "m", 1).replace(":", "m") + "s"

        default = core.safe_filename(f"{self.audio.title} [{stamp(s)}-{stamp(e)}]") + ".wav"
        path = filedialog.asksaveasfilename(
            title="Export WAV", defaultextension=".wav", initialfile=default,
            filetypes=[("WAV audio", "*.wav")])
        if not path:
            return
        if not path.lower().endswith(".wav"):
            path += ".wav"
        rate = None if self.rate_var.get() == "Original" else int(self.rate_var.get())
        ch = {"Original": None, "Stereo": 2, "Mono": 1}[self.ch_var.get()]
        fade = 10.0 if self.fade_var.get() else 0.0
        audio = self.audio
        self.busy = True
        self._update_controls()
        self.set_status("Exporting WAV…")

        def work():
            try:
                core.export_wav(audio, s, e, path, sample_rate=rate, channels=ch, fade_ms=fade)
                self.queue.put(("exported", path))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("export_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # --------------------------------------------------------------- misc
    def show_help(self):
        messagebox.showinfo(APP_NAME, (
            "1. Copy a YouTube link in your browser and click Paste & Load. You can also "
            "paste into the URL box (⌘V, right-click → Paste, or Edit → Paste) and press "
            "Load, or open a local file.\n\n"
            "2. The top strip shows the whole video. The yellow window marks the part "
            "shown in the big zoomed view. Drag the window to move it; drag its edges to "
            "expand it to the whole video or narrow it down to a tiny section.\n\n"
            "3. In the zoomed view, drag to select the part to rip. Drag the white edges "
            "to fine-tune, or drag inside the selection to move it. Click to set the "
            "cursor. Scroll to zoom, Shift+scroll or swipe sideways to pan.\n\n"
            "4. Press Space to preview, then Export WAV.\n\n"
            "Keys: Space play/stop · + / − zoom · Z zoom to selection · 0 whole video · "
            "[ and ] set start/end at cursor · A select all · Esc clear · "
            f"{MOD_LABEL}+E export"))

    def on_close(self):
        try:
            self.cancel_flag.set()
            self.player.stop()
        finally:
            self.audio = None
            _rmtree(self.workdir)
            self.root.destroy()


def _rmtree(path: Optional[str]):
    if path:
        shutil.rmtree(path, ignore_errors=True)


def _friendly_error(exc: Exception) -> str:
    import re
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r";?\s*please report this issue on.*$", "", msg, flags=re.S | re.I)
    msg = re.sub(r"\s*\(caused by .*$", "", msg, flags=re.S)
    if len(msg) > 400:
        msg = msg[:400] + "…"
    low = msg.lower()
    hint = ""
    if "certificate" in low or "ssl" in low:
        hint = ("\n\nA secure connection could not be made. Check your internet "
                "connection, VPN or firewall.")
    elif "sign in to confirm" in low or "not a bot" in low:
        hint = ("\n\nYouTube is asking for a sign-in / bot check for this video. "
                "Try again later or try a different video.")
    elif "private video" in low or "unavailable" in low:
        hint = "\n\nThe video is private, removed or unavailable in your region."
    elif "unsupported url" in low:
        hint = "\n\nThat doesn't look like a supported video link."
    elif "urlopen error" in low or "timed out" in low or "name resolution" in low:
        hint = "\n\nCheck your internet connection."
    return (msg or exc.__class__.__name__) + hint


def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "appname", APP_NAME)
    except tk.TclError:
        pass
    app = AudioRipperApp(root)

    def _activate():
        # Make sure the window gets keyboard focus when launched from Finder.
        root.lift()
        root.focus_force()
        app.url_entry.focus_set()
    root.after(150, _activate)
    args = [a for a in sys.argv[1:] if not a.startswith("-psn")]
    if args:
        target = args[0]
        if os.path.isfile(target):
            root.after(200, lambda: app._start_job(app._job_file, target))
        else:
            app.url_var.set(target)
            root.after(200, app.load_url)
    if IS_MAC:
        # Files dropped on the Dock icon / "Open With".
        def open_doc(*paths):
            if paths and not app.busy:
                app._start_job(app._job_file, paths[0])
        try:
            root.createcommand("::tk::mac::OpenDocument", open_doc)
        except tk.TclError:
            pass
    root.mainloop()


if __name__ == "__main__":
    main()

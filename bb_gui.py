#!/usr/bin/env python3
"""
Discord-подобный GUI для нативного клиента «Белой Берёзки».

Лёгкий Tkinter (системный python3-tk, без pip-зависимостей сверх движка).
Движок (aiortc + socket.io) крутится в фоновом потоке со своим asyncio-loop'ом;
GUI общается с ним через потокобезопасную очередь событий.

Запуск:
    python3 bb_gui.py
или через ./run.sh (поставит зависимости и стартует это окно).
"""

from __future__ import annotations

import queue
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk

from bb_native import (
    Engine,
    MODE_PRESETS,
    apply_mode_defaults,
    build_arg_parser,
    setup_sink,
)

# ── Палитра в духе Discord ────────────────────────────────────────────────
C = {
    "bg": "#313338", "panel": "#2b2d31", "dark": "#1e1f22", "card": "#383a40",
    "text": "#f2f3f5", "muted": "#b5bac1", "accent": "#5865f2", "accent_hi": "#4752c4",
    "green": "#23a55a", "green_hi": "#1a8546", "red": "#da373c", "red_hi": "#a12828",
    "field": "#1e1f22", "online": "#23a55a", "off": "#80848e",
}
FONT = ("DejaVu Sans", 11)
FONT_SM = ("DejaVu Sans", 9)
FONT_BIG = ("DejaVu Sans", 16, "bold")
FONT_MONO = ("DejaVu Sans Mono", 8)


def _btn(parent, text, cmd, bg, hi, fg="#ffffff", font=FONT, **kw):
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, font=font,
                  activebackground=hi, activeforeground=fg, relief="flat", bd=0,
                  cursor="hand2", highlightthickness=0, padx=14, pady=9, **kw)
    b.bind("<Enter>", lambda e: b.config(bg=hi))
    b.bind("<Leave>", lambda e: b.config(bg=b._restbg))
    b._restbg = bg
    return b


def _entry(parent, show=None):
    e = tk.Entry(parent, bg=C["field"], fg=C["text"], font=FONT, relief="flat",
                 insertbackground=C["text"], highlightthickness=1,
                 highlightbackground=C["dark"], highlightcolor=C["accent"], show=show)
    return e


def _combo(parent, values, initial=""):
    style = ttk.Style(parent)
    style.theme_use("default")
    style.configure("Dark.TCombobox",
                    fieldbackground=C["field"], background=C["card"],
                    foreground=C["text"], selectbackground=C["accent"],
                    selectforeground=C["text"], arrowcolor=C["muted"],
                    insertcolor=C["text"])
    style.map("Dark.TCombobox",
              fieldbackground=[("readonly", C["field"])],
              foreground=[("readonly", C["text"])],
              selectbackground=[("readonly", C["accent"])])
    c = ttk.Combobox(parent, values=values, style="Dark.TCombobox", font=FONT_SM)
    c.set(initial if initial else (values[0] if values else ""))
    return c


def _label(parent, text, fg=None, font=FONT):
    return tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["muted"], font=font,
                    anchor="w")


class App:
    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        self.engine = None
        self.q: "queue.Queue" = queue.Queue()
        self.mic_on = not args.no_mic
        self.mode = tk.StringVar(value=args.mode)

        root.title("Белая Берёзка — нативка")
        root.configure(bg=C["bg"])
        root.geometry("460x640")
        root.minsize(420, 560)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_header()
        self.body = tk.Frame(root, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self._show_connect()

        self.root.after(80, self._poll)

    # ── шапка ─────────────────────────────────────────────────────────────
    def _build_header(self):
        h = tk.Frame(self.root, bg=C["panel"])
        h.pack(fill="x")
        inner = tk.Frame(h, bg=C["panel"])
        inner.pack(fill="x", padx=16, pady=12)
        tk.Label(inner, text="🌳 Белая Берёзка", bg=C["panel"], fg=C["text"],
                 font=FONT_BIG).pack(side="left")
        self.dot = tk.Label(inner, text="●", bg=C["panel"], fg=C["off"], font=("DejaVu Sans", 13))
        self.dot.pack(side="right")
        self.status = tk.Label(inner, text="не в эфире", bg=C["panel"], fg=C["muted"],
                               font=FONT_SM)
        self.status.pack(side="right", padx=(0, 6))

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _get_audio_devices(self):
        sources, sinks = ["default"], [None]
        try:
            r = subprocess.run(["pactl", "list", "short", "sources"],
                               capture_output=True, text=True, timeout=3)
            names = [ln.split("\t")[1] for ln in r.stdout.strip().splitlines()
                     if len(ln.split("\t")) >= 2]
            if names:
                sources = names
        except Exception:
            pass
        try:
            r = subprocess.run(["pactl", "list", "short", "sinks"],
                               capture_output=True, text=True, timeout=3)
            names = [ln.split("\t")[1] for ln in r.stdout.strip().splitlines()
                     if len(ln.split("\t")) >= 2]
            if names:
                sinks = names
        except Exception:
            pass
        return sources, sinks

    # ── экран «подключиться» ──────────────────────────────────────────────
    def _show_connect(self):
        self._clear_body()
        self._set_state("off", "не в эфире")

        def field(label, value, show=None):
            _label(self.body, label).pack(fill="x", pady=(10, 2))
            e = _entry(self.body, show=show)
            e.insert(0, value)
            e.pack(fill="x", ipady=4)
            return e

        self.f_server = field("Сервер", self.args.server)
        self.f_pass = field("Пароль", self.args.password, show="•")
        row = tk.Frame(self.body, bg=C["bg"])
        row.pack(fill="x")
        # канал и имя в один ряд
        lcol = tk.Frame(row, bg=C["bg"]); lcol.pack(side="left", fill="x", expand=True, padx=(0, 6))
        rcol = tk.Frame(row, bg=C["bg"]); rcol.pack(side="left", fill="x", expand=True, padx=(6, 0))
        _label(lcol, "Канал").pack(fill="x", pady=(10, 2))
        self.f_room = _entry(lcol); self.f_room.insert(0, self.args.room); self.f_room.pack(fill="x", ipady=4)
        _label(rcol, "Имя").pack(fill="x", pady=(10, 2))
        self.f_name = _entry(rcol); self.f_name.insert(0, self.args.name); self.f_name.pack(fill="x", ipady=4)

        # аудио-устройства
        sources, sinks = self._get_audio_devices()
        audio_row = tk.Frame(self.body, bg=C["bg"]); audio_row.pack(fill="x")
        mcol = tk.Frame(audio_row, bg=C["bg"]); mcol.pack(side="left", fill="x", expand=True, padx=(0, 6))
        scol = tk.Frame(audio_row, bg=C["bg"]); scol.pack(side="left", fill="x", expand=True, padx=(6, 0))

        _label(mcol, "🎤 Микрофон").pack(fill="x", pady=(10, 2))
        mic_init = self.args.mic_source if self.args.mic_source in sources else (sources[0] if sources else "default")
        self.f_mic = _combo(mcol, sources, mic_init)
        self.f_mic.pack(fill="x", ipady=3)

        _label(scol, "🎧 Наушники / колонки").pack(fill="x", pady=(10, 2))
        sink_init = self.args.play_sink if self.args.play_sink in sinks else (sinks[0] if sinks else "")
        self.f_sink = _combo(scol, sinks, sink_init or "")
        self.f_sink.pack(fill="x", ipady=3)

        # кнопка обновить список устройств
        _btn(self.body, "↻ Обновить список устройств", self._refresh_audio_combos,
             C["card"], "#404249", font=FONT_SM).pack(fill="x", pady=(4, 0))

        # звук системы
        self.sys_on = tk.BooleanVar(value=not self.args.no_system_audio)
        sc = tk.Checkbutton(self.body, text="  Транслировать звук системы (игра/музыка)",
                            variable=self.sys_on, bg=C["bg"], fg=C["text"], font=FONT_SM,
                            selectcolor=C["field"], activebackground=C["bg"],
                            activeforeground=C["text"], anchor="w", bd=0, highlightthickness=0)
        sc.pack(fill="x", pady=(12, 2))
        self.f_sys = _entry(self.body); self.f_sys.insert(0, self.args.system_source)
        self.f_sys.pack(fill="x", ipady=3)

        # режим (сегмент)
        _label(self.body, "Качество демонстрации").pack(fill="x", pady=(14, 4))
        self.seg = tk.Frame(self.body, bg=C["bg"])
        self.seg.pack(fill="x")
        self._render_segment(self.seg, live=False)

        # кнопка входа
        _btn(self.body, "Войти в канал", self._connect, C["green"], C["green_hi"],
             font=("DejaVu Sans", 12, "bold")).pack(fill="x", pady=(18, 6), ipady=2)

        # утилита анти-эхо
        _btn(self.body, "🎚 Анти-эхо", self._do_setup_sink, C["card"], "#404249",
             font=FONT_SM).pack(fill="x")

    def _render_segment(self, parent, live: bool):
        for w in parent.winfo_children():
            w.destroy()
        defs = [("quality", "🎬 Качество", "1080p · 60"), ("fps", "⚡ ФПС", "720p · 60")]
        for key, title, sub in defs:
            active = self.mode.get() == key
            cell = tk.Frame(parent, bg=C["accent"] if active else C["card"], cursor="hand2")
            cell.pack(side="left", expand=True, fill="both", padx=(0 if key == "quality" else 6, 0))
            tk.Label(cell, text=title, bg=cell["bg"], fg="#ffffff" if active else C["text"],
                     font=("DejaVu Sans", 11, "bold")).pack(pady=(8, 0))
            tk.Label(cell, text=sub, bg=cell["bg"], fg="#dbdee1" if active else C["muted"],
                     font=FONT_SM).pack(pady=(0, 8))
            for wdg in (cell, *cell.winfo_children()):
                wdg.bind("<Button-1>", lambda e, k=key, lv=live: self._pick_mode(k, lv))

    def _refresh_audio_combos(self):
        sources, sinks = self._get_audio_devices()
        if hasattr(self, "f_mic") and self.f_mic.winfo_exists():
            cur_mic = self.f_mic.get()
            self.f_mic["values"] = sources
            if cur_mic not in sources and sources:
                self.f_mic.set(sources[0])
        if hasattr(self, "f_sink") and self.f_sink.winfo_exists():
            cur_sink = self.f_sink.get()
            self.f_sink["values"] = sinks
            if cur_sink not in sinks and sinks:
                self.f_sink.set(sinks[0])

    def _pick_mode(self, key, live):
        if self.mode.get() == key:
            return
        self.mode.set(key)
        if live and self.engine:
            self.engine.set_mode(key)
        # перерисовать активный сегмент
        parent = self.seg_live if (live and hasattr(self, "seg_live")) else self.seg
        self._render_segment(parent, live=live)

    # ── экран «в канале» ──────────────────────────────────────────────────
    def _show_call(self):
        self._clear_body()

        tk.Label(self.body, text="В ЭФИРЕ", bg=C["bg"], fg=C["muted"],
                 font=("DejaVu Sans", 9, "bold")).pack(fill="x", pady=(8, 4))
        listwrap = tk.Frame(self.body, bg=C["dark"])
        listwrap.pack(fill="both", expand=False, ipady=2)
        self.peers_box = tk.Listbox(listwrap, bg=C["dark"], fg=C["text"], font=FONT,
                                    relief="flat", bd=0, highlightthickness=0,
                                    selectbackground=C["card"], height=6, activestyle="none")
        self.peers_box.pack(fill="both", expand=True, padx=6, pady=6)
        self.peers_box.insert("end", "  …подключаюсь")

        # переключатель режима (живой)
        _label(self.body, "Качество демонстрации (можно менять на лету)").pack(fill="x", pady=(14, 4))
        self.seg_live = tk.Frame(self.body, bg=C["bg"]); self.seg_live.pack(fill="x")
        self._render_segment(self.seg_live, live=True)

        # кнопки управления
        ctl = tk.Frame(self.body, bg=C["bg"]); ctl.pack(fill="x", pady=(16, 0))
        self.mic_btn = _btn(ctl, "", self._toggle_mic, C["card"], "#404249")
        self.mic_btn.pack(side="left", expand=True, fill="x", padx=(0, 6), ipady=2)
        self._refresh_mic_btn()
        _btn(ctl, "⏹ Выйти", self._disconnect, C["red"], C["red_hi"]).pack(
            side="left", expand=True, fill="x", padx=(6, 0), ipady=2)

        # лог
        _label(self.body, "Журнал").pack(fill="x", pady=(16, 2))
        logwrap = tk.Frame(self.body, bg=C["dark"]); logwrap.pack(fill="both", expand=True)
        self.log = tk.Text(logwrap, bg=C["dark"], fg=C["muted"], font=FONT_MONO,
                           relief="flat", bd=0, highlightthickness=0, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

    def _refresh_mic_btn(self):
        if not hasattr(self, "mic_btn"):
            return
        if self.mic_on:
            self.mic_btn.config(text="🎤 Микрофон вкл", bg=C["green"])
            self.mic_btn._restbg = C["green"]
        else:
            self.mic_btn.config(text="🔇 Выключен", bg=C["red"])
            self.mic_btn._restbg = C["red"]

    # ── действия ──────────────────────────────────────────────────────────
    def _connect(self):
        a = self.args
        a.server = self.f_server.get().strip()
        a.password = self.f_pass.get()
        a.room = self.f_room.get().strip() or "general"
        a.name = self.f_name.get().strip() or "Боец"
        a.mic_source = self.f_mic.get().strip() or "default"
        a.play_sink = self.f_sink.get().strip() or None
        a.system_source = self.f_sys.get().strip() or "@DEFAULT_MONITOR@"
        a.no_system_audio = not self.sys_on.get()
        a.mode = self.mode.get()
        a.width = a.height = a.fps = None
        apply_mode_defaults(a)

        self._show_call()
        self._set_state("connecting", "подключаюсь…")
        self.engine = Engine(a, self._engine_event)
        self.engine.start()

    def _disconnect(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
        self._show_connect()

    def _toggle_mic(self):
        self.mic_on = not self.mic_on
        self._refresh_mic_btn()
        if self.engine:
            self.engine.set_mic(self.mic_on)

    def _do_setup_sink(self):
        if not messagebox.askyesno("Анти-эхо",
                                   "Создать виртуальный приёмник 'bb_stream' для "
                                   "трансляции звука игры без эха?\n\n"
                                   "Потом в pavucontrol переведите звук игры на 'BB_Stream' "
                                   "и укажите источник bb_stream.monitor."):
            return
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                setup_sink()
            except Exception as e:  # noqa: BLE001
                print(f"Ошибка: {e}")
        self.f_sys.delete(0, "end")
        self.f_sys.insert(0, "bb_stream.monitor")
        self._text_popup("Анти-эхо", buf.getvalue())

    def _text_popup(self, title, text):
        top = tk.Toplevel(self.root, bg=C["bg"])
        top.title(title)
        top.geometry("560x420")
        t = tk.Text(top, bg=C["dark"], fg=C["text"], font=FONT_MONO, relief="flat",
                    bd=0, wrap="none", padx=10, pady=10)
        t.pack(fill="both", expand=True)
        t.insert("1.0", text or "(пусто)")
        t.config(state="disabled")

    # ── события движка (из фонового потока → очередь) ─────────────────────
    def _engine_event(self, kind, data):
        self.q.put((kind, data))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _handle(self, kind, data):
        if kind == "log":
            self._append_log(str(data))
        elif kind == "status":
            self.status.config(text=str(data))
        elif kind == "state":
            if data == "connected":
                self._set_state("online", None)
            elif data == "connecting":
                self._set_state("connecting", "подключаюсь…")
            elif data == "disconnected":
                self._set_state("connecting", "переподключение…")
            elif data == "error":
                self._set_state("off", "ошибка")
        elif kind == "peers":
            self._update_peers(data)
        elif kind == "mic":
            self.mic_on = bool(data)
            self._refresh_mic_btn()
        elif kind == "mode":
            self.mode.set(str(data))
            if hasattr(self, "seg_live") and self.seg_live.winfo_exists():
                self._render_segment(self.seg_live, live=True)
        elif kind == "error":
            messagebox.showerror("Не удалось", str(data))
            self._disconnect()

    def _set_state(self, st, status_text):
        colors = {"online": C["online"], "connecting": "#f0b232", "off": C["off"]}
        self.dot.config(fg=colors.get(st, C["off"]))
        if status_text is not None:
            self.status.config(text=status_text)

    def _update_peers(self, names):
        if not hasattr(self, "peers_box") or not self.peers_box.winfo_exists():
            return
        self.peers_box.delete(0, "end")
        if not names:
            self.peers_box.insert("end", "  пока никого — ждём коллег")
        else:
            for n in names:
                self.peers_box.insert("end", f"  🔊  {n}")
        self.status.config(text=f"В канале «{self.args.room}» · {len(names)} в эфире")

    def _append_log(self, msg):
        if not hasattr(self, "log") or not self.log.winfo_exists():
            return
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        # не даём логу пухнуть бесконечно
        if int(self.log.index("end-1c").split(".")[0]) > 400:
            self.log.delete("1.0", "100.0")
        self.log.config(state="disabled")

    def _on_close(self):
        if self.engine:
            try:
                self.engine.stop()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()


def main():
    # переиспользуем парсер движка → можно префилить поля из флагов/окружения
    ap = build_arg_parser()
    args, _ = ap.parse_known_args()
    root = tk.Tk()
    # тёмная тема для стандартных диалогов по возможности
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:  # noqa: BLE001
        pass
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from processor import process_video


class VideoProjectApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("معالجة فيديو محلية — المرحلة الأولى")
        self.root.geometry("820x570")
        self.root.minsize(680, 450)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.video_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.status_text = tk.StringVar(value="اختر ملف فيديو للبدء.")
        self.is_running = False

        self._build_ui()
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="ملف الفيديو",
            font=("", 10, "bold"),
        ).pack(anchor="w")

        video_row = ttk.Frame(frame)
        video_row.pack(fill="x", pady=(5, 12))

        self.video_entry = ttk.Entry(video_row, textvariable=self.video_path)
        self.video_entry.pack(side="left", fill="x", expand=True)

        self.browse_video_button = ttk.Button(
            video_row, text="اختيار فيديو...", command=self._choose_video
        )
        self.browse_video_button.pack(side="left", padx=(8, 0))

        ttk.Label(
            frame,
            text="مجلد حفظ المشاريع (اختياري؛ افتراضيًا بجانب الفيديو)",
            font=("", 10, "bold"),
        ).pack(anchor="w")

        output_row = ttk.Frame(frame)
        output_row.pack(fill="x", pady=(5, 12))

        self.output_entry = ttk.Entry(output_row, textvariable=self.output_path)
        self.output_entry.pack(side="left", fill="x", expand=True)

        self.browse_output_button = ttk.Button(
            output_row, text="اختيار مجلد...", command=self._choose_output
        )
        self.browse_output_button.pack(side="left", padx=(8, 0))

        self.start_button = ttk.Button(
            frame, text="بدء المعالجة", command=self._start
        )
        self.start_button.pack(anchor="w", pady=(0, 10))

        self.progress = ttk.Progressbar(
            frame, orient="horizontal", mode="determinate", maximum=100
        )
        self.progress.pack(fill="x", pady=(0, 6))

        ttk.Label(frame, textvariable=self.status_text).pack(anchor="w", pady=(0, 6))

        ttk.Label(frame, text="سجل العملية").pack(anchor="w")
        log_frame = ttk.Frame(frame)
        log_frame.pack(fill="both", expand=True, pady=(5, 0))

        self.log = tk.Text(log_frame, wrap="word", height=14, state="disabled")
        scrollbar = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log.yview
        )
        self.log.configure(yscrollcommand=scrollbar.set)

        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _choose_video(self) -> None:
        path = filedialog.askopenfilename(
            title="اختر ملف فيديو",
            filetypes=[
                (
                    "ملفات فيديو",
                    "*.mp4 *.mkv *.mov *.avi *.wmv *.webm *.m4v *.mpeg *.mpg",
                ),
                ("كل الملفات", "*.*"),
            ],
        )
        if path:
            self.video_path.set(path)
            self.status_text.set("جاهز للمعالجة.")

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="اختر مجلد حفظ المشاريع")
        if path:
            self.output_path.set(path)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self) -> None:
        if self.is_running:
            return

        source = self.video_path.get().strip()
        if not source:
            messagebox.showwarning("لا يوجد ملف", "اختر ملف فيديو أولًا.")
            return

        source_path = Path(source)
        if not source_path.is_file():
            messagebox.showerror("ملف غير موجود", "المسار المحدد لا يشير إلى ملف.")
            return

        output_base = self.output_path.get().strip()
        if not output_base:
            output_base = str(source_path.parent)

        self.is_running = True
        self.start_button.configure(state="disabled")
        self.browse_video_button.configure(state="disabled")
        self.browse_output_button.configure(state="disabled")
        self.progress["value"] = 0
        self.status_text.set("جارٍ العمل...")
        self._append_log("— بدء عملية جديدة —")

        thread = threading.Thread(
            target=self._worker,
            args=(source, output_base),
            daemon=True,
        )
        thread.start()

    def _worker(self, source: str, output_base: str) -> None:
        def callback(kind: str, value: object) -> None:
            self.events.put((kind, value))

        try:
            project_dir = process_video(source, output_base, callback)
            self.events.put(("success", str(project_dir)))
        except Exception as exc:
            self.events.put(("failure", str(exc)))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()

                if kind == "log":
                    self._append_log(str(value))
                elif kind == "progress":
                    number = max(0, min(100, float(value)))
                    self.progress["value"] = number
                    self.status_text.set(f"التقدم: {number:.0f}%")
                elif kind == "success":
                    self.is_running = False
                    self._set_controls_enabled(True)
                    self.status_text.set("اكتملت المعالجة بنجاح.")
                    self._append_log(f"مجلد النتائج: {value}")
                    messagebox.showinfo(
                        "اكتملت المعالجة",
                        f"تم إنشاء المشروع في:\n{value}",
                    )
                elif kind == "failure":
                    self.is_running = False
                    self._set_controls_enabled(True)
                    self.status_text.set("انتهت العملية مع خطأ.")
                    self._append_log(f"خطأ: {value}")
                    messagebox.showerror("تعذر إكمال المعالجة", str(value))
        except queue.Empty:
            pass

        self.root.after(100, self._poll_events)

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.start_button.configure(state=state)
        self.browse_video_button.configure(state=state)
        self.browse_output_button.configure(state=state)


def main() -> None:
    root = tk.Tk()
    VideoProjectApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

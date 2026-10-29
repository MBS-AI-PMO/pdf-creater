"""Image to PDF Converter.

Pick a folder and convert the images inside it (and its subfolders) to PDF:
  - One PDF per folder: all images in a folder are combined into one PDF named
    after the folder and saved inside it. Each subfolder gets its own PDF.
  - One PDF per image: each image gets a PDF with the same name, saved next to it.
"""

import io
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import img2pdf
import pikepdf
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

APP_TITLE = "Image to PDF Converter"
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp",
    ".gif", ".tif", ".tiff", ".webp",
}
# Images converted in parallel. Measured: 8 threads is ~1.6x faster than 4 on
# TIFF/PNG; more than 8 adds almost nothing.
WORKERS = min(8, os.cpu_count() or 1)
# Folder PDFs hold every image of a folder in memory at once, so build at most
# two folder PDFs at a time (each one still converts its images on all WORKERS).
FOLDER_WORKERS = 2
# img2pdf re-compresses TIFF/PNG pixels with zlib. Level 4 is ~4x faster than the
# default 6 for ~9% larger output; it is lossless either way.
ZLIB_LEVEL = 4

MODE_FOLDER = "folder"
MODE_IMAGE = "image"

# Progress panel colours
TROUGH = "#e3e6ea"
BAR_RUNNING = "#1a73e8"
BAR_SHINE = "#6aa5f5"
BAR_DONE = "#2e7d32"
BAR_PROBLEMS = "#f9a825"
BAR_STOPPED = "#9e9e9e"
FRAME_MS = 30

# Large scans can trip Pillow's decompression-bomb guard; these are trusted local files.
Image.MAX_IMAGE_PIXELS = None
logging.getLogger("img2pdf").setLevel(logging.ERROR)


class _FasterZlib:
    """Stands in for img2pdf's zlib module so it compresses at ZLIB_LEVEL."""

    def __getattr__(self, name):
        return getattr(zlib, name)

    @staticmethod
    def compress(data, level=ZLIB_LEVEL):
        return zlib.compress(data, level)


img2pdf.zlib = _FasterZlib()


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #

def natural_key(path: Path) -> list:
    """Sort so that page2.jpg comes before page10.jpg."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path))]


def find_images(root: Path, recursive: bool) -> list[Path]:
    pattern = root.rglob("*") if recursive else root.glob("*")
    images = [p for p in pattern if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    return sorted(images, key=natural_key)


def plan_per_image(images: list[Path]) -> list[tuple[list[Path], Path]]:
    """Each image -> PDF with the same name in the same folder.

    If two images share a name in one folder (scan.jpg and scan.png), the first
    gets scan.pdf and the others get "scan (png).pdf" so nothing is overwritten.
    """
    taken: set[str] = set()
    plan = []
    for img in images:
        pdf = img.with_suffix(".pdf")
        if str(pdf).lower() in taken:
            pdf = img.with_name(f"{img.stem} ({img.suffix.lstrip('.').lower()}).pdf")
        taken.add(str(pdf).lower())
        plan.append(([img], pdf))
    return plan


def plan_per_folder(images: list[Path]) -> list[tuple[list[Path], Path]]:
    """All images of a folder -> <folder name>.pdf inside that folder."""
    plan = []
    by_folder = sorted(images, key=lambda p: natural_key(p.parent))
    for folder, group in groupby(by_folder, key=lambda p: p.parent):
        name = folder.name or "images"  # a drive root like D:\ has no name
        plan.append((sorted(group, key=natural_key), folder / f"{name}.pdf"))
    return plan


def _pillow_frames_as_png(src: Path) -> list[bytes]:
    """Fallback for images img2pdf can't embed directly (WebP, odd colour modes)."""
    pages = []
    with Image.open(src) as im:
        dpi = im.info.get("dpi")
        for frame in ImageSequence.Iterator(im):
            frame = ImageOps.exif_transpose(frame)
            if frame.mode in ("RGBA", "LA", "P", "PA"):
                frame = frame.convert("RGBA")
                background = Image.new("RGB", frame.size, "white")
                background.paste(frame, mask=frame.getchannel("A"))
                frame = background
            elif frame.mode not in ("RGB", "L", "1"):
                frame = frame.convert("RGB")
            buf = io.BytesIO()
            options = {"dpi": dpi} if dpi else {}
            frame.save(buf, format="PNG", compress_level=ZLIB_LEVEL, **options)
            pages.append(buf.getvalue())
    return pages


def _img2pdf(inputs) -> bytes:
    # The internal engine is ~10x faster than img2pdf's default (pikepdf) engine.
    return img2pdf.convert(inputs, rotation=img2pdf.Rotation.ifvalid,
                           engine=img2pdf.Engine.internal)


def image_pdf_bytes(src: Path) -> bytes:
    """One image -> PDF bytes (several pages for a multi-page TIFF)."""
    try:
        # Embeds JPEGs as-is, without re-compression.
        return _img2pdf(str(src))
    except Exception:
        return _img2pdf(_pillow_frames_as_png(src))


def _try_image_pdf_bytes(src: Path) -> tuple[bytes | None, Exception | None]:
    try:
        return image_pdf_bytes(src), None
    except Exception as e:
        return None, e


def images_to_pdf(srcs: list[Path], dst: Path, on_image=lambda src: None,
                  pool: ThreadPoolExecutor | None = None) -> list[tuple[Path, Exception]]:
    """Write all images into one PDF, in order. on_image(src) is called after each image.

    With a pool, the images are converted in parallel (pages still keep their order).
    Unreadable images are left out and returned as (image, error) pairs; if none
    of the images can be read, the error is raised and no PDF is written.
    """
    results = (pool.map if pool else map)(_try_image_pdf_bytes, srcs)
    parts, bad = [], []
    for src, (data, err) in zip(srcs, results):
        if err:
            bad.append((src, err))
        else:
            parts.append(data)
        on_image(src)
    if not parts:
        raise bad[0][1]

    # Write to a temp file first so an interrupted run never leaves a broken PDF.
    tmp = dst.with_name(dst.name + ".part")
    try:
        if len(parts) == 1:
            tmp.write_bytes(parts[0])
        else:
            # Copying pages between PDFs keeps the image data untouched. The source
            # PDFs must stay open until the merged file is saved.
            sources = [pikepdf.open(io.BytesIO(p)) for p in parts]
            try:
                with pikepdf.new() as merged:
                    for pdf in sources:
                        merged.pages.extend(pdf.pages)
                    merged.save(tmp)
            finally:
                for pdf in sources:
                    pdf.close()
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return bad


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def time_left(seconds: float) -> str:
    if seconds < 5:
        return "almost done"
    seconds = int(seconds + 0.5)
    if seconds < 60:
        return f"about {seconds} sec left"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"about {m} min {s:02} sec left"
    h, m = divmod(m, 60)
    return f"about {h} hr {m:02} min left"


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    def __init__(self, start_folder: str | None = None):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("820x640")
        self.minsize(640, 520)

        self.folder = tk.StringVar(value=start_folder or "")
        self.mode = tk.StringVar(value=MODE_FOLDER)
        self.recursive = tk.BooleanVar(value=True)
        self.skip_existing = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Choose a folder to begin.")
        self.percent = tk.StringVar(value="0%")
        self.details = tk.StringVar(value="")

        self.images: list[Path] = []
        self.events: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.busy = False
        self.converting = False

        # Progress state (counted in images, so folder mode moves smoothly too)
        self.total_images = 0
        self.done_images = 0       # converted + skipped + unreadable
        self.worked_images = 0     # images actually converted, used for speed / time left
        self.started_at = 0.0
        self.finished_at = 0.0
        self.shown_percent = 0.0   # animated value that eases towards the real one
        self.bar_colour = BAR_RUNNING
        self.frame = 0

        self._build_ui()
        self.after(100, self._drain_events)
        self.after(FRAME_MS, self._animate)
        if start_folder:
            self.scan()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Folder:").pack(side="left")
        ttk.Entry(top, textvariable=self.folder, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6)
        self.browse_btn = ttk.Button(top, text="Browse...", command=self.browse)
        self.browse_btn.pack(side="left")

        mode = ttk.LabelFrame(self, text="Output")
        mode.pack(fill="x", **pad)
        ttk.Radiobutton(mode, variable=self.mode, value=MODE_FOLDER,
                        command=self.show_found,
                        text="One PDF per folder  (all images of a folder combined, "
                             "saved in that folder as <folder name>.pdf)"
                        ).pack(anchor="w", padx=8, pady=2)
        ttk.Radiobutton(mode, variable=self.mode, value=MODE_IMAGE,
                        command=self.show_found,
                        text="One PDF per image  (same name as the image, saved next to it)"
                        ).pack(anchor="w", padx=8, pady=2)

        opts = ttk.Frame(self)
        opts.pack(fill="x", **pad)
        ttk.Checkbutton(opts, text="Include subfolders", variable=self.recursive,
                        command=self.scan).pack(side="left")
        ttk.Checkbutton(opts, text="Skip if the PDF already exists",
                        variable=self.skip_existing).pack(side="left", padx=16)

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.convert_btn = ttk.Button(actions, text="Convert to PDF",
                                      command=self.start_convert, state="disabled")
        self.convert_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(actions, text="Open folder", command=self.open_folder,
                                   state="disabled")
        self.open_btn.pack(side="right")

        # Progress panel: spinner + status + big percentage, animated bar, details line
        panel = ttk.Frame(self)
        panel.pack(fill="x", padx=10, pady=(10, 4))
        head = ttk.Frame(panel)
        head.pack(fill="x")
        self.spinner = tk.Canvas(head, width=22, height=22, highlightthickness=0,
                                 bg=self.cget("bg"))
        self.spinner.pack(side="left", padx=(0, 6))
        ttk.Label(head, textvariable=self.status).pack(side="left", fill="x", expand=True)
        ttk.Label(head, textvariable=self.percent, font=("Segoe UI", 22, "bold"),
                  width=5, anchor="e").pack(side="right")
        self.bar = tk.Canvas(panel, height=20, highlightthickness=0, bg=TROUGH)
        self.bar.pack(fill="x", pady=(2, 4))
        ttk.Label(panel, textvariable=self.details, foreground="#555").pack(anchor="w")

        self.log = ScrolledText(self, height=12, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, **pad)
        self.log.tag_config("error", foreground="#c62828")
        self.log.tag_config("ok", foreground="#2e7d32")

    # ---- helpers ---------------------------------------------------------- #

    def write_log(self, text: str, tag: str | None = None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy: bool):
        self.busy = busy
        self.browse_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self.convert_btn.configure(
            state="disabled" if busy or not self.images else "normal")

    def open_folder(self):
        if self.folder.get():
            os.startfile(self.folder.get())

    def rel(self, path: Path) -> str:
        return os.path.relpath(path, self.folder.get())

    def plan(self) -> list[tuple[list[Path], Path]]:
        if self.mode.get() == MODE_FOLDER:
            return plan_per_folder(self.images)
        return plan_per_image(self.images)

    def reset_progress(self):
        self.total_images = self.done_images = self.worked_images = 0
        self.shown_percent = 0.0
        self.bar_colour = BAR_RUNNING
        self.details.set("")

    def show_found(self):
        if self.busy or not self.folder.get():
            return
        if not self.images:
            self.status.set("No images found in this folder.")
            return
        folders = len({p.parent for p in self.images})
        pdfs = folders if self.mode.get() == MODE_FOLDER else len(self.images)
        self.status.set(f"Found {len(self.images)} images in {folders} folder(s)  ->  "
                        f"{pdfs} PDF(s) will be created. Click 'Convert to PDF' to start.")

    # ---- scanning --------------------------------------------------------- #

    def browse(self):
        chosen = filedialog.askdirectory(title="Select folder with images",
                                         initialdir=self.folder.get() or None)
        if chosen:
            self.folder.set(os.path.normpath(chosen))
            self.scan()

    def scan(self):
        if self.busy or not self.folder.get():
            return
        root = Path(self.folder.get())
        self.images = []
        self.set_busy(True)
        self.stop_btn.configure(state="disabled")
        self.open_btn.configure(state="normal")
        self.reset_progress()
        self.status.set("Scanning folder...")

        def work():
            try:
                self.events.put(("scanned", find_images(root, self.recursive.get())))
            except Exception as e:
                self.events.put(("scan_error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # ---- converting ------------------------------------------------------- #

    def start_convert(self):
        if not self.images:
            return
        jobs = self.plan()
        self.stop_flag.clear()
        self.set_busy(True)
        self.reset_progress()
        self.converting = True
        self.total_images = len(self.images)
        self.started_at = time.monotonic()
        self.status.set("Starting...")
        self.write_log(f"--- Creating {len(jobs)} PDF(s) from {len(self.images)} images ---")
        threading.Thread(target=self._convert_worker,
                         args=(jobs, self.skip_existing.get(),
                               self.mode.get() == MODE_FOLDER),
                         daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        self.stop_btn.configure(state="disabled")
        self.status.set("Stopping after the images in progress...")

    def _convert_worker(self, jobs, skip_existing: bool, per_folder: bool):
        counts = {"converted": 0, "skipped": 0, "failed": 0, "bad_images": 0}
        # Per folder: a few folder PDFs at a time, their images shared over one pool.
        # Per image: every job is a single image, so the jobs themselves run in parallel.
        image_pool = ThreadPoolExecutor(max_workers=WORKERS) if per_folder else None
        workers = FOLDER_WORKERS if per_folder else WORKERS

        def on_image(src: Path):
            self.events.put(("image", src))
            if self.stop_flag.is_set():
                raise Cancelled

        def convert_one(imgs: list[Path], pdf: Path):
            if self.stop_flag.is_set():
                return "cancelled", imgs, pdf, None, []
            if skip_existing and pdf.exists():
                return "skipped", imgs, pdf, None, []
            try:
                return "converted", imgs, pdf, None, images_to_pdf(imgs, pdf, on_image,
                                                                   image_pool)
            except Cancelled:
                return "cancelled", imgs, pdf, None, []
            except Exception as e:
                return "failed", imgs, pdf, e, []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(convert_one, imgs, pdf) for imgs, pdf in jobs]
            for fut in as_completed(futures):
                result, imgs, pdf, err, bad = fut.result()
                if result in counts:
                    counts[result] += 1
                counts["bad_images"] += len(bad)
                self.events.put(("job", result, imgs, pdf, err, bad))

        if image_pool:
            image_pool.shutdown(cancel_futures=True)
        self.events.put(("done", counts, self.stop_flag.is_set()))

    # ---- animation -------------------------------------------------------- #

    def _animate(self):
        self.frame += 1

        target = 100 * self.done_images / self.total_images if self.total_images else 0
        gap = target - self.shown_percent
        # Ease towards the real value so the number counts up instead of jumping.
        self.shown_percent = target if abs(gap) < 0.2 else self.shown_percent + gap * 0.15
        self.percent.set(f"{int(self.shown_percent)}%")

        self._draw_bar()
        self._draw_spinner()
        if self.converting and self.frame % 10 == 0:
            self._update_details()
        self.after(FRAME_MS, self._animate)

    def _draw_bar(self):
        c = self.bar
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        filled = width * self.shown_percent / 100
        if filled < 1:
            return
        c.create_rectangle(0, 0, filled, height, fill=self.bar_colour, width=0)
        if self.converting:
            # A light band sweeping across the filled part while work is running.
            band = 90
            x = (self.frame * 8) % (filled + band) - band
            c.create_rectangle(max(x, 0), 0, min(x + band, filled), height,
                               fill=BAR_SHINE, width=0)

    def _draw_spinner(self):
        s = self.spinner
        s.delete("all")
        if self.busy:
            s.create_arc(3, 3, 19, 19, start=-self.frame * 12, extent=270,
                         style="arc", outline=BAR_RUNNING, width=3)

    def _update_details(self):
        end = self.finished_at if not self.converting else time.monotonic()
        elapsed = end - self.started_at
        parts = [f"{self.done_images} of {self.total_images} images",
                 f"Elapsed {clock(elapsed)}"]
        speed = self.worked_images / elapsed if elapsed > 0 else 0
        if not self.converting:
            if speed:
                parts.append(f"{speed:.1f} images/sec")
        elif self.worked_images >= 2 and elapsed >= 1:
            parts.append(time_left((self.total_images - self.done_images) / speed).capitalize())
            parts.append(f"{speed:.1f} images/sec")
        else:
            parts.append("Estimating time left...")
        self.details.set("   •   ".join(parts))

    # ---- event loop ------------------------------------------------------- #

    def _drain_events(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    @staticmethod
    def _reason(err: Exception) -> str:
        if isinstance(err, UnidentifiedImageError):
            return "not a valid image (corrupt or unsupported format)"
        return str(err)

    def _handle(self, event):
        kind = event[0]

        if kind == "scanned":
            self.images = event[1]
            self.set_busy(False)
            self.show_found()

        elif kind == "scan_error":
            self.set_busy(False)
            self.status.set("Could not read folder.")
            messagebox.showerror(APP_TITLE, f"Could not read folder:\n{event[1]}")

        elif kind == "image":
            self.done_images += 1
            self.worked_images += 1
            if not self.stop_flag.is_set():
                self.status.set(f"Converting   {self.rel(event[1])}")

        elif kind == "job":
            _, result, imgs, pdf, err, bad = event
            if result == "skipped":
                self.done_images += len(imgs)
            elif result == "failed":
                self.write_log(f"FAILED  {self.rel(pdf)}  ->  {self._reason(err)}", "error")
            elif result == "converted" and self.mode.get() == MODE_FOLDER:
                self.write_log(f"Created  {self.rel(pdf)}  ({len(imgs) - len(bad)} images)")
            elif result == "converted" and imgs[0].stem != pdf.stem:
                self.write_log(f"Name clash: {self.rel(imgs[0])} saved as \"{pdf.name}\"")
            for img, img_err in bad:
                self.write_log(f"    LEFT OUT  {self.rel(img)}  ->  {self._reason(img_err)}",
                               "error")

        elif kind == "done":
            _, counts, cancelled = event
            self.converting = False
            self.finished_at = time.monotonic()
            self.set_busy(False)
            problems = counts["failed"] or counts["bad_images"]
            if cancelled:
                self.bar_colour = BAR_STOPPED
            else:
                self.done_images = self.total_images
                self.bar_colour = BAR_PROBLEMS if problems else BAR_DONE
            self._update_details()

            summary = (f"{'Stopped' if cancelled else 'Finished'}:  "
                       f"{counts['converted']} PDF(s) created,  {counts['skipped']} skipped "
                       f"(PDF already existed),  {counts['failed']} failed.")
            if counts["bad_images"]:
                summary += f"  {counts['bad_images']} unreadable image(s) left out, see log."
            self.status.set(summary)
            self.write_log(summary, "error" if problems else "ok")


def main():
    # Allow dragging a folder onto the .exe icon.
    start = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None
    App(start).mainloop()


if __name__ == "__main__":
    main()

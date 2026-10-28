from __future__ import annotations

import json
import math
import queue
import re
import shutil
import subprocess
import threading
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional


EventCallback = Callable[[str, object], None]


def find_binary(name: str, app_dir: Path) -> Optional[str]:
    """ابحث أولًا في PATH، ثم في مجلد tools بجانب التطبيق."""
    executable = f"{name}.exe" if name.lower() in {"ffmpeg", "ffprobe"} else name

    bundled = app_dir / "tools" / executable
    if bundled.is_file():
        return str(bundled)

    return shutil.which(executable) or shutil.which(name)


def format_timestamp(seconds: float) -> str:
    """تنسيق توقيت مناسب للعرض واسم الملف."""
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def make_timestamps(duration: float, interval: int = 10) -> list[int]:
    """ابدأ من الثانية صفر، ولا تنتج توقيتًا يتجاوز مدة الفيديو."""
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("مدة الفيديو غير صالحة.")

    if duration == 0:
        return [0]

    return list(range(0, math.ceil(duration), interval))


def parse_frame_rate(value: object) -> Optional[float]:
    """تحويل معدل الإطارات من صيغة مثل 30000/1001 إلى رقم عشري."""
    if not value or value in {"N/A", "0/0"}:
        return None

    try:
        rate = float(Fraction(str(value)))
        return rate if math.isfinite(rate) and rate > 0 else None
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def safe_project_name(source: Path) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source.stem).strip(" .")
    return stem or "video"


def create_project_dir(output_base: Path, source: Path) -> Path:
    output_base.mkdir(parents=True, exist_ok=True)
    base_name = f"{safe_project_name(source)}_project"

    # دقة الميكروثانية مع فحص التكرار تمنع الكتابة فوق مشروع سابق.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    project_dir = output_base / f"{base_name}_{stamp}"
    project_dir.mkdir(parents=False, exist_ok=False)
    return project_dir


def _hidden_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_process(
    command: list[str],
    on_progress: Optional[Callable[[float], None]] = None,
    duration: Optional[float] = None,
    timeout: Optional[float] = None,
) -> tuple[int, str]:
    """
    تشغيل عملية مع قراءة stdout وstderr بالتوازي.
    يدعم ذلك عرض تقدم FFmpeg دون حبس واجهة البرنامج.
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_hidden_window_flag(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "تعذر العثور على البرنامج التنفيذي المطلوب. "
            "تحقق من تثبيت FFmpeg وFFprobe أو من مجلد tools."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"تعذر تشغيل الأمر: {exc}") from exc

    messages: queue.Queue[tuple[str, str]] = queue.Queue()
    stderr_lines: list[str] = []

    def read_stream(stream, label: str) -> None:
        try:
            for line in stream:
                messages.put((label, line.rstrip("\r\n")))
        finally:
            messages.put((label, ""))

    assert process.stdout is not None
    assert process.stderr is not None

    threading.Thread(
        target=read_stream, args=(process.stdout, "stdout"), daemon=True
    ).start()
    threading.Thread(
        target=read_stream, args=(process.stderr, "stderr"), daemon=True
    ).start()

    current_time = 0.0
    completed_at: Optional[float] = None

    while process.poll() is None or not messages.empty():
        try:
            label, line = messages.get(timeout=0.1)
        except queue.Empty:
            if timeout is not None and completed_at is not None:
                if (datetime.now().timestamp() - completed_at) > timeout:
                    process.kill()
                    raise RuntimeError("تجاوزت العملية المهلة المحددة.")
            continue

        if not line:
            continue

        if label == "stderr":
            stderr_lines.append(line)
            # لا نحتفظ بكمية غير محدودة من رسائل الخطأ.
            if len(stderr_lines) > 100:
                del stderr_lines[:20]
            continue

        if line.startswith("out_time_us=") and duration and duration > 0:
            try:
                current_time = int(line.split("=", 1)[1]) / 1_000_000
                if on_progress:
                    on_progress(min(100.0, max(0.0, current_time / duration * 100)))
            except ValueError:
                pass
        elif line.startswith("out_time=") and duration and duration > 0:
            try:
                hh, mm, ss = line.split("=", 1)[1].split(":")
                current_time = int(hh) * 3600 + int(mm) * 60 + float(ss)
                if on_progress:
                    on_progress(min(100.0, max(0.0, current_time / duration * 100)))
            except (ValueError, TypeError):
                pass

    return_code = process.wait()
    return return_code, "\n".join(stderr_lines).strip()


def _run_ffprobe(ffprobe: str, source: Path) -> dict:
    command = [
        ffprobe,
        "-v", "error",
        "-count_frames",
        "-show_entries",
        (
            "format=duration:"
            "stream=index,codec_type,width,height,avg_frame_rate,"
            "r_frame_rate,nb_frames,nb_read_frames,duration"
        ),
        "-of", "json",
        str(source),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=_hidden_window_flag(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("لم يتم العثور على ffprobe.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("انتهت مهلة تحليل الملف بواسطة ffprobe.") from exc
    except OSError as exc:
        raise RuntimeError(f"تعذر تشغيل ffprobe: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or "لم يتمكن FFprobe من قراءة الملف."
        raise RuntimeError(f"فشل تحليل الفيديو:\n{detail}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("أعاد ffprobe معلومات غير قابلة للقراءة.") from exc


def probe_video(ffprobe: str, source: Path) -> dict:
    data = _run_ffprobe(ffprobe, source)
    streams = data.get("streams", [])
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )

    if video_stream is None:
        raise RuntimeError("الملف لا يحتوي على مسار فيديو قابل للقراءة.")

    raw_duration = (
        data.get("format", {}).get("duration")
        or video_stream.get("duration")
    )
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        raise RuntimeError("تعذر تحديد مدة الفيديو من معلومات الملف.")

    if not math.isfinite(duration) or duration < 0:
        raise RuntimeError("أبلغ ffprobe عن مدة فيديو غير صالحة.")

    width = video_stream.get("width")
    height = video_stream.get("height")
    rate = parse_frame_rate(video_stream.get("avg_frame_rate"))
    if rate is None:
        rate = parse_frame_rate(video_stream.get("r_frame_rate"))

    # nb_read_frames ينتج من العد أثناء القراءة. إذا لم يتوفر، نستخدم
    # nb_frames الذي قد يكون قيمة بيانات وصفية غير متاحة في بعض الملفات.
    frame_count = video_stream.get("nb_read_frames")
    frame_count_source = "count_frames"
    if not frame_count or frame_count == "N/A":
        frame_count = video_stream.get("nb_frames")
        frame_count_source = "metadata"

    try:
        frame_count = int(frame_count) if frame_count not in (None, "N/A") else None
    except (ValueError, TypeError):
        frame_count = None

    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]

    return {
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "frame_rate": rate,
        "frame_count": frame_count,
        "frame_count_source": frame_count_source if frame_count is not None else None,
        "has_audio": bool(audio_streams),
        "video_codec": video_stream.get("codec_name"),
        "audio_stream_count": len(audio_streams),
    }


def _run_ffmpeg(
    ffmpeg: str,
    command_args: list[str],
    duration: Optional[float],
    on_progress: Optional[Callable[[float], None]] = None,
) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-progress", "pipe:1",
        "-v", "error",
        *command_args,
    ]

    code, error_text = run_process(
        command,
        on_progress=on_progress,
        duration=duration,
    )
    if code != 0:
        raise RuntimeError(
            error_text or f"انتهى FFmpeg برمز خطأ ({code})."
        )


def extract_audio(
    ffmpeg: str,
    source: Path,
    destination: Path,
    duration: float,
    on_progress: Callable[[float], None],
) -> None:
    _run_ffmpeg(
        ffmpeg,
        [
            "-i", str(source),
            "-map", "0:a:0",
            "-vn",
            "-c:a", "pcm_s16le",
            "-y",
            str(destination),
        ],
        duration,
        on_progress,
    )


def extract_thumbnail(
    ffmpeg: str,
    source: Path,
    destination: Path,
    timestamp: int,
) -> None:
    _run_ffmpeg(
        ffmpeg,
        [
            "-i", str(source),
            "-ss", str(timestamp),
            "-frames:v", "1",
            "-vf", "scale=320:-2",
            "-q:v", "3",
            "-y",
            str(destination),
        ],
        None,
    )


def create_contact_sheet(
    image_items: list[tuple[Path, int]],
    destination: Path,
) -> None:
    from PIL import Image, ImageDraw, ImageFont

    if not image_items:
        raise RuntimeError("لا توجد صور مصغرة لإنشاء Contact Sheet.")

    columns = 4
    cell_width = 340
    image_height = 200
    label_height = 34
    rows = math.ceil(len(image_items) / columns)

    sheet = Image.new(
        "RGB",
        (columns * cell_width, rows * (image_height + label_height)),
        color=(30, 30, 30),
    )
    draw = ImageDraw.Draw(sheet)

    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()

    for index, (path, timestamp) in enumerate(image_items):
        with Image.open(path) as original:
            thumbnail = original.convert("RGB")
            thumbnail.thumbnail((cell_width - 12, image_height - 12))

        row, column = divmod(index, columns)
        x = column * cell_width
        y = row * (image_height + label_height)

        paste_x = x + (cell_width - thumbnail.width) // 2
        paste_y = y + (image_height - thumbnail.height) // 2
        sheet.paste(thumbnail, (paste_x, paste_y))

        label = format_timestamp(float(timestamp))
        draw.text(
            (x + 10, y + image_height + 4),
            label,
            fill=(255, 255, 255),
            font=font,
        )

    sheet.save(destination, format="JPEG", quality=90)


def process_video(
    source_path: str,
    output_base_path: str,
    callback: EventCallback,
) -> Path:
    source = Path(source_path).expanduser().resolve()
    output_base = Path(output_base_path).expanduser().resolve()
    app_dir = Path(__file__).resolve().parent

    if not source.is_file():
        raise RuntimeError("ملف الفيديو غير موجود أو لا يمكن الوصول إليه.")

    ffmpeg = find_binary("ffmpeg", app_dir)
    ffprobe = find_binary("ffprobe", app_dir)

    missing = []
    if not ffmpeg:
        missing.append("ffmpeg.exe")
    if not ffprobe:
        missing.append("ffprobe.exe")

    if missing:
        raise RuntimeError(
            "لم يتم العثور على: "
            + " و".join(missing)
            + ".\nثبّت نسخة FFmpeg لويندوز، ثم أضف مجلد bin إلى PATH، "
            "أو ضع ffmpeg.exe وffprobe.exe في مجلد tools بجانب app.py."
        )

    callback("log", f"تحليل الملف: {source}")
    callback("progress", 2)

    info = probe_video(ffprobe, source)
    duration = info["duration_seconds"]
    project_dir = create_project_dir(output_base, source)

    metadata = {
        "source_file": str(source),
        "project_directory": str(project_dir),
        "duration_seconds": duration,
        "resolution": {
            "width": info["width"],
            "height": info["height"],
        },
        "frame_rate_fps": info["frame_rate"],
        "frame_count": info["frame_count"],
        "frame_count_method": info["frame_count_source"],
        "video_codec": info["video_codec"],
        "audio_stream_count": info["audio_stream_count"],
        "created_at": datetime.now().astimezone().isoformat(),
        "notes": (
            "frame_count_method=count_frames يعني أن ffprobe عدّ الإطارات "
            "أثناء القراءة. metadata يعني أن الرقم مأخوذ من بيانات الملف."
        ),
    }

    (project_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    callback(
        "log",
        f"الدقة: {info['width']} × {info['height']} | "
        f"المدة: {format_timestamp(duration)} | "
        f"معدل الإطارات: "
        f"{info['frame_rate'] if info['frame_rate'] is not None else 'غير متاح'}",
    )
    callback(
        "log",
        f"عدد الإطارات: "
        f"{info['frame_count'] if info['frame_count'] is not None else 'غير متاح'} "
        f"({info['frame_count_source'] or 'لم يحدده ffprobe'})",
    )
    callback("progress", 10)

    if info["has_audio"]:
        callback("log", "استخراج الصوت إلى WAV...")
        audio_path = project_dir / "audio.wav"

        def audio_progress(percent: float) -> None:
            callback("progress", 10 + percent * 0.45)

        extract_audio(ffmpeg, source, audio_path, duration, audio_progress)
        callback("log", f"تم حفظ الصوت: {audio_path}")
    else:
        callback("log", "لم يُعثر على مسار صوتي؛ تم تخطي إنشاء WAV.")

    callback("progress", 55)
    callback("log", "التقاط صورة كل 10 ثوانٍ، بدءًا من الثانية صفر...")

    thumbnails_dir = project_dir / "thumbnails"
    thumbnails_dir.mkdir()

    timestamps = make_timestamps(duration, 10)
    successful_images: list[tuple[Path, int]] = []
    thumbnail_errors: list[str] = []

    for index, timestamp in enumerate(timestamps):
        image_path = thumbnails_dir / f"thumb_{timestamp:06d}s.jpg"
        try:
            extract_thumbnail(ffmpeg, source, image_path, timestamp)
            if image_path.is_file() and image_path.stat().st_size > 0:
                successful_images.append((image_path, timestamp))
                callback("log", f"تم التقاط الصورة عند {timestamp} ثانية.")
            else:
                raise RuntimeError("لم ينتج FFmpeg ملف صورة.")
        except Exception as exc:
            message = f"تعذر التقاط الصورة عند {timestamp} ثانية: {exc}"
            thumbnail_errors.append(message)
            callback("log", message)

        percent = 55 + ((index + 1) / max(1, len(timestamps))) * 35
        callback("progress", percent)

    if not successful_images:
        raise RuntimeError(
            "لم يتمكن FFmpeg من استخراج أي صورة من الفيديو. "
            "قد يكون الملف تالفًا أو غير مدعوم."
        )

    callback("log", "إنشاء Contact Sheet...")
    contact_sheet_path = project_dir / "contact_sheet.jpg"
    create_contact_sheet(successful_images, contact_sheet_path)

    metadata["thumbnail_timestamps_seconds"] = [
        timestamp for _, timestamp in successful_images
    ]
    metadata["thumbnail_errors"] = thumbnail_errors
    (project_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    callback("progress", 100)
    callback("log", f"اكتملت المعالجة. مجلد المشروع: {project_dir}")
    return project_dir

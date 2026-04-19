"""圖片轉換為吧唧顯示格式。

單圖：JPEG 368x368 (或 BadgeInfo 解析度)
支援 Pillow 或 ffmpeg。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def prepare_image(
    file_path: str,
    target_size: tuple[int, int] = (368, 368),
) -> bytes:
    """將圖片轉換為吧唧可接受的格式 (JPEG)。

    - .avi, .mjpeg: 直接讀取
    - .png, .jpg, .jpeg: 縮放後轉 JPEG
    - 其他: 嘗試 Pillow 或 ffmpeg
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"file not found: {file_path}")
    ext = Path(file_path).suffix.lower()
    if ext in (".avi", ".mjpeg"):
        with open(file_path, "rb") as f:
            return f.read()
    if ext in (".png", ".jpg", ".jpeg"):
        try:
            return _convert_with_pillow(file_path, target_size)
        except ImportError:
            return _convert_with_ffmpeg(file_path, target_size)
    try:
        return _convert_with_pillow(file_path, target_size)
    except Exception:
        return _convert_with_ffmpeg(file_path, target_size)


E87_TARGET_IMAGE_BYTES = 16000

# Fallback font lookup order — 中文優先，英文字型為最後備案
_DEFAULT_CJK_FONTS = (
    "/System/Library/Fonts/PingFang.ttc",            # macOS 11+
    "/System/Library/Fonts/Hiragino Sans GB.ttc",    # macOS (含簡繁中文)
    "/System/Library/Fonts/STHeiti Medium.ttc",      # 舊版 macOS
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Apple LiGothic Medium.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",  # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Helvetica.ttc",           # 英文才會走這裡
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def encode_badge_jpeg(pil_img) -> bytes:
    """單張圖片 (JPEG) 編碼；用 quality bracketing 壓到 ≤16KB 以符合裝置單張限制。"""
    import io
    from PIL import Image
    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")
    quality_steps = [88, 80, 72, 64, 56, 48, 40, 34]
    result = b""
    for q in quality_steps:
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=q)
        result = buf.getvalue()
        if len(result) <= E87_TARGET_IMAGE_BYTES:
            break
    return result


def encode_video_jpeg(pil_img, quality: int = 85) -> bytes:
    """影片/多圖 AVI 幀用的 JPEG 編碼；固定品質 (預設 85) 以確保裝置能解碼。
    裝置對 AVI 幀大小容忍較高，壓太低會回 Device error 0x01。"""
    import io
    from PIL import Image
    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def text_frames(
    text: str,
    mode: str = "scroll",            # scroll / static / circle / pulse / wave
    target_size: tuple[int, int] = (368, 368),
    fps: int = 12,
    duration: float = 5.0,
    font_size: int = 64,
    font_color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    font_path: Optional[str] = None,
    bold: bool = False,
) -> list:
    """通用文字動畫 frames 產生器。回傳 PIL.Image list。

    mode:
        scroll  — 從右到左跑馬燈 (預設)
        static  — 置中靜置
        circle  — 文字繞圓心旋轉
        pulse   — 縮放脈衝 (中心固定)
        wave    — 上下波浪位移
    """
    import math
    from PIL import Image, ImageDraw, ImageFont

    # 載入字體
    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except (OSError, IOError):
            font = None
    if font is None:
        for fp in _DEFAULT_CJK_FONTS:
            try:
                font = ImageFont.truetype(fp, font_size); break
            except (OSError, IOError):
                continue
    if font is None:
        font = ImageFont.load_default()

    w, h = target_size
    dummy = Image.new("RGB", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    text_y_off = bbox[1]
    n = max(1, int(duration * fps))

    frames = []

    if mode == "static":
        img = Image.new("RGB", (w, h), bg_color)
        _sw = 2 if bold else 0
        ImageDraw.Draw(img).text(
            ((w - tw) // 2 - bbox[0], (h - th) // 2 - text_y_off),
            text, fill=font_color, font=font,
            stroke_width=_sw, stroke_fill=font_color,
        )
        # static：只需一幀重複
        for _ in range(n):
            frames.append(img.copy())
        return frames

    if mode == "scroll":
        total = w + tw
        y = (h - th) // 2 - text_y_off
        for i in range(n):
            progress = i / max(n - 1, 1)
            x = int(w - total * progress)
            img = Image.new("RGB", (w, h), bg_color)
            _sw = 2 if bold else 0
            ImageDraw.Draw(img).text((x, y), text, fill=font_color, font=font,
                                      stroke_width=_sw, stroke_fill=font_color)
            frames.append(img)
        return frames

    if mode == "circle":
        # 文字繞中心 360° 旋轉 (每幀貼一次旋轉後的文字 sprite)
        # 先把文字 render 到透明 sprite
        sprite = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
        ImageDraw.Draw(sprite).text((-bbox[0], -text_y_off), text, fill=font_color, font=font)
        for i in range(n):
            angle = (i / n) * 360.0
            rotated = sprite.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
            img = Image.new("RGB", (w, h), bg_color)
            rx, ry = rotated.size
            img.paste(rotated, ((w - rx) // 2, (h - ry) // 2), rotated)
            frames.append(img)
        return frames

    if mode == "pulse":
        # 縮放 0.6× ~ 1.2× 呼吸
        sprite = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
        ImageDraw.Draw(sprite).text((-bbox[0], -text_y_off), text, fill=font_color, font=font)
        for i in range(n):
            t = i / max(n - 1, 1)
            scale = 0.9 + 0.3 * math.sin(t * math.pi * 2)
            sw, sh = max(1, int(sprite.width * scale)), max(1, int(sprite.height * scale))
            scaled = sprite.resize((sw, sh), Image.Resampling.LANCZOS)
            img = Image.new("RGB", (w, h), bg_color)
            img.paste(scaled, ((w - sw) // 2, (h - sh) // 2), scaled)
            frames.append(img)
        return frames

    if mode == "wave":
        # y 軸正弦波動
        sprite = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
        ImageDraw.Draw(sprite).text((-bbox[0], -text_y_off), text, fill=font_color, font=font)
        amp = 40
        for i in range(n):
            t = i / max(n - 1, 1)
            dy = int(amp * math.sin(t * math.pi * 4))
            img = Image.new("RGB", (w, h), bg_color)
            img.paste(sprite, ((w - sprite.width) // 2, (h - sprite.height) // 2 + dy), sprite)
            frames.append(img)
        return frames

    raise ValueError(f"unknown mode: {mode}")


def danmaku_frames(
    text: str,
    target_size: tuple[int, int] = (368, 368),
    fps: int = 12,
    duration: float = 5.0,
    font_size: int = 64,
    font_color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    font_path: Optional[str] = None,
) -> list:
    """回傳彈幕每一幀的 PIL.Image (用於預覽)."""
    from PIL import Image, ImageDraw, ImageFont
    font = None
    if font_path:
        font = ImageFont.truetype(font_path, font_size)
    else:
        for fp in _DEFAULT_CJK_FONTS:
            try:
                font = ImageFont.truetype(fp, font_size); break
            except (OSError, IOError):
                continue
    if font is None:
        font = ImageFont.load_default()
    dummy = Image.new("RGB", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    text_y_off = bbox[1]
    w, h = target_size
    total = w + tw
    n = max(1, int(duration * fps))
    y = (h - th) // 2 - text_y_off
    frames = []
    for i in range(n):
        progress = i / max(n - 1, 1)
        x = int(w - total * progress)
        img = Image.new("RGB", (w, h), bg_color)
        ImageDraw.Draw(img).text((x, y), text, fill=font_color, font=font)
        frames.append(img)
    return frames


def pattern_frames(
    pattern: str = "gradient",
    target_size: tuple[int, int] = (368, 368),
    frame_count: int = 60,
    color1: tuple[int, int, int] = (255, 0, 0),
    color2: tuple[int, int, int] = (0, 0, 255),
) -> list:
    """回傳圖案動畫每一幀的 PIL.Image."""
    import math
    from PIL import Image, ImageDraw
    w, h = target_size
    frames = []
    for i in range(frame_count):
        t = i / max(frame_count - 1, 1)
        img = Image.new("RGB", (w, h), (0, 0, 0))
        if pattern == "gradient":
            for y in range(h):
                ratio = ((y / h) + t) % 1.0
                c = (
                    int(color1[0] * (1 - ratio) + color2[0] * ratio),
                    int(color1[1] * (1 - ratio) + color2[1] * ratio),
                    int(color1[2] * (1 - ratio) + color2[2] * ratio),
                )
                ImageDraw.Draw(img).line([(0, y), (w, y)], fill=c)
        elif pattern == "pulse":
            b = (math.sin(t * math.pi * 2) + 1) / 2
            img = Image.new("RGB", (w, h), (int(color1[0] * b), int(color1[1] * b), int(color1[2] * b)))
        elif pattern == "checker":
            draw = ImageDraw.Draw(img)
            cell = 46; off = int(t * cell)
            for cy in range(-1, h // cell + 2):
                for cx in range(-1, w // cell + 2):
                    c = color1 if (cx + cy) % 2 == 0 else color2
                    draw.rectangle([cx * cell + off, cy * cell + off,
                                    cx * cell + off + cell, cy * cell + off + cell], fill=c)
        elif pattern == "rainbow":
            import colorsys
            for y in range(h):
                hue = ((y / h) + t) % 1.0
                r, g, b_ = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
                ImageDraw.Draw(img).line([(0, y), (w, y)],
                                         fill=(int(r * 255), int(g * 255), int(b_ * 255)))
        elif pattern == "wave":
            # 取樣降維以加速預覽 (92×92 → 放大到 368×368)
            step = 4
            sw, sh = w // step, h // step
            small = Image.new("RGB", (sw, sh))
            px = small.load()
            for x in range(sw):
                for y in range(sh):
                    val = (math.sin((x / sw + t) * math.pi * 4) +
                           math.sin((y / sh + t) * math.pi * 4)) / 2
                    br = (val + 1) / 2
                    px[x, y] = (
                        int(color1[0] * br + color2[0] * (1 - br)),
                        int(color1[1] * br + color2[1] * (1 - br)),
                        int(color1[2] * br + color2[2] * (1 - br)),
                    )
            img = small.resize((w, h), Image.Resampling.NEAREST)
        else:
            raise ValueError(f"unknown pattern: {pattern}")
        frames.append(img)
    return frames


def video_frames(
    file_path: str,
    target_size: tuple[int, int] = (368, 368),
    fps: int = 12,
    duration: Optional[float] = None,
    fit: str = "cover",
    zoom: float = 1.0,
) -> list:
    """回傳影片/GIF 幀序列 (PIL.Image)。優先 PIL (GIF/APNG)、否則 ffmpeg。"""
    import subprocess
    from PIL import Image
    ext = Path(file_path).suffix.lower()
    frames: list = []

    if ext in (".gif", ".apng", ".webp", ".png"):
        try:
            with Image.open(file_path) as im:
                src_fps = 1000.0 / (im.info.get("duration") or 80)
                step = max(1, int(round(src_fps / max(1, fps))))
                idx = 0
                while True:
                    try:
                        im.seek(idx)
                    except EOFError:
                        break
                    if idx % step == 0:
                        frames.append(transform_to_badge(
                            im.convert("RGB"), fit=fit, zoom=zoom, target_size=target_size))
                    idx += 1
                    if duration and len(frames) >= int(duration * fps):
                        break
            if frames:
                return frames
        except Exception:
            frames = []

    # ffmpeg fallback → extract JPEG frames into memory
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cmd = ["ffmpeg", "-nostdin", "-y", "-i", file_path, "-vf", f"fps={fps}"]
        if duration:
            cmd += ["-t", str(duration)]
        cmd += ["-q:v", "4", os.path.join(td, "f_%04d.jpg")]
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
        for fname in sorted(os.listdir(td)):
            with Image.open(os.path.join(td, fname)) as im:
                frames.append(transform_to_badge(
                    im.convert("RGB"), fit=fit, zoom=zoom, target_size=target_size))
    return frames


def transform_to_badge(pil_img, fit: str = "cover", zoom: float = 1.0,
                       target_size: tuple[int, int] = (368, 368),
                       bg_color: tuple[int, int, int] = (0, 0, 0)):
    """將源圖轉換為 368×368 的 PIL 圖 (用於預覽或上傳)。

    fit:
        cover   - 中心裁切填滿（默認）
        contain - 保留完整畫面、留黑邊
        stretch - 直接拉伸
    zoom: 縮放倍率 (>1 放大 / <1 縮小)。
    """
    from PIL import Image
    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")
    tw, th = target_size
    out = Image.new("RGB", (tw, th), bg_color)
    src_w, src_h = pil_img.size

    if fit == "stretch":
        scaled = pil_img.resize((int(tw * zoom), int(th * zoom)), Image.Resampling.LANCZOS)
    elif fit == "contain":
        ratio = min(tw / src_w, th / src_h) * zoom
        nw, nh = int(src_w * ratio), int(src_h * ratio)
        scaled = pil_img.resize((max(1, nw), max(1, nh)), Image.Resampling.LANCZOS)
    else:  # cover
        ratio = max(tw / src_w, th / src_h) * zoom
        nw, nh = int(src_w * ratio), int(src_h * ratio)
        scaled = pil_img.resize((max(1, nw), max(1, nh)), Image.Resampling.LANCZOS)

    ox = (tw - scaled.width) // 2
    oy = (th - scaled.height) // 2
    out.paste(scaled, (ox, oy))
    return out


def _convert_with_pillow(file_path: str, size: tuple[int, int]) -> bytes:
    import io
    from PIL import Image

    with Image.open(file_path) as img:
        img = img.convert("RGB")
        # Crop to square (center-crop) then resize — matches the web
        # reference implementation which fills the target area.
        src_w, src_h = img.size
        crop_dim = min(src_w, src_h)
        left = (src_w - crop_dim) // 2
        top = (src_h - crop_dim) // 2
        img = img.crop((left, top, left + crop_dim, top + crop_dim))
        img = img.resize(size, Image.Resampling.LANCZOS)

        # Quality bracketing: step down until file fits in target size
        quality_steps = [88, 80, 72, 64, 56, 48, 40, 34]
        result = b""
        for q in quality_steps:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=q)
            result = buf.getvalue()
            if len(result) <= E87_TARGET_IMAGE_BYTES:
                break
        return result


def _convert_with_ffmpeg(file_path: str, size: tuple[int, int]) -> bytes:
    w, h = size
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            file_path,
            "-vf",
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "5",
            "-frames:v",
            "1",
            "-an",
            tmp_path,
        ]
        subprocess.run(cmd, capture_output=True, timeout=30, check=True)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def prepare_video(
    file_path: str,
    target_size: tuple[int, int] = (368, 368),
    fps: int = 12,
    duration: Optional[float] = None,
    jpeg_quality: int = 5,
) -> bytes:
    """將影片/動畫轉換為 AVI MJPEG 格式。

    Args:
        duration: 截取時長（秒），None 表示完整轉換。

    優先使用 Pillow 提取幀 (GIF/APNG)，若失敗則 fallback 到 ffmpeg。
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"file not found: {file_path}")

    ext = Path(file_path).suffix.lower()

    # Try Pillow for animated images (GIF, APNG, WebP)
    if ext in (".gif", ".apng", ".webp", ".png"):
        try:
            return _animated_with_pillow(file_path, target_size, fps, duration, jpeg_quality)
        except Exception:
            pass

    # Fallback: ffmpeg for video files
    return _video_with_ffmpeg(file_path, target_size, fps, duration, jpeg_quality)


def _animated_with_pillow(
    file_path: str,
    size: tuple[int, int],
    fps: int,
    duration: Optional[float] = None,
    jpeg_quality: int = 5,
) -> bytes:
    """Extract frames from animated image using Pillow, build AVI."""
    import io
    from PIL import Image
    from ebadge_cli.avi_builder import build_mjpg_avi

    with Image.open(file_path) as img:
        n_frames = getattr(img, "n_frames", 1)
        if n_frames < 2:
            raise ValueError("Not an animated image")

        max_frames = int(duration * fps) if duration else n_frames
        jpeg_frames: list[bytes] = []
        for i in range(min(n_frames, max_frames)):
            img.seek(i)
            frame = img.convert("RGB")
            frame.thumbnail(size, Image.Resampling.LANCZOS)
            w, h = frame.size
            out = Image.new("RGB", size, (0, 0, 0))
            out.paste(frame, ((size[0] - w) // 2, (size[1] - h) // 2))
            buf = io.BytesIO()
            # ffmpeg -q:v (1-31, 低=高品質) → PIL quality (0-100, 高=高品質) 粗略轉換
            pil_q = max(30, min(95, 100 - jpeg_quality * 8))
            out.save(buf, format="JPEG", quality=pil_q)
            jpeg_frames.append(buf.getvalue())

    return build_mjpg_avi(jpeg_frames, width=size[0], height=size[1], fps=fps)


def _video_with_ffmpeg(
    file_path: str,
    size: tuple[int, int],
    fps: int,
    duration: Optional[float] = None,
    jpeg_quality: int = 5,
) -> bytes:
    """Convert video to AVI MJPEG using ffmpeg."""
    w, h = size
    with tempfile.NamedTemporaryFile(suffix=".avi", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            file_path,
        ]
        if duration is not None:
            cmd.extend(["-t", str(duration)])
        cmd.extend([
            "-vf",
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            "-r",
            str(fps),
            "-vcodec",
            "mjpeg",
            "-q:v",
            str(jpeg_quality),
            "-an",
            "-f",
            "avi",
            tmp_path,
        ])
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def prepare_text(
    text: str,
    mode: str = "scroll",
    target_size: tuple[int, int] = (368, 368),
    fps: int = 12,
    duration: float = 5.0,
    font_size: int = 64,
    font_color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    font_path: Optional[str] = None,
    bold: bool = False,
    quality: int = 80,
) -> bytes:
    """把 text_frames 打包成 AVI。"""
    import io
    from ebadge_cli.avi_builder import build_mjpg_avi
    frames = text_frames(text=text, mode=mode, target_size=target_size,
                          fps=fps, duration=duration, font_size=font_size,
                          font_color=font_color, bg_color=bg_color,
                          font_path=font_path, bold=bold)
    jpeg_frames: list[bytes] = []
    for f in frames:
        buf = io.BytesIO()
        f.save(buf, format="JPEG", quality=quality)
        jpeg_frames.append(buf.getvalue())
    return build_mjpg_avi(jpeg_frames, width=target_size[0],
                           height=target_size[1], fps=fps)


def prepare_danmaku(
    text: str,
    target_size: tuple[int, int] = (368, 368),
    fps: int = 12,
    duration: float = 5.0,
    font_size: int = 64,
    font_color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    font_path: Optional[str] = None,
) -> bytes:
    """生成跑馬燈（滾動文字）AVI 動畫。

    Args:
        font_path: 字體檔案路徑 (.ttf/.ttc/.otf)，None 使用系統預設。

    文字從右到左滾動。優先使用 Pillow，若無則 fallback 到 ffmpeg。
    """
    try:
        return _danmaku_with_pillow(text, target_size, fps, duration, font_size, font_color, bg_color, font_path)
    except ImportError:
        return _danmaku_with_ffmpeg(text, target_size, fps, duration, font_size, font_color, bg_color)


def _danmaku_with_pillow(
    text: str,
    size: tuple[int, int],
    fps: int,
    duration: float,
    font_size: int,
    font_color: tuple[int, int, int],
    bg_color: tuple[int, int, int],
    font_path: Optional[str] = None,
) -> bytes:
    import io
    from PIL import Image, ImageDraw, ImageFont
    from ebadge_cli.avi_builder import build_mjpg_avi

    font = None
    # User-specified font
    if font_path:
        font = ImageFont.truetype(font_path, font_size)
    else:
        for fp in _DEFAULT_CJK_FONTS:
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except (OSError, IOError):
                continue
    if font is None:
        font = ImageFont.load_default()

    # Measure text
    dummy = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_y_offset = bbox[1]

    w, h = size
    total_scroll = w + text_w
    n_frames = max(1, int(duration * fps))
    y = (h - text_h) // 2 - text_y_offset

    jpeg_frames: list[bytes] = []
    for i in range(n_frames):
        progress = i / max(n_frames - 1, 1)
        x = int(w - total_scroll * progress)
        img = Image.new("RGB", (w, h), bg_color)
        draw = ImageDraw.Draw(img)
        draw.text((x, y), text, fill=font_color, font=font)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        jpeg_frames.append(buf.getvalue())

    return build_mjpg_avi(jpeg_frames, width=w, height=h, fps=fps)


def _danmaku_with_ffmpeg(
    text: str,
    size: tuple[int, int],
    fps: int,
    duration: float,
    font_size: int,
    font_color: tuple[int, int, int],
    bg_color: tuple[int, int, int],
) -> bytes:
    w, h = size
    bg_hex = f"{bg_color[0]:02x}{bg_color[1]:02x}{bg_color[2]:02x}"
    fg_hex = f"{font_color[0]:02x}{font_color[1]:02x}{font_color[2]:02x}"
    escaped_text = text.replace("'", "'\\''").replace(":", "\\:")
    with tempfile.NamedTemporaryFile(suffix=".avi", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cmd = [
            "ffmpeg", "-nostdin", "-y",
            "-f", "lavfi",
            "-i", f"color=c=0x{bg_hex}:s={w}x{h}:r={fps}:d={duration}",
            "-vf", f"drawtext=text='{escaped_text}':fontcolor=0x{fg_hex}:fontsize={font_size}:x=w-(w+tw)*t/{duration}:y=(h-th)/2",
            "-vcodec", "mjpeg", "-q:v", "7", "-an", "-f", "avi",
            tmp_path,
        ]
        subprocess.run(cmd, capture_output=True, timeout=30, check=True)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def prepare_slideshow(
    file_paths: list[str],
    target_size: tuple[int, int] = (368, 368),
    fps: int = 12,
    duration_per_image: float = 2.0,
) -> bytes:
    """將多張圖片合成為 slideshow AVI 動畫。"""
    import io
    from PIL import Image
    from ebadge_cli.avi_builder import build_mjpg_avi

    frames_per_image = max(1, int(duration_per_image * fps))
    jpeg_frames: list[bytes] = []

    for path in file_paths:
        if not os.path.exists(path):
            raise FileNotFoundError(f"file not found: {path}")
        with Image.open(path) as img:
            img = img.convert("RGB")
            src_w, src_h = img.size
            crop_dim = min(src_w, src_h)
            left = (src_w - crop_dim) // 2
            top = (src_h - crop_dim) // 2
            img = img.crop((left, top, left + crop_dim, top + crop_dim))
            img = img.resize(target_size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            frame_bytes = buf.getvalue()

        for _ in range(frames_per_image):
            jpeg_frames.append(frame_bytes)

    return build_mjpg_avi(jpeg_frames, width=target_size[0], height=target_size[1], fps=fps)


def prepare_qr(
    data: str,
    target_size: tuple[int, int] = (368, 368),
    fg_color: tuple[int, int, int] = (0, 0, 0),
    bg_color: tuple[int, int, int] = (255, 255, 255),
    zoom: float = 1.0,
) -> bytes:
    """生成 QR Code JPEG 圖片。

    Args:
        data: QR Code 內容（URL 或文字）。
        target_size: 輸出尺寸。
        fg_color: 前景色 (R, G, B)。
        bg_color: 背景色 (R, G, B)。
        zoom: 縮放比例 (0.85 - 1.45)。
    """
    import io
    import qrcode
    from PIL import Image

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=fg_color, back_color=bg_color).convert("RGB")

    # Apply zoom: scale QR within target area
    w, h = target_size
    qr_dim = int(min(w, h) * zoom)
    qr_img = qr_img.resize((qr_dim, qr_dim), Image.Resampling.NEAREST)

    # Center on background
    out = Image.new("RGB", (w, h), bg_color)
    offset_x = (w - qr_dim) // 2
    offset_y = (h - qr_dim) // 2
    out.paste(qr_img, (offset_x, offset_y))

    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def prepare_pattern(
    pattern: str = "gradient",
    target_size: tuple[int, int] = (368, 368),
    fps: int = 12,
    frame_count: int = 60,
    color1: tuple[int, int, int] = (255, 0, 0),
    color2: tuple[int, int, int] = (0, 0, 255),
) -> bytes:
    """生成內建圖案動畫 AVI。

    Patterns:
        gradient  - 漸變色循環
        pulse     - 脈衝閃爍
        checker   - 棋盤格動畫
        rainbow   - 彩虹漸變
        wave      - 波浪掃描
    """
    import io
    import math
    from PIL import Image, ImageDraw
    from ebadge_cli.avi_builder import build_mjpg_avi

    w, h = target_size
    jpeg_frames: list[bytes] = []

    for i in range(frame_count):
        t = i / max(frame_count - 1, 1)  # 0.0 ~ 1.0
        img = Image.new("RGB", (w, h), (0, 0, 0))

        if pattern == "gradient":
            # Smooth gradient sweep between two colors
            for y in range(h):
                ratio = ((y / h) + t) % 1.0
                r = int(color1[0] * (1 - ratio) + color2[0] * ratio)
                g = int(color1[1] * (1 - ratio) + color2[1] * ratio)
                b = int(color1[2] * (1 - ratio) + color2[2] * ratio)
                ImageDraw.Draw(img).line([(0, y), (w, y)], fill=(r, g, b))

        elif pattern == "pulse":
            # Pulsating solid color
            brightness = (math.sin(t * math.pi * 2) + 1) / 2
            r = int(color1[0] * brightness)
            g = int(color1[1] * brightness)
            b = int(color1[2] * brightness)
            img = Image.new("RGB", (w, h), (r, g, b))

        elif pattern == "checker":
            # Animated checkerboard
            draw = ImageDraw.Draw(img)
            cell = 46  # 368/8
            offset = int(t * cell)
            for cy in range(-1, h // cell + 2):
                for cx in range(-1, w // cell + 2):
                    if (cx + cy) % 2 == 0:
                        x0 = cx * cell + offset
                        y0 = cy * cell + offset
                        draw.rectangle([x0, y0, x0 + cell, y0 + cell], fill=color1)
                    else:
                        x0 = cx * cell + offset
                        y0 = cy * cell + offset
                        draw.rectangle([x0, y0, x0 + cell, y0 + cell], fill=color2)

        elif pattern == "rainbow":
            # Full rainbow sweep
            import colorsys
            for y in range(h):
                hue = ((y / h) + t) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
                ImageDraw.Draw(img).line(
                    [(0, y), (w, y)],
                    fill=(int(r * 255), int(g * 255), int(b * 255)),
                )

        elif pattern == "wave":
            # Sine wave color sweep
            draw = ImageDraw.Draw(img)
            for x in range(w):
                for y in range(h):
                    val = (math.sin((x / w + t) * math.pi * 4) +
                           math.sin((y / h + t) * math.pi * 4)) / 2
                    brightness = (val + 1) / 2
                    r = int(color1[0] * brightness + color2[0] * (1 - brightness))
                    g = int(color1[1] * brightness + color2[1] * (1 - brightness))
                    b = int(color1[2] * brightness + color2[2] * (1 - brightness))
                    draw.point((x, y), fill=(r, g, b))
        else:
            raise ValueError(f"unknown pattern: {pattern} (choices: gradient, pulse, checker, rainbow, wave)")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        jpeg_frames.append(buf.getvalue())

    return build_mjpg_avi(jpeg_frames, width=w, height=h, fps=fps)

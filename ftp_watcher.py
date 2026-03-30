#!/usr/bin/env python3
import configparser
import ftplib
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.etree import ElementTree as ET


def is_interactive() -> bool:
    return sys.stdout.isatty()


class TerminalUI:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"

    def __init__(self, enabled: bool, rich: bool = True) -> None:
        self.enabled = enabled
        self.rich = rich
        self.status_visible = False

    def style(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return f"{''.join(codes)}{text}{self.RESET}"

    def terminal_width(self, default: int = 100) -> int:
        return shutil.get_terminal_size((default, 20)).columns

    def rule(self, label: str = "") -> str:
        width = max(60, self.terminal_width())
        if not label:
            return "-" * width
        prefix = f" {label} "
        return prefix + "-" * max(0, width - len(prefix))

    def print_header(self, title: str, rows: List[Tuple[str, str]]) -> None:
        if not self.enabled or not self.rich:
            return
        print()
        print(self.style(self.rule(title), self.BOLD, self.CYAN))
        for key, value in rows:
            print(
                f"{self.style(key.rjust(12), self.BOLD)} : "
                f"{self.style(value, self.GREEN)}"
            )
        print(self.style(self.rule(), self.DIM))

    def print_progress_legend(self) -> None:
        if not self.enabled or not self.rich:
            return
        width = max(60, self.terminal_width())
        legend = "File / Progress / Transfer Stats"
        print(self.style(shorten_text(legend, width), self.DIM))

    def render_status_line(self, state: str, message: str, tone: str = "info") -> None:
        if not self.enabled:
            return

        color = {
            "info": self.CYAN,
            "idle": self.BLUE,
            "success": self.GREEN,
            "warn": self.YELLOW,
        }.get(tone, self.CYAN)

        width = max(60, self.terminal_width())
        badge_text = f" {state.upper()} "
        content_width = max(1, width - len(state) - 5)
        content = shorten_text(message, content_width)
        badge = self.style(badge_text, self.BOLD, color)
        line = f"{badge} {content}"
        sys.stdout.write(f"\r{line.ljust(width)}")
        sys.stdout.flush()
        self.status_visible = True

    def clear_status_line(self) -> None:
        if not self.enabled or not self.status_visible:
            return
        width = max(60, self.terminal_width())
        sys.stdout.write(f"\r{' ' * width}\r")
        sys.stdout.flush()
        self.status_visible = False


def setup_logging(log_to_file: bool = False, log_file: str = "ftp_watcher.log") -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_to_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
    )


def load_config(config_path: str) -> configparser.ConfigParser:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path)

    for section in ["ftp", "local", "watcher"]:
        if section not in config:
            raise ValueError(f"Missing required section [{section}] in config file")

    return config


def get_bool(config: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    return config.getboolean(section, key) if key in config[section] else default


def get_int(config: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    return config.getint(section, key) if key in config[section] else default


def load_state(state_file: Path) -> Set[str]:
    if not state_file.exists():
        return set()
    try:
        return set(json.loads(state_file.read_text(encoding="utf-8")))
    except Exception:
        logging.warning("Could not read state file, starting with empty state.")
        return set()


def save_state(state_file: Path, downloaded_files: Set[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(sorted(downloaded_files), indent=2), encoding="utf-8")


def connect_ftp(config: configparser.ConfigParser) -> ftplib.FTP:
    host = config["ftp"]["host"]
    port = get_int(config, "ftp", "port", 21)
    user = config["ftp"]["user"]
    password = config["ftp"]["password"]
    use_tls = get_bool(config, "ftp", "use_tls", False)
    passive = get_bool(config, "ftp", "passive", True)

    ftp = ftplib.FTP_TLS() if use_tls else ftplib.FTP()
    ftp.connect(host, port, timeout=30)
    ftp.login(user=user, passwd=password)
    ftp.set_pasv(passive)

    if use_tls and isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()

    return ftp


def list_remote_files(ftp: ftplib.FTP, remote_dir: str) -> Dict[str, Tuple[int, str]]:
    ftp.cwd(remote_dir)
    names = []
    ftp.retrlines("NLST", names.append)

    files = {}
    for name in names:
        try:
            size = ftp.size(name) or 0
        except Exception:
            size = 0

        try:
            modified = ftp.sendcmd(f"MDTM {name}").replace("213 ", "").strip()
        except Exception:
            modified = ""

        files[name] = (size, modified)

    return files


def build_file_key(filename: str, size: int, modified: str) -> str:
    return f"{filename}|{size}|{modified}"


def format_bytes(num_bytes: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{num_bytes:.0f} B"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def format_uptime(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def shorten_text(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width <= 3:
        return text[:max_width]
    return f"{text[:max_width - 3]}..."


def pad_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return shorten_text(text, width).ljust(width)


def shorten_filename(filename: str, max_width: int) -> str:
    if max_width <= 0 or len(filename) <= max_width:
        return filename[:max_width] if max_width > 0 else ""
    base, dot, extension = filename.rpartition(".")
    if not dot or max_width <= len(extension) + 4:
        return shorten_text(filename, max_width)
    suffix = f".{extension}"
    prefix_width = max_width - len(suffix) - 3
    return f"{base[:prefix_width]}...{suffix}"


def clip_group_key(name: str) -> str:
    stem = Path(name).stem.upper()
    normalized = re.sub(r"[MS]\d{2}$", "", stem)
    return normalized.rstrip("_-") or stem


def format_timestamp(timestamp: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


def format_iso_datetime(value: str) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def parse_fps_value(value: str) -> Optional[float]:
    if not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isdigit() or ch == ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_clip_duration_from_frames(frame_count: Optional[int], fps: Optional[float]) -> str:
    if frame_count is None or fps is None or fps <= 0:
        return "-"
    seconds = frame_count / fps
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remainder = seconds - (minutes * 60)
    return f"{minutes:02d}:{remainder:04.1f}"


def normalize_gamma(value: str) -> str:
    lowered = value.lower()
    if "hlg" in lowered:
        return "HLG"
    return value


def normalize_color_metadata(value: str) -> str:
    lowered = value.lower()
    if "2020" in lowered:
        return "rec2020"
    return value


def format_timecode(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) != 8:
        return value or "-"
    return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}:{digits[6:8]}"


def parse_clip_metadata_from_xml(xml_path: Path) -> Optional[Dict[str, Any]]:
    namespace = {"nrt": "urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.20"}
    try:
        root = ET.fromstring(xml_path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        return None

    def first(path: str) -> Optional[ET.Element]:
        return root.find(path, namespace)

    def attr(path: str, name: str) -> str:
        element = first(path)
        return element.get(name, "").strip() if element is not None else ""

    related = first("./nrt:RelevantFiles/nrt:RelatedTo")
    related_file = related.get("file", "").strip() if related is not None else ""
    frame_rate_label = attr("./nrt:VideoFormat/nrt:VideoFrame", "formatFps") or (
        related.get("formatFps", "").strip() if related is not None else ""
    )
    fps_value = parse_fps_value(frame_rate_label) or parse_fps_value(attr("./nrt:VideoFormat/nrt:VideoFrame", "captureFps"))

    duration_frames_text = attr("./nrt:Duration", "value")
    duration_frames = int(duration_frames_text) if duration_frames_text.isdigit() else None
    start_tc = ""
    for change in root.findall("./nrt:LtcChangeTable/nrt:LtcChange", namespace):
        if change.get("status") == "increment":
            start_tc = change.get("value", "").strip()
            break

    gamma = ""
    color = ""
    for item in root.findall(".//nrt:Item", namespace):
        name = item.get("name", "")
        value = item.get("value", "").strip()
        if name == "CaptureGammaEquation":
            gamma = normalize_gamma(value)
        elif name in {"CaptureColorPrimaries", "CodingEquations"} and not color:
            color = normalize_color_metadata(value)
        elif name == "CodingEquations":
            color = normalize_color_metadata(value)

    manufacturer = attr("./nrt:Device", "manufacturer")
    model_name = attr("./nrt:Device", "modelName")
    serial_number = attr("./nrt:Device", "serialNo")
    camera = " ".join(part for part in [manufacturer, model_name] if part).strip()

    return {
        "mp4_filename": related_file or f"{xml_path.stem}.MP4",
        "xml_group_key": clip_group_key(xml_path.name),
        "related_group_key": clip_group_key(related_file) if related_file else "",
        "xml_filename": xml_path.name,
        "camera": camera or "-",
        "serial_number": serial_number or "",
        "created": format_iso_datetime(attr("./nrt:CreationDate", "value")),
        "resolution_fps": (
            f"{attr('./nrt:VideoFormat/nrt:VideoLayout', 'pixel')}x"
            f"{attr('./nrt:VideoFormat/nrt:VideoLayout', 'numOfVerticalLine')} / {frame_rate_label}"
            if attr("./nrt:VideoFormat/nrt:VideoLayout", "pixel") and frame_rate_label
            else "-"
        ),
        "duration": format_clip_duration_from_frames(duration_frames, fps_value),
        "timecode": format_timecode(start_tc),
        "gamma_color": ", ".join(part for part in [gamma, color] if part) or "-",
    }


@dataclass
class SessionStats:
    started_at: float
    files_downloaded: int = 0
    bytes_downloaded: int = 0
    failures: int = 0
    skipped: int = 0
    last_poll_at: Optional[float] = None
    last_success_at: Optional[float] = None
    last_download_name: str = "-"
    last_error: str = "-"
    idle_since: Optional[float] = None


class DashboardState:
    def __init__(self, config_data: Dict[str, Any]) -> None:
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.config_data = config_data
        self.clips: Dict[str, Dict[str, Any]] = {}
        self.runtime: Dict[str, Any] = {
            "state": "starting",
            "tone": "info",
            "status_message": "Booting watcher",
            "last_poll_at": None,
            "next_check_at": None,
            "last_success_at": None,
            "last_download_name": None,
            "last_error": None,
            "idle_since": None,
            "files_downloaded": 0,
            "bytes_downloaded": 0,
            "failures": 0,
            "skipped": 0,
            "current_download": None,
            "recent_events": [],
        }

    def update_runtime(self, **kwargs: Any) -> None:
        with self.lock:
            self.runtime.update(kwargs)

    def set_current_download(
        self,
        filename: str,
        downloaded: int,
        total_size: int,
        speed_bytes: float,
        eta_seconds: Optional[float],
    ) -> None:
        current_download = {
            "filename": filename,
            "downloaded": downloaded,
            "total_size": total_size,
            "speed_bytes": speed_bytes,
            "eta_seconds": eta_seconds,
            "percent": (downloaded / total_size * 100.0) if total_size > 0 else None,
        }
        self.update_runtime(current_download=current_download)

    def clear_current_download(self) -> None:
        self.update_runtime(current_download=None)

    def add_event(
        self,
        level: str,
        title: str,
        detail: str,
        filename: Optional[str] = None,
    ) -> None:
        event = {
            "time": time.time(),
            "level": level,
            "title": title,
            "detail": detail,
            "filename": filename,
        }
        with self.lock:
            events = [event, *self.runtime["recent_events"]]
            self.runtime["recent_events"] = events[:20]

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            runtime = dict(self.runtime)
            runtime["recent_events"] = list(self.runtime["recent_events"])
            current_download = self.runtime["current_download"]
            runtime["current_download"] = dict(current_download) if current_download else None
            clips = sorted(
                (dict(clip) for clip in self.clips.values()),
                key=lambda clip: clip.get("downloaded_at") or clip.get("updated_at") or 0,
                reverse=True,
            )
        return {
            "generated_at": time.time(),
            "started_at": self.started_at,
            "config": dict(self.config_data),
            "runtime": runtime,
            "clips": clips,
        }

    def _upsert_clip(self, filename: str, group_key: Optional[str] = None, **fields: Any) -> None:
        now = time.time()
        with self.lock:
            canonical_group_key = group_key or clip_group_key(filename)
            clip_name = filename
            for existing_name in list(self.clips):
                existing_group_key = self.clips[existing_name].get("group_key") or clip_group_key(existing_name)
                if existing_group_key == canonical_group_key:
                    clip_name = existing_name
                    break

            clip = self.clips.setdefault(
                clip_name,
                {
                    "filename": clip_name,
                    "group_key": canonical_group_key,
                    "downloaded": False,
                    "downloaded_at": None,
                    "size_bytes": None,
                    "local_path": None,
                    "metadata": {},
                    "updated_at": now,
                },
            )
            clip.update(fields)
            clip["group_key"] = canonical_group_key
            if clip_name != filename and fields.get("downloaded"):
                clip["filename"] = filename
                self.clips[filename] = clip
                del self.clips[clip_name]
            clip["updated_at"] = now

    def attach_xml_metadata(self, xml_path: Path) -> None:
        metadata = parse_clip_metadata_from_xml(xml_path)
        if metadata is None:
            return
        mp4_filename = metadata.pop("mp4_filename")
        xml_group_key = metadata.pop("xml_group_key", "")
        related_group_key = metadata.pop("related_group_key", "")

        preferred_filename = mp4_filename
        with self.lock:
            for existing_name, clip in self.clips.items():
                existing_key = clip_group_key(existing_name)
                if existing_key in {xml_group_key, related_group_key}:
                    preferred_filename = clip.get("filename", existing_name)
                    break

        self._upsert_clip(preferred_filename, group_key=xml_group_key or related_group_key, metadata=metadata)

    def mark_clip_downloaded(self, filename: str, size_bytes: int, local_path: Path) -> None:
        if not filename.lower().endswith(".mp4"):
            return
        self._upsert_clip(
            filename,
            group_key=clip_group_key(filename),
            downloaded=True,
            downloaded_at=time.time(),
            size_bytes=size_bytes,
            local_path=str(local_path),
        )


def build_dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FTP Sync Dashboard</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23f4eee5'/%3E%3Cpath d='M12 22a6 6 0 0 1 6-6h13l4 5h11a6 6 0 0 1 6 6v15a6 6 0 0 1-6 6H18a6 6 0 0 1-6-6V22Z' fill='%232b7c99'/%3E%3Crect x='16' y='28' width='32' height='8' rx='4' fill='%23ffffff' opacity='.95'/%3E%3Ccircle cx='24' cy='32' r='2.3' fill='%231e9a75'/%3E%3Ccircle cx='32' cy='32' r='2.3' fill='%231e9a75'/%3E%3Ccircle cx='40' cy='32' r='2.3' fill='%231e9a75'/%3E%3C/svg%3E">
  <style>
    :root {
      --bg: #f4eee5;
      --panel: rgba(255, 251, 246, 0.92);
      --panel-strong: #faf6f0;
      --border: rgba(83, 97, 96, 0.12);
      --text: #193033;
      --muted: #738583;
      --accent: #1e9a75;
      --accent-2: #2b7c99;
      --warn: #d69a31;
      --danger: #ca6458;
      --idle: #6887d8;
      --shadow: 0 20px 48px rgba(73, 56, 40, 0.12);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(30, 154, 117, 0.12), transparent 28%),
        radial-gradient(circle at top right, rgba(43, 124, 153, 0.10), transparent 24%),
        linear-gradient(180deg, #fbf7f1 0%, #f1e8dc 100%);
    }
    .shell {
      width: min(1280px, calc(100vw - 32px));
      margin: 24px auto;
      display: grid;
      gap: 18px;
    }
    .hero, .card {
      background: var(--panel);
      backdrop-filter: blur(18px);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
    }
    .hero {
      padding: 28px;
      display: grid;
      gap: 18px;
    }
    .hero-top {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
      flex-wrap: wrap;
    }
    .hero-brand {
      display: flex;
      align-items: center;
      gap: 18px;
      min-width: 0;
    }
    .hero-logo {
      width: 264px;
      height: auto;
      display: block;
      object-fit: contain;
      flex: 0 0 auto;
    }
    h1 {
      margin: 0;
      font-size: clamp(1.2rem, 2.2vw, 2rem);
      letter-spacing: -0.04em;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 12px 16px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid var(--border);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 0.72rem;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 18px currentColor;
    }
    .stats-grid {
      display: grid;
      gap: 16px;
    }
    .stats-grid { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
    .mini {
      padding: 18px 18px 16px;
      background: rgba(255, 255, 255, 0.62);
      border: 1px solid rgba(83, 97, 96, 0.08);
      border-radius: 20px;
    }
    .hero-side {
      display: grid;
      justify-items: end;
      gap: 8px;
    }
    .hero-meta {
      display: grid;
      gap: 2px;
      text-align: right;
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.35;
    }
    .label {
      color: var(--muted);
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 10px;
    }
    .value {
      font-size: 1.12rem;
      font-weight: 700;
      line-height: 1.3;
      word-break: break-word;
    }
    .card {
      padding: 22px;
    }
    .card h2 {
      margin: 0 0 16px;
      font-size: 1.15rem;
      letter-spacing: -0.02em;
    }
    .progress-wrap {
      display: grid;
      gap: 12px;
    }
    .progress-meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 0.95rem;
    }
    .bar {
      width: 100%;
      height: 18px;
      background: rgba(25, 48, 51, 0.08);
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid rgba(83, 97, 96, 0.08);
    }
    .bar-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      border-radius: inherit;
      transition: width 0.4s ease;
    }
    .clip-list {
      display: grid;
      gap: 10px;
      max-height: 460px;
      overflow: auto;
      padding-right: 4px;
    }
    .clip-card {
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.58);
      border: 1px solid rgba(83, 97, 96, 0.08);
    }
    .clip-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 0.88rem;
      margin-bottom: 6px;
    }
    .clip-title {
      font-weight: 700;
    }
    .clip-meta {
      color: var(--muted);
      line-height: 1.45;
      word-break: break-word;
    }
    .clip-grid {
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      margin-top: 12px;
    }
    .clip-cell {
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(255, 249, 244, 0.96);
      border: 1px solid rgba(83, 97, 96, 0.07);
    }
    .clip-cell .k {
      display: block;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 6px;
    }
    .clip-cell .v {
      font-weight: 700;
      line-height: 1.35;
      word-break: break-word;
    }
    .clip-cell .subv {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.86rem;
      font-weight: 600;
    }
    .clip-action {
      margin-top: 10px;
    }
    .link-button {
      appearance: none;
      border: 0;
      cursor: pointer;
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(54, 214, 160, 0.12);
      color: var(--text);
      font-weight: 700;
      letter-spacing: 0.01em;
    }
    .link-button:hover {
      background: rgba(54, 214, 160, 0.2);
    }
    .muted {
      color: var(--muted);
    }
    .pill-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .pill {
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid rgba(83, 97, 96, 0.08);
      color: var(--muted);
      font-size: 0.88rem;
    }
    .pill strong {
      color: var(--text);
    }
    .player-modal {
      position: fixed;
      inset: 0;
      display: none;
      place-items: center;
      background: rgba(0, 0, 0, 0.65);
      backdrop-filter: blur(12px);
      padding: 18px;
      z-index: 30;
    }
    .player-modal.open {
      display: grid;
    }
    .player-shell {
      width: min(1200px, 100%);
      background: rgba(255, 252, 248, 0.98);
      border: 1px solid rgba(83, 97, 96, 0.08);
      border-radius: 24px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .player-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 18px;
      border-bottom: 1px solid rgba(83, 97, 96, 0.08);
    }
    .player-title {
      font-size: 1rem;
      font-weight: 700;
      word-break: break-word;
    }
    .close-button {
      appearance: none;
      border: 0;
      background: rgba(25, 48, 51, 0.08);
      color: var(--text);
      border-radius: 999px;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 700;
    }
    .player-frame {
      display: block;
      width: 100%;
      height: min(72vh, 780px);
      border: 0;
      background: #f1ebe2;
    }
    @media (max-width: 760px) {
      .hero-brand {
        align-items: flex-start;
      }
      .hero-logo {
        width: 176px;
      }
      .hero-side {
        justify-items: start;
      }
      .hero-meta {
        text-align: left;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-top">
        <div class="hero-brand">
          <img class="hero-logo" src="https://cdn-liveutv.pressidium.com/wp-content/uploads/2024/01/Live-and-Ulimted-Light-Background-V2.png" alt="LiveU logo">
          <div>
            <h1>FTP Sync</h1>
          </div>
        </div>
        <div class="hero-side">
          <div class="badge"><span class="dot" id="state-dot"></span><span id="state-badge">Starting</span></div>
          <div class="hero-meta">
            <span id="last-poll-inline">Last poll: -</span>
            <span id="next-poll-inline">Next poll: -</span>
          </div>
        </div>
      </div>
    </section>

    <section class="card">
      <h2>Session Snapshot</h2>
      <div class="stats-grid">
        <div class="mini"><div class="label">Files Downloaded</div><div class="value" id="files-downloaded">0</div></div>
        <div class="mini"><div class="label">Data Downloaded</div><div class="value" id="bytes-downloaded">0 B</div></div>
        <div class="mini"><div class="label">Failures</div><div class="value" id="failures">0</div></div>
        <div class="mini"><div class="label">Skipped</div><div class="value" id="skipped">0</div></div>
        <div class="mini"><div class="label">Uptime</div><div class="value" id="uptime">00:00</div></div>
        <div class="mini"><div class="label">Idle For</div><div class="value" id="idle-for">-</div></div>
      </div>
    </section>

    <section class="card">
      <h2>Current Transfer</h2>
      <div class="progress-wrap">
        <div class="value" id="current-file">No active transfer</div>
        <div class="bar"><div class="bar-fill" id="bar-fill"></div></div>
        <div class="progress-meta">
          <span id="progress-left">Waiting for work</span>
          <span id="progress-right">-</span>
        </div>
        <div class="pill-row">
          <div class="pill"><strong>Last Success:</strong> <span id="last-success">-</span></div>
          <div class="pill"><strong>Last File:</strong> <span id="last-file">-</span></div>
          <div class="pill"><strong>Last Error:</strong> <span id="last-error">-</span></div>
        </div>
      </div>
    </section>

    <section class="card">
      <h2>Recent Clips</h2>
      <div class="clip-list" id="clip-list"></div>
    </section>
  </div>

  <div class="player-modal" id="player-modal">
    <div class="player-shell">
      <div class="player-head">
        <div class="player-title" id="player-title">Clip preview</div>
        <button class="close-button" id="close-player" type="button">Close</button>
      </div>
      <iframe class="player-frame" id="player-frame" allow="autoplay; fullscreen"></iframe>
    </div>
  </div>

  <script>
    const byId = (id) => document.getElementById(id);

    function formatBytes(bytes) {
      if (bytes === null || bytes === undefined) return "-";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let value = Number(bytes);
      let index = 0;
      while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
      }
      return `${index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
    }

    function formatClock(ts) {
      if (!ts) return "-";
      return new Date(ts * 1000).toLocaleTimeString();
    }

    function formatDuration(seconds) {
      if (seconds === null || seconds === undefined) return "-";
      const total = Math.max(0, Math.floor(seconds));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
      return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }

    function stateColor(state) {
      switch ((state || "").toLowerCase()) {
        case "idle": return "var(--idle)";
        case "retry": return "var(--warn)";
        case "cleanup": return "var(--accent-2)";
        case "ready": return "var(--accent)";
        case "polling": return "var(--accent-2)";
        case "downloading": return "var(--accent)";
        default: return "var(--accent)";
      }
    }

    function setText(id, value) {
      byId(id).textContent = value ?? "-";
    }

    function isPlayableMp4(filename) {
      return !!filename && filename.toLowerCase().endsWith(".mp4");
    }

    function openPlayer(filename) {
      if (!isPlayableMp4(filename)) return;
      byId("player-title").textContent = filename;
      byId("player-frame").src = `/player?file=${encodeURIComponent(filename)}`;
      byId("player-modal").classList.add("open");
    }

    function closePlayer() {
      byId("player-modal").classList.remove("open");
      byId("player-frame").src = "about:blank";
    }

    function renderClips(clips) {
      const host = byId("clip-list");
      host.innerHTML = "";
      if (!clips.length) {
        host.innerHTML = '<div class="clip-card"><div class="clip-meta">No MP4 clips yet.</div></div>';
        return;
      }
      clips.forEach((clip) => {
        const metadata = clip.metadata || {};
        const el = document.createElement("div");
        el.className = "clip-card";
        const cameraValue = metadata.serial_number
          ? `${metadata.camera || "-"}<span class="subv">SN ${metadata.serial_number}</span>`
          : (metadata.camera || "-");
        const action = isPlayableMp4(clip.filename)
          ? `<div class="clip-action"><button class="link-button" data-file="${clip.filename}">Play clip</button></div>`
          : "";
        el.innerHTML = `
          <div class="clip-head">
            <span class="clip-title">${clip.filename}</span>
            <span class="muted">${clip.downloaded_at ? formatClock(clip.downloaded_at) : "metadata only"}</span>
          </div>
          <div class="clip-meta">${clip.downloaded ? `${formatBytes(clip.size_bytes)} downloaded` : "Waiting for MP4 download"}${metadata.xml_filename ? ` | XML ${metadata.xml_filename}` : ""}</div>
          <div class="clip-grid">
            <div class="clip-cell"><span class="k">Camera</span><span class="v">${cameraValue}</span></div>
            <div class="clip-cell"><span class="k">Created</span><span class="v">${metadata.created || "-"}</span></div>
            <div class="clip-cell"><span class="k">Resolution / FPS</span><span class="v">${metadata.resolution_fps || "-"}</span></div>
            <div class="clip-cell"><span class="k">Duration</span><span class="v">${metadata.duration || "-"}</span></div>
            <div class="clip-cell"><span class="k">Start TC</span><span class="v">${metadata.timecode || "-"}</span></div>
            <div class="clip-cell"><span class="k">Gamma / Color</span><span class="v">${metadata.gamma_color || "-"}</span></div>
          </div>
          ${action}
        `;
        const button = el.querySelector("[data-file]");
        if (button) {
          button.addEventListener("click", () => openPlayer(button.dataset.file));
        }
        host.appendChild(el);
      });
    }

    function render(data) {
      const runtime = data.runtime;
      const config = data.config;
      const now = data.generated_at;
      const state = runtime.state || "starting";
      byId("state-dot").style.color = stateColor(state);
      byId("state-dot").style.background = stateColor(state);
      setText("state-badge", state);
      setText("last-poll-inline", `Last poll: ${formatClock(runtime.last_poll_at)}`);
      setText("next-poll-inline", `Next poll: ${runtime.next_check_at ? formatDuration(runtime.next_check_at - now) : "-"}`);
      setText("files-downloaded", runtime.files_downloaded);
      setText("bytes-downloaded", formatBytes(runtime.bytes_downloaded));
      setText("failures", runtime.failures);
      setText("skipped", runtime.skipped);
      setText("uptime", formatDuration(now - data.started_at));
      setText("idle-for", runtime.idle_since ? formatDuration(now - runtime.idle_since) : "-");
      setText("last-success", formatClock(runtime.last_success_at));
      const lastFile = runtime.last_download_name || "-";
      const lastFileEl = byId("last-file");
      lastFileEl.textContent = lastFile;
      lastFileEl.style.cursor = isPlayableMp4(runtime.last_download_name) ? "pointer" : "default";
      lastFileEl.style.textDecoration = isPlayableMp4(runtime.last_download_name) ? "underline" : "none";
      lastFileEl.onclick = isPlayableMp4(runtime.last_download_name) ? () => openPlayer(runtime.last_download_name) : null;
      setText("last-error", runtime.last_error || "-");

      const current = runtime.current_download;
      if (current) {
        setText("current-file", current.filename);
        setText("progress-left", `${(current.percent || 0).toFixed(2)}% | ${formatBytes(current.downloaded)}/${formatBytes(current.total_size)}`);
        setText("progress-right", `${formatBytes(current.speed_bytes)}/s | ETA ${formatDuration(current.eta_seconds || 0)}`);
        byId("bar-fill").style.width = `${Math.max(0, Math.min(100, current.percent || 0))}%`;
      } else {
        setText("current-file", "No active transfer");
        setText("progress-left", "Waiting for work");
        setText("progress-right", runtime.state ? `State: ${runtime.state}` : "-");
        byId("bar-fill").style.width = "0%";
      }

      renderClips(data.clips || []);
    }

    byId("close-player").addEventListener("click", closePlayer);
    byId("player-modal").addEventListener("click", (event) => {
      if (event.target === byId("player-modal")) closePlayer();
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closePlayer();
    });

    async function refresh() {
      try {
        const response = await fetch("/api/status", { cache: "no-store" });
        const data = await response.json();
        render(data);
      } catch (error) {
        setText("state-badge", "offline");
      }
    }

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>"""


def start_web_server(
    host: str,
    port: int,
    dashboard_state: DashboardState,
    download_dir: Path,
) -> ThreadingHTTPServer:
    dashboard_html = build_dashboard_html().encode("utf-8")
    download_root = download_dir.resolve()

    def resolve_media_path(raw_filename: str) -> Optional[Path]:
        candidate = (download_root / raw_filename).resolve()
        if download_root == candidate or download_root not in candidate.parents:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def serve_file(handler: BaseHTTPRequestHandler, path: Path) -> None:
        file_size = path.stat().st_size
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        range_header = handler.headers.get("Range")

        start = 0
        end = file_size - 1
        status = 200

        if range_header and range_header.startswith("bytes="):
            range_spec = range_header.split("=", 1)[1]
            start_text, _, end_text = range_spec.partition("-")
            if start_text:
                start = int(start_text)
            if end_text:
                end = int(end_text)
            end = min(end, file_size - 1)
            status = 206

        length = max(0, end - start + 1)
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Length", str(length))
        if status == 206:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        handler.end_headers()

        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    handler.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def build_player_html(filename: str) -> bytes:
        safe_filename = filename.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        media_url = f"/media/{quote(filename)}"
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_filename}</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: radial-gradient(circle at top, rgba(65, 203, 177, 0.2), transparent 30%), #081017;
      color: #eef8f4;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }}
    .frame {{
      width: min(1100px, calc(100vw - 24px));
      background: rgba(10, 24, 33, 0.92);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 24px;
      padding: 18px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.35);
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 1.2rem;
      letter-spacing: -0.02em;
      word-break: break-word;
    }}
    video {{
      width: 100%;
      max-height: 78vh;
      border-radius: 18px;
      background: #000;
    }}
  </style>
</head>
<body>
  <div class="frame">
    <h1>{safe_filename}</h1>
    <video controls autoplay playsinline src="{media_url}"></video>
  </div>
</body>
</html>""".encode("utf-8")

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path in {"/", "/index.html"}:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(dashboard_html)))
                self.end_headers()
                self.wfile.write(dashboard_html)
                return

            if parsed.path == "/api/status":
                payload = json.dumps(dashboard_state.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if parsed.path == "/player":
                filename = parse_qs(parsed.query).get("file", [""])[0]
                media_path = resolve_media_path(unquote(filename))
                if media_path is None:
                    self.send_error(404, "Media not found")
                    return
                player_html = build_player_html(media_path.name)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(player_html)))
                self.end_headers()
                self.wfile.write(player_html)
                return

            if parsed.path.startswith("/media/"):
                filename = unquote(parsed.path[len("/media/"):])
                media_path = resolve_media_path(filename)
                if media_path is None:
                    self.send_error(404, "Media not found")
                    return
                serve_file(self, media_path)
                return

            self.send_error(404, "Not Found")

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, name="ftp-watcher-web", daemon=True)
    thread.start()
    return server


class ProgressTracker:
    def __init__(
        self,
        filename: str,
        total_size: int,
        enabled: bool,
        progress_callback: Optional[Any] = None,
    ) -> None:
        self.filename = filename
        self.total_size = total_size
        self.enabled = enabled
        self.progress_callback = progress_callback
        self.downloaded = 0
        self.start_time = time.time()
        self.last_render = 0.0
        self.ui = TerminalUI(enabled)
        self.smoothed_speed: Optional[float] = None

    def update(self, chunk: bytes) -> None:
        self.downloaded += len(chunk)
        now = time.time()
        elapsed = max(now - self.start_time, 0.001)
        instant_speed = self.downloaded / elapsed
        if self.smoothed_speed is None:
            self.smoothed_speed = instant_speed
        else:
            self.smoothed_speed = (self.smoothed_speed * 0.7) + (instant_speed * 0.3)
        speed = self.smoothed_speed

        if self.progress_callback is not None:
            eta_seconds = (
                (self.total_size - self.downloaded) / speed
                if self.total_size > 0 and speed > 0
                else None
            )
            self.progress_callback(
                filename=self.filename,
                downloaded=self.downloaded,
                total_size=self.total_size,
                speed_bytes=speed,
                eta_seconds=eta_seconds,
            )

        if not self.enabled:
            return

        if now - self.last_render < 0.1 and self.downloaded < self.total_size:
            return
        self.last_render = now

        width = max(72, self.ui.terminal_width())

        if self.total_size > 0:
            percent = min(self.downloaded / self.total_size, 1.0)
            stats = (
                f"{percent * 100:6.2f}%"
                f" | {format_bytes(self.downloaded)}/{format_bytes(self.total_size)}"
                f" | {format_bytes(speed)}/s"
                f" | ETA {format_duration((self.total_size - self.downloaded) / speed if speed > 0 else 0)}"
            )
            min_bar_width = 12
            reserved_width = len(stats) + min_bar_width + 4
            name_width = min(24, max(16, width - reserved_width))
            bar_width = max(min_bar_width, width - (name_width + len(stats) + 4))
            label = pad_text(shorten_filename(self.filename, name_width), name_width)
            filled = int(bar_width * percent)
            if filled >= bar_width:
                bar = "=" * bar_width
            else:
                bar = "=" * filled + ">" + "." * max(0, bar_width - filled - 1)
            line = (
                f"\r{self.ui.style(label, self.ui.BOLD)} "
                f"[{self.ui.style(bar, self.ui.CYAN)}] "
                f"{stats}"
            )
        else:
            stats = f"{format_bytes(self.downloaded)} {format_bytes(speed)}/s"
            name_width = min(24, max(16, width - len(stats) - 20))
            label = pad_text(shorten_filename(self.filename, name_width), name_width)
            line = (
                f"\r{self.ui.style(label, self.ui.BOLD)} "
                f"{stats} "
                f"{self.ui.style('(size unavailable)', self.ui.DIM)}"
            )

        sys.stdout.write(line)
        sys.stdout.flush()

    def finish(self) -> None:
        if self.enabled:
            sys.stdout.write("\n")
            sys.stdout.flush()


def download_file(
    ftp: ftplib.FTP,
    remote_dir: str,
    filename: str,
    local_dir: Path,
    temp_suffix: str,
    total_size: int,
    show_progress: bool,
    progress_callback: Optional[Any] = None,
) -> Path:
    ftp.cwd(remote_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    final_path = local_dir / filename
    temp_path = local_dir / f"{filename}{temp_suffix}"

    tracker = ProgressTracker(
        filename=filename,
        total_size=total_size,
        enabled=show_progress,
        progress_callback=progress_callback,
    )

    with temp_path.open("wb") as f:
        def callback(chunk: bytes) -> None:
            f.write(chunk)
            tracker.update(chunk)

        ftp.retrbinary(f"RETR {filename}", callback, blocksize=64 * 1024)

    tracker.finish()
    temp_path.replace(final_path)
    return final_path


def delete_remote_file(ftp: ftplib.FTP, remote_dir: str, filename: str) -> None:
    ftp.cwd(remote_dir)
    ftp.delete(filename)


def get_ui_mode(config: configparser.ConfigParser) -> str:
    raw_mode = config["watcher"].get("ui_mode", "rich").strip().lower()
    if raw_mode in {"rich", "minimal", "off"}:
        return raw_mode
    return "rich"


def log_processing_error(filename: str, exc: Exception, debug_tracebacks: bool) -> None:
    if debug_tracebacks:
        logging.exception("Failed processing %s: %s", filename, exc)
    else:
        logging.error("Failed processing %s: %s", filename, exc)


def log_polling_error(exc: Exception, debug_tracebacks: bool) -> None:
    if debug_tracebacks:
        logging.exception("FTP polling failed: %s", exc)
    else:
        logging.error("FTP polling failed: %s", exc)


def build_status_message(
    stats: SessionStats,
    now: float,
    next_check_in: int,
    extra: str,
) -> str:
    parts = [
        "FTP live",
        f"last poll {format_timestamp(stats.last_poll_at or now)}",
        f"next {format_duration(next_check_in)}",
        f"ok {stats.files_downloaded}",
        f"fail {stats.failures}",
        f"data {format_bytes(stats.bytes_downloaded)}",
        f"up {format_uptime(now - stats.started_at)}",
    ]
    if stats.idle_since is not None:
        parts.append(f"idle {format_uptime(now - stats.idle_since)}")
    if stats.last_success_at is not None:
        parts.append(f"last ok {format_timestamp(stats.last_success_at)}")
    if stats.last_download_name != "-":
        parts.append(f"last {shorten_filename(stats.last_download_name, 18)}")
    parts.append(extra)
    return " | ".join(parts)


def render_sleep_status(
    ui: TerminalUI,
    stats: SessionStats,
    duration_seconds: int,
    state: str,
    tone: str,
    extra: str,
) -> None:
    if not ui.enabled:
        time.sleep(duration_seconds)
        return

    spinner_frames = "|/-\\"
    end_time = time.time() + duration_seconds
    frame = 0

    while True:
        now = time.time()
        remaining = max(0, int(end_time - now + 0.999))
        status = build_status_message(
            stats=stats,
            now=now,
            next_check_in=remaining,
            extra=f"{spinner_frames[frame % len(spinner_frames)]} {extra}",
        )
        ui.render_status_line(state=state, message=status, tone=tone)
        if remaining <= 0:
            break
        frame += 1
        time.sleep(min(1.0, end_time - now))


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.ini"
    config = load_config(config_path)

    log_to_file = get_bool(config, "watcher", "log_to_file", False)
    setup_logging(log_to_file=log_to_file)

    remote_dir = config["ftp"].get("remote_dir", "/")
    download_dir = Path(config["local"].get("download_dir", "./downloads")).resolve()
    poll_interval = get_int(config, "watcher", "poll_interval_seconds", 10)
    state_file = Path(config["watcher"].get("state_file", "./downloaded_files.json")).resolve()
    temp_suffix = config["watcher"].get("temp_suffix", ".part")
    delete_after_download = get_bool(config, "watcher", "delete_remote_after_download", True)
    debug_tracebacks = get_bool(config, "watcher", "debug_tracebacks", False)
    ui_mode = get_ui_mode(config)
    show_progress = get_bool(config, "watcher", "show_progress", True) and is_interactive() and ui_mode != "off"
    ui = TerminalUI(show_progress, rich=(ui_mode == "rich"))
    web_enabled = get_bool(config, "web", "enabled", True) if "web" in config else True
    web_host = config["web"].get("host", "127.0.0.1") if "web" in config else "127.0.0.1"
    web_port = get_int(config, "web", "port", 8080) if "web" in config else 8080

    downloaded_files = load_state(state_file)
    session = SessionStats(started_at=time.time())
    dashboard = DashboardState(
        {
            "ftp_host": config["ftp"]["host"],
            "ftp_port": get_int(config, "ftp", "port", 21),
            "remote_dir": remote_dir,
            "download_dir": str(download_dir),
            "poll_interval_seconds": poll_interval,
            "delete_remote_after_download": delete_after_download,
            "use_tls": get_bool(config, "ftp", "use_tls", False),
            "passive": get_bool(config, "ftp", "passive", True),
            "ui_mode": ui_mode,
            "web_enabled": web_enabled,
            "web_host": web_host,
            "web_port": web_port,
        }
    )
    web_server: Optional[ThreadingHTTPServer] = None
    if web_enabled:
        try:
            web_server = start_web_server(web_host, web_port, dashboard, download_dir)
            dashboard.add_event("info", "Dashboard online", f"http://{web_host}:{web_port}")
        except OSError as exc:
            web_enabled = False
            dashboard.config_data["web_enabled"] = False
            dashboard.update_runtime(
                state="warn",
                tone="warn",
                status_message=f"Web dashboard disabled: {exc}",
            )
            dashboard.add_event("error", "Dashboard failed", str(exc))
            logging.warning("Web dashboard disabled: %s", exc)

    logging.info("Watching FTP folder: %s", remote_dir)
    logging.info("Downloading to: %s", download_dir)
    logging.info("Delete remote after success: %s", delete_after_download)
    logging.info("Interactive progress display: %s", show_progress)
    logging.info("Web dashboard: %s", f"http://{web_host}:{web_port}" if web_enabled else "disabled")
    dashboard.update_runtime(
        state="ready",
        tone="success",
        status_message=f"Watcher started | dashboard {'enabled' if web_enabled else 'disabled'}",
        files_downloaded=session.files_downloaded,
        bytes_downloaded=session.bytes_downloaded,
        failures=session.failures,
        skipped=session.skipped,
    )

    ui.print_header(
        "FTP Watcher",
        [
            ("Remote", remote_dir),
            ("Local", str(download_dir)),
            ("Interval", f"{poll_interval}s"),
            ("Cleanup", "delete remote files" if delete_after_download else "keep remote files"),
            ("State", str(state_file)),
            ("UI", ui_mode),
            ("Web", f"http://{web_host}:{web_port}" if web_enabled else "disabled"),
        ],
    )
    ui.print_progress_legend()

    try:
        while True:
            ftp = None
            try:
                cycle_start = time.time()
                files_processed = 0
                skipped_this_poll = 0
                session.last_poll_at = cycle_start
                dashboard.update_runtime(
                    state="polling",
                    tone="info",
                    status_message="Connecting to FTP",
                    last_poll_at=cycle_start,
                    next_check_at=None,
                    idle_since=session.idle_since,
                    last_success_at=session.last_success_at,
                    last_download_name=session.last_download_name if session.last_download_name != "-" else None,
                    last_error=session.last_error if session.last_error != "-" else None,
                    files_downloaded=session.files_downloaded,
                    bytes_downloaded=session.bytes_downloaded,
                    failures=session.failures,
                    skipped=session.skipped,
                )
                ui.render_status_line(
                    state="POLLING",
                    message=build_status_message(
                        stats=session,
                        now=cycle_start,
                        next_check_in=0,
                        extra="connecting to FTP",
                    ),
                    tone="info",
                )
                ftp = connect_ftp(config)
                dashboard.update_runtime(
                    state="polling",
                    tone="info",
                    status_message="Connected to FTP, listing files",
                )
                ui.render_status_line(
                    state="POLLING",
                    message=build_status_message(
                        stats=session,
                        now=time.time(),
                        next_check_in=0,
                        extra="listing remote files",
                    ),
                    tone="info",
                )
                remote_files = list_remote_files(ftp, remote_dir)

                for filename, (size, modified) in remote_files.items():
                    file_key = build_file_key(filename, size, modified)

                    if file_key in downloaded_files:
                        skipped_this_poll += 1
                        continue

                    ui.clear_status_line()
                    logging.info("Detected | %s | %s", filename, format_bytes(size))
                    dashboard.add_event("info", "Detected", f"{filename} | {format_bytes(size)}", filename=filename)
                    dashboard.update_runtime(
                        state="downloading",
                        tone="info",
                        status_message=f"Downloading {filename}",
                        last_error=None,
                    )

                    try:
                        download_started = time.time()
                        local_path = download_file(
                            ftp=ftp,
                            remote_dir=remote_dir,
                            filename=filename,
                            local_dir=download_dir,
                            temp_suffix=temp_suffix,
                            total_size=size,
                            show_progress=show_progress,
                            progress_callback=dashboard.set_current_download,
                        )

                        duration = max(time.time() - download_started, 0.001)
                        avg_speed = size / duration if size > 0 else 0
                        ui.clear_status_line()
                        logging.info(
                            "Done | %s | %s | %s | %s/s | %s",
                            filename,
                            format_bytes(size),
                            format_duration(duration),
                            format_bytes(avg_speed),
                            local_path,
                        )
                        dashboard.add_event(
                            "success",
                            "Downloaded",
                            f"{filename} | {format_bytes(size)} | {format_duration(duration)} | {format_bytes(avg_speed)}/s",
                            filename=filename,
                        )
                        if filename.lower().endswith(".xml"):
                            dashboard.attach_xml_metadata(local_path)
                        elif filename.lower().endswith(".mp4"):
                            dashboard.mark_clip_downloaded(filename, size, local_path)

                        if delete_after_download:
                            dashboard.update_runtime(
                                state="cleanup",
                                tone="info",
                                status_message=f"Deleting remote copy of {filename}",
                            )
                            ui.render_status_line(
                                state="CLEANUP",
                                message=build_status_message(
                                    stats=session,
                                    now=time.time(),
                                    next_check_in=0,
                                    extra=f"deleting remote {shorten_filename(filename, 18)}",
                                ),
                                tone="info",
                            )
                            delete_remote_file(ftp, remote_dir, filename)
                            ui.clear_status_line()
                            logging.info("Deleted remote file: %s", filename)
                            dashboard.add_event("info", "Remote deleted", filename, filename=filename)

                        downloaded_files.add(file_key)
                        save_state(state_file, downloaded_files)
                        files_processed += 1
                        session.files_downloaded += 1
                        session.bytes_downloaded += size
                        session.last_success_at = time.time()
                        session.last_download_name = filename
                        session.last_error = "-"
                        session.idle_since = None
                        dashboard.clear_current_download()
                        dashboard.update_runtime(
                            state="ready",
                            tone="success",
                            status_message=f"Downloaded {filename}",
                            last_success_at=session.last_success_at,
                            last_download_name=filename,
                            idle_since=None,
                            files_downloaded=session.files_downloaded,
                            bytes_downloaded=session.bytes_downloaded,
                            failures=session.failures,
                            skipped=session.skipped,
                            last_error=None,
                        )

                    except Exception as exc:
                        ui.clear_status_line()
                        session.failures += 1
                        session.last_error = str(exc)
                        dashboard.clear_current_download()
                        dashboard.update_runtime(
                            state="retry",
                            tone="warn",
                            status_message=f"Failed processing {filename}: {exc}",
                            failures=session.failures,
                            last_error=str(exc),
                        )
                        dashboard.add_event("error", "Processing error", f"{filename} | {exc}")
                        log_processing_error(filename, exc, debug_tracebacks)

                session.skipped += skipped_this_poll

                if show_progress:
                    if files_processed == 0:
                        if session.idle_since is None:
                            session.idle_since = cycle_start
                        dashboard.update_runtime(
                            state="idle",
                            tone="idle",
                            status_message="FTP live, waiting for new files",
                            next_check_at=time.time() + poll_interval,
                            idle_since=session.idle_since,
                            files_downloaded=session.files_downloaded,
                            bytes_downloaded=session.bytes_downloaded,
                            failures=session.failures,
                            skipped=session.skipped,
                        )
                        render_sleep_status(
                            ui=ui,
                            stats=session,
                            duration_seconds=poll_interval,
                            state="IDLE",
                            tone="idle",
                            extra=f"no new files, skipped {skipped_this_poll}",
                        )
                    else:
                        dashboard.update_runtime(
                            state="ready",
                            tone="success",
                            status_message=f"Processed {files_processed} file(s)",
                            next_check_at=time.time() + poll_interval,
                            idle_since=session.idle_since,
                            files_downloaded=session.files_downloaded,
                            bytes_downloaded=session.bytes_downloaded,
                            failures=session.failures,
                            skipped=session.skipped,
                        )
                        render_sleep_status(
                            ui=ui,
                            stats=session,
                            duration_seconds=poll_interval,
                            state="READY",
                            tone="success",
                            extra=f"processed {files_processed}, skipped {skipped_this_poll}",
                        )
                else:
                    dashboard.update_runtime(
                        state="idle" if files_processed == 0 else "ready",
                        tone="idle" if files_processed == 0 else "success",
                        status_message="Waiting for next poll",
                        next_check_at=time.time() + poll_interval,
                        idle_since=session.idle_since,
                        files_downloaded=session.files_downloaded,
                        bytes_downloaded=session.bytes_downloaded,
                        failures=session.failures,
                        skipped=session.skipped,
                    )
                    time.sleep(poll_interval)

            except Exception as exc:
                ui.clear_status_line()
                session.failures += 1
                session.last_error = str(exc)
                dashboard.clear_current_download()
                dashboard.update_runtime(
                    state="retry",
                    tone="warn",
                    status_message=f"FTP polling failed: {exc}",
                    failures=session.failures,
                    last_error=str(exc),
                    next_check_at=time.time() + poll_interval,
                )
                dashboard.add_event("error", "Polling error", str(exc))
                log_polling_error(exc, debug_tracebacks)
                if show_progress:
                    render_sleep_status(
                        ui=ui,
                        stats=session,
                        duration_seconds=poll_interval,
                        state="RETRY",
                        tone="warn",
                        extra="waiting to reconnect",
                    )
                else:
                    time.sleep(poll_interval)

            finally:
                if ftp is not None:
                    try:
                        ftp.quit()
                    except Exception:
                        try:
                            ftp.close()
                        except Exception:
                            pass

    except KeyboardInterrupt:
        ui.clear_status_line()
        dashboard.update_runtime(state="stopped", tone="warn", status_message="Stopped by user")
        dashboard.add_event("warn", "Stopped", "Watcher stopped by user")
        logging.info("Stopped by user.")
        return 0
    finally:
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())

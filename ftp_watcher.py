#!/usr/bin/env python3
import configparser
import ftplib
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


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


def format_timestamp(timestamp: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


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


class ProgressTracker:
    def __init__(self, filename: str, total_size: int, enabled: bool) -> None:
        self.filename = filename
        self.total_size = total_size
        self.enabled = enabled
        self.downloaded = 0
        self.start_time = time.time()
        self.last_render = 0.0
        self.ui = TerminalUI(enabled)
        self.smoothed_speed: Optional[float] = None

    def update(self, chunk: bytes) -> None:
        self.downloaded += len(chunk)

        if not self.enabled:
            return

        now = time.time()
        if now - self.last_render < 0.1 and self.downloaded < self.total_size:
            return
        self.last_render = now

        elapsed = max(now - self.start_time, 0.001)
        instant_speed = self.downloaded / elapsed
        if self.smoothed_speed is None:
            self.smoothed_speed = instant_speed
        else:
            self.smoothed_speed = (self.smoothed_speed * 0.7) + (instant_speed * 0.3)
        speed = self.smoothed_speed
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
) -> Path:
    ftp.cwd(remote_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    final_path = local_dir / filename
    temp_path = local_dir / f"{filename}{temp_suffix}"

    tracker = ProgressTracker(filename, total_size, show_progress)

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

    downloaded_files = load_state(state_file)
    session = SessionStats(started_at=time.time())

    logging.info("Watching FTP folder: %s", remote_dir)
    logging.info("Downloading to: %s", download_dir)
    logging.info("Delete remote after success: %s", delete_after_download)
    logging.info("Interactive progress display: %s", show_progress)

    ui.print_header(
        "FTP Watcher",
        [
            ("Remote", remote_dir),
            ("Local", str(download_dir)),
            ("Interval", f"{poll_interval}s"),
            ("Cleanup", "delete remote files" if delete_after_download else "keep remote files"),
            ("State", str(state_file)),
            ("UI", ui_mode),
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

                        if delete_after_download:
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

                        downloaded_files.add(file_key)
                        save_state(state_file, downloaded_files)
                        files_processed += 1
                        session.files_downloaded += 1
                        session.bytes_downloaded += size
                        session.last_success_at = time.time()
                        session.last_download_name = filename
                        session.last_error = "-"
                        session.idle_since = None

                    except Exception as exc:
                        ui.clear_status_line()
                        session.failures += 1
                        session.last_error = str(exc)
                        log_processing_error(filename, exc, debug_tracebacks)

                session.skipped += skipped_this_poll

                if show_progress:
                    if files_processed == 0:
                        if session.idle_since is None:
                            session.idle_since = cycle_start
                        render_sleep_status(
                            ui=ui,
                            stats=session,
                            duration_seconds=poll_interval,
                            state="IDLE",
                            tone="idle",
                            extra=f"no new files, skipped {skipped_this_poll}",
                        )
                    else:
                        render_sleep_status(
                            ui=ui,
                            stats=session,
                            duration_seconds=poll_interval,
                            state="READY",
                            tone="success",
                            extra=f"processed {files_processed}, skipped {skipped_this_poll}",
                        )
                else:
                    time.sleep(poll_interval)

            except Exception as exc:
                ui.clear_status_line()
                session.failures += 1
                session.last_error = str(exc)
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
        logging.info("Stopped by user.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

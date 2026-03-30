#!/usr/bin/env python3
import configparser
import ftplib
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple


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

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

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
        if not self.enabled:
            return
        print()
        print(self.style(self.rule(title), self.BOLD, self.CYAN))
        for key, value in rows:
            print(
                f"{self.style(key.rjust(12), self.BOLD)} : "
                f"{self.style(value, self.GREEN)}"
            )
        print(self.style(self.rule(), self.DIM))

    def print_status(self, message: str, tone: str = "info") -> None:
        if not self.enabled:
            return

        color = {
            "info": self.CYAN,
            "idle": self.BLUE,
            "success": self.GREEN,
            "warn": self.YELLOW,
        }.get(tone, self.CYAN)

        width = max(60, self.terminal_width())
        content = shorten_text(message, width - 4)
        line = f"{self.style('>>', self.BOLD, color)} {content}"
        print(line.ljust(width))


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
    hours, mins = divmod(minutes, 60)

    if hours:
        return f"{hours:d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def shorten_text(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width <= 3:
        return text[:max_width]
    return f"{text[:max_width - 3]}..."


def format_timestamp(timestamp: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


class ProgressTracker:
    def __init__(self, filename: str, total_size: int, enabled: bool) -> None:
        self.filename = filename
        self.total_size = total_size
        self.enabled = enabled
        self.downloaded = 0
        self.start_time = time.time()
        self.last_render = 0.0
        self.ui = TerminalUI(enabled)

    def update(self, chunk: bytes) -> None:
        self.downloaded += len(chunk)

        if not self.enabled:
            return

        now = time.time()
        if now - self.last_render < 0.1 and self.downloaded < self.total_size:
            return
        self.last_render = now

        elapsed = max(now - self.start_time, 0.001)
        speed = self.downloaded / elapsed
        width = max(80, self.ui.terminal_width())
        name_width = min(30, max(18, width // 4))
        size_width = 18
        speed_width = 14
        eta_width = 10
        percent_width = 8
        fixed_width = name_width + size_width + speed_width + eta_width + percent_width + 14
        bar_width = max(10, width - fixed_width)
        label = shorten_text(self.filename, name_width).ljust(name_width)

        if self.total_size > 0:
            percent = min(self.downloaded / self.total_size, 1.0)
            filled = int(bar_width * percent)
            if filled >= bar_width:
                bar = "=" * bar_width
            else:
                bar = "=" * filled + ">" + "." * max(0, bar_width - filled - 1)
            eta_seconds = (self.total_size - self.downloaded) / speed if speed > 0 else 0
            line = (
                f"\r{self.ui.style(label, self.ui.BOLD)} "
                f"[{self.ui.style(bar, self.CYAN)}] "
                f"{percent * 100:6.2f}% "
                f"{format_bytes(self.downloaded).rjust(8)}/{format_bytes(self.total_size).ljust(8)} "
                f"{format_bytes(speed).rjust(8)}/s "
                f"ETA {format_duration(eta_seconds).rjust(5)}"
            )
        else:
            line = (
                f"\r{self.ui.style(label, self.ui.BOLD)} "
                f"{format_bytes(self.downloaded).rjust(8)} "
                f"{format_bytes(speed).rjust(8)}/s "
                f"{self.ui.style('(size unavailable)', self.DIM)}"
            )

        line = line[:width]
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
    show_progress = get_bool(config, "watcher", "show_progress", True) and is_interactive()
    ui = TerminalUI(show_progress)

    downloaded_files = load_state(state_file)

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
        ],
    )

    try:
        poll_count = 0
        while True:
            ftp = None
            try:
                poll_count += 1
                cycle_start = time.time()
                files_processed = 0
                ftp = connect_ftp(config)
                remote_files = list_remote_files(ftp, remote_dir)

                for filename, (size, modified) in remote_files.items():
                    file_key = build_file_key(filename, size, modified)

                    if file_key in downloaded_files:
                        continue

                    logging.info("New file detected: %s (%s bytes)", filename, size)

                    try:
                        local_path = download_file(
                            ftp=ftp,
                            remote_dir=remote_dir,
                            filename=filename,
                            local_dir=download_dir,
                            temp_suffix=temp_suffix,
                            total_size=size,
                            show_progress=show_progress,
                        )

                        logging.info("Downloaded successfully: %s -> %s", filename, local_path)

                        if delete_after_download:
                            delete_remote_file(ftp, remote_dir, filename)
                            logging.info("Deleted remote file: %s", filename)

                        downloaded_files.add(file_key)
                        save_state(state_file, downloaded_files)
                        files_processed += 1

                    except Exception as exc:
                        logging.exception("Failed processing %s: %s", filename, exc)

                if show_progress:
                    if files_processed == 0:
                        ui.print_status(
                            (
                                f"Poll #{poll_count} at {format_timestamp(cycle_start)}: "
                                f"no new files. Next check in {poll_interval}s."
                            ),
                            tone="idle",
                        )
                    else:
                        ui.print_status(
                            (
                                f"Poll #{poll_count} complete: processed {files_processed} new "
                                f"file(s). Next check in {poll_interval}s."
                            ),
                            tone="success",
                        )
                time.sleep(poll_interval)

            except Exception as exc:
                logging.exception("FTP polling failed: %s", exc)
                if show_progress:
                    ui.print_status(
                        f"FTP polling failed. Retrying in {poll_interval}s.",
                        tone="warn",
                    )
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
        logging.info("Stopped by user.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

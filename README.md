# FTP Watcher

A Python tool that monitors a remote FTP directory and automatically downloads new files as they appear.

## Features

- **Automatic Monitoring**: Continuously polls a remote FTP directory for new files
- **Incremental Downloads**: Tracks downloaded files to avoid re-downloading
- **Interactive Terminal UI**: Startup summary, per-file progress bar, idle heartbeat, and retry status lines in interactive terminals
- **Live Status Footer**: Single-line connection state, countdown, last poll time, uptime, and transfer counters
- **Progress Tracking**: Real-time progress bar with percent, size, smoothed speed, and ETA
- **FTP/FTPS Support**: Works with standard FTP and secure FTP (TLS) connections
- **Passive Mode**: Configurable FTP passive/active modes
- **State Persistence**: Remembers downloaded files across runs using JSON
- **Logging**: Optional file logging and console output
- **Quiet-By-Default Errors**: Normal runs log concise errors; optional debug tracebacks are available
- **Temporary Files**: Uses `.part` suffix for in-progress downloads to prevent incomplete file processing
- **Auto Cleanup**: Optional remote file deletion after successful download

## Requirements

- Python 3.6+
- Standard library only (no external dependencies)

## Installation

1. Clone the repository:
```bash
git clone https://github.com/alonraif/ftp-watcher.git
cd ftp-watcher
```

2. No additional dependencies needed - uses Python standard library only!

The repository already includes a `downloads/` directory, which matches the default `download_dir` value in `config.ini`.

## Configuration

Edit `config.ini` to configure the FTP connection and behavior:

### FTP Settings
```ini
[ftp]
host = ftp.example.com          # FTP server hostname
port = 21                        # FTP port (default: 21)
user = myuser                    # FTP username
password = mypassword            # FTP password
remote_dir = /incoming           # Remote directory to monitor
use_tls = false                  # Use FTPS (TLS) connection
passive = true                   # Use passive mode
```

### Local Settings
```ini
[local]
download_dir = ./downloads       # Local directory to save downloaded files
```

### Watcher Settings
```ini
[watcher]
poll_interval_seconds = 10       # Interval between directory checks (seconds)
state_file = ./downloaded_files.json  # File to track downloaded files
temp_suffix = .part              # Suffix for incomplete downloads
delete_remote_after_download = true   # Delete file on server after download
show_progress = true             # Show interactive progress and status output in a TTY
ui_mode = rich                   # rich, minimal, or off
debug_tracebacks = false         # Show full stack traces for processing/polling errors
log_to_file = false              # Enable file logging
```

## Usage

Run the watcher:
```bash
python3 ftp_watcher.py config.ini
```

The tool will:
1. Connect to the FTP server
2. List all files in the remote directory
3. Download any new files not in the state file
4. Update the state file with downloaded file information
5. Wait for the configured poll interval
6. Repeat steps 2-5

When `show_progress = true` and the script is running in an interactive terminal, the watcher displays:

- A startup summary with remote path, local download path, poll interval, cleanup mode, and state file
- A compact legend for the progress area in `rich` UI mode
- A live per-file progress line with percent, transferred size, smoothed speed, and `ETA mm:ss`
- A single live footer with connection state, last poll time, countdown, uptime, success/failure counters, and last successful download
- A retry footer when polling fails

`ui_mode` values:

- `rich`: full header, legend, colors, progress bar, and live footer
- `minimal`: progress bar and live footer without the richer header/legend treatment
- `off`: disable interactive UI even in a TTY

## How It Works

1. **File Tracking**: Files are tracked by a combination of filename, size, and modification time, preventing duplicate downloads even if the file is re-uploaded with the same name.

2. **Incomplete Downloads**: Downloads are saved with a `.part` suffix. Upon successful completion, files are renamed to their original names.

3. **State Persistence**: Downloaded file information is stored in `downloaded_files.json`, allowing the tool to safely resume after restarts without re-downloading.

4. **Interactive Output**: In a TTY, progress is shown as a compact line such as `73.50% | 82.5 MB/112.2 MB | 11.4 MB/s | ETA 00:24`, and the footer shows state such as `IDLE`, `POLLING`, `READY`, `CLEANUP`, or `RETRY`.

5. **Session Stats**: The live footer tracks uptime, downloaded file count, downloaded data volume, failures, last poll time, and the last successful download.

6. **Error Handling**: Connection failures and download errors are logged. The tool attempts to continue operation rather than crashing. Full tracebacks can be enabled with `debug_tracebacks = true`.

## Example Workflow

```
1. You have a camera system uploading photos to an FTP server
2. Configure FTP-Watcher to monitor the `/photos` directory
3. Run: python3 ftp_watcher.py config.ini
4. Every 10 seconds, new photos are automatically downloaded to ./downloads
5. If nothing new appears, the watcher still prints a status line so you know it is running
6. Track which photos have been processed via the state file
7. Optionally delete photos from the server after download
```

## State File Format

The `downloaded_files.json` file stores file keys in the format:
```
filename|size|modification_timestamp
```

Example:
```json
[
  "photo_001.jpg|2048576|20240330120000",
  "photo_002.jpg|2097152|20240330120015"
]
```

## Troubleshooting

**Connection refused**: Check your FTP credentials and firewall settings

**Files not downloading**: 
- Verify the `remote_dir` path exists on the FTP server
- Check user permissions on the remote directory
- Ensure `download_dir` is writable locally

**No progress UI appears**:
- Confirm `show_progress = true` in `config.ini`
- Run the script in a real terminal; interactive progress is disabled when stdout is redirected or non-interactive

**Too much UI output for your environment**:
- Set `ui_mode = minimal` for a lighter interactive display
- Set `ui_mode = off` to disable the interactive dashboard entirely

**Stuck at 0%**: May indicate a large file with no size information from the FTP server

**Mixed state files between runs**: Delete `downloaded_files.json` to start fresh (files will be re-downloaded)

## License

MIT

## Author

Created for automated file monitoring from FTP servers

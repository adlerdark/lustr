"""
Logging configuration for Video Manager.

Sets up rotating log files for:
- Application logs (docker.log with rotation)
- FFmpeg logs (ffmpeg.log with rotation)

Features:
- Timestamped entries
- Automatic rotation when files get too large (10MB)
- Keeps last 5 backup files
- Retention: automatic via rotation
- Timestamped print() wrapper
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime

# Log directory
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# Log file paths
APP_LOG_FILE = os.path.join(LOG_DIR, 'docker.log')
FFMPEG_LOG_FILE = os.path.join(LOG_DIR, 'ffmpeg.log')

# Log format with timestamp
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

def setup_logger(name, log_file, level=logging.INFO):
    """
    Set up a logger with rotating file handler.
    
    Args:
        name: Logger name
        log_file: Path to log file
        level: Logging level
        
    Returns:
        Logger instance
    """
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    
    # Rotating file handler: 10MB per file, keep 5 backups
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    handler.setFormatter(formatter)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    
    # Also output to console (for Docker logs)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

# Create loggers
app_logger = setup_logger('app', APP_LOG_FILE, logging.INFO)
ffmpeg_logger = setup_logger('ffmpeg', FFMPEG_LOG_FILE, logging.DEBUG)

# Configure root logger to catch everything
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
root_console = logging.StreamHandler()
root_console.setFormatter(root_formatter)
root_file = logging.handlers.RotatingFileHandler(
    APP_LOG_FILE,
    maxBytes=10*1024*1024,
    backupCount=5
)
root_file.setFormatter(root_formatter)
root_logger.addHandler(root_console)
root_logger.addHandler(root_file)

# Configure uvicorn loggers to use our format
uvicorn_access = logging.getLogger("uvicorn.access")
uvicorn_access.handlers = []
uvicorn_access.addHandler(root_console)
uvicorn_access.addHandler(root_file)

uvicorn_error = logging.getLogger("uvicorn.error")
uvicorn_error.handlers = []
uvicorn_error.addHandler(root_console)
uvicorn_error.addHandler(root_file)

uvicorn_main = logging.getLogger("uvicorn")
uvicorn_main.handlers = []
uvicorn_main.addHandler(root_console)
uvicorn_main.addHandler(root_file)

# Timestamped print wrapper
_original_print = print
def timestamped_print(*args, **kwargs):
    """Wrapper for print() that adds timestamps."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # Convert args to strings properly to handle Unicode
    try:
        message = ' '.join(str(arg) for arg in args)
        _original_print(f"[{timestamp}]", message, **kwargs)
    except UnicodeEncodeError:
        # Fallback if Unicode issues
        _original_print(f"[{timestamp}]", *args, **kwargs)
    
# Replace built-in print
import builtins
builtins.print = timestamped_print

def get_app_logger():
    """Get the application logger."""
    return app_logger

def get_ffmpeg_logger():
    """Get the FFmpeg logger."""
    return ffmpeg_logger

# Convenience functions
def log_info(message):
    """Log info message to app log."""
    app_logger.info(message)

def log_warning(message):
    """Log warning message to app log."""
    app_logger.warning(message)

def log_error(message):
    """Log error message to app log."""
    app_logger.error(message)

def log_debug(message):
    """Log debug message to app log."""
    app_logger.debug(message)

def log_ffmpeg(message):
    """Log FFmpeg-related message to separate log."""
    ffmpeg_logger.info(message)

def log_ffmpeg_debug(message):
    """Log detailed FFmpeg output to separate log."""
    ffmpeg_logger.debug(message)

# Example usage:
if __name__ == '__main__':
    log_info('Application started')
    log_ffmpeg('Starting FFmpeg transcode session abc123')
    log_ffmpeg_debug('FFmpeg command: ffmpeg -i input.mp4 ...')
    log_error('Something went wrong!')
    print('Regular print statement - now with timestamp!')
"""Shared access for generated streamer data and logs."""

from pathlib import Path
import stat


def share_output(path, recursive=False):
    """Set rwxrwxr-x, retaining special bits and leaving symlinks alone."""
    path = Path(path)
    if path.is_symlink() or not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    shared = (mode & 0o7000) | 0o775
    if mode != shared:
        path.chmod(shared)
    if recursive and path.is_dir():
        for child in path.iterdir():
            share_output(child, recursive=True)


def output_directory(path):
    """Create output parents with shared permissions, independent of umask."""
    path = Path(path)
    if path.is_dir():
        return path
    if not path.parent.exists():
        output_directory(path.parent)
    path.mkdir(exist_ok=True)
    share_output(path)
    return path

"""Share the server's local timezone rules for offline output naming."""

import base64
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def timezone_description():
    """Describe TZ overrides or the system zone, preserving DST rules on Unix."""
    override = os.environ.get("TZ")
    if override:
        name = override.removeprefix(":")
        try:
            ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            path = Path(name)
            if path.is_absolute() and path.is_file():
                return {"tzif": base64.b64encode(path.read_bytes()).decode("ascii")}
        else:
            return {"key": name}
    elif override is None:
        path = Path("/etc/localtime")
        if path.is_file():
            return {"tzif": base64.b64encode(path.read_bytes()).decode("ascii")}
    # Platforms without a zoneinfo file, or non-IANA POSIX TZ overrides.
    local = datetime.now().astimezone()
    return {"offset_seconds": local.utcoffset().total_seconds(), "name": local.tzname()}


def timezone_from_description(description):
    if "key" in description:
        return ZoneInfo(description["key"])
    if "tzif" in description:
        return ZoneInfo.from_file(io.BytesIO(base64.b64decode(description["tzif"], validate=True)))
    return timezone(timedelta(seconds=description["offset_seconds"]), description["name"])

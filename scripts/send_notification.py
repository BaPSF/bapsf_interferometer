#!/usr/bin/env python3
"""Publish to ntfy and validate its acknowledgment, using only the standard library.

Usage: python scripts/send_notification.py TOPIC "MESSAGE"
NTFY_SERVER_URL defaults to https://ntfy.sh; NTFY_TOKEN supplies an optional token.
TOPIC can also be a full HTTP(S) topic URL.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def destination(topic):
    if topic.startswith(("http://", "https://")):
        url = topic
    else:
        if not topic or any(c in topic for c in "/?#"):
            raise ValueError("Topic must be a name or a full HTTP(S) topic URL")
        server = os.environ.get("NTFY_SERVER_URL", "https://ntfy.sh")
        url = server.rstrip("/") + "/" + urllib.parse.quote(topic, safe="")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or not parsed.path.strip("/")):
        raise ValueError("Invalid ntfy topic URL; use NTFY_TOKEN for authentication")
    return url, urllib.parse.unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])


def print_response(response):
    print(f"HTTP status: {response.status}")
    print(f"Response URL: {response.geturl()}")
    print(f"Content-Type: {response.headers.get('Content-Type', '(missing)')}")
    body = response.read().decode("utf-8", errors="replace")
    print("Response body:")
    print(body)
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", help="topic name or full HTTP(S) topic URL")
    parser.add_argument("message", help="message text (quote it if it contains spaces)")
    args = parser.parse_args(argv)
    try:
        url, topic = destination(args.topic)
        if not args.message.strip():
            raise ValueError("Message must not be empty")
    except ValueError as exc:
        parser.error(str(exc))

    headers = {"Content-Type": "text/plain; charset=utf-8", "Accept": "application/json"}
    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=args.message.encode("utf-8"), headers=headers, method="POST")
    print(f"Publishing to: {url}", flush=True)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = print_response(response)
            if not 200 <= response.status < 300:
                raise ValueError("Server returned an unsuccessful HTTP status")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Expected an ntfy JSON message object")
        if result.get("event") != "message":
            raise ValueError("Response is not an ntfy message acknowledgment")
        if not isinstance(result.get("id"), str) or not result["id"]:
            raise ValueError("Response has no message ID")
        if result.get("topic") != topic:
            raise ValueError(f"Response topic does not match {topic!r}")
        if not isinstance(result.get("message"), str) or not result["message"]:
            raise ValueError("Response has no message text")
        if result["message"].strip() != args.message.strip():
            raise ValueError("Response message differs from the submitted text")
    except urllib.error.HTTPError as exc:
        with exc:
            print_response(exc)
        print(f"Publish failed: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Publish failed: {exc}", file=sys.stderr)
        return 1

    print(f"Server accepted message {result['id']} on topic {topic}.")
    print("This confirms publishing; device delivery may be delayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

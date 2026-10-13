"""Update this profile's existing WakaTime section using an OAuth access token."""

from datetime import datetime
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_URL = "https://api.wakatime.com/api/v1/users/current/stats/all_time"
START = "<!--START_SECTION:waka-->"
END = "<!--END_SECTION:waka-->"


class UpdateError(Exception):
    """An actionable error message containing no response body or credentials."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards Authorization when following redirects.
        return None


def fetch_stats(token):
    request = Request(API_URL, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "marsevilspirit-waka-readme",
    })
    opener = build_opener(NoRedirects())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=30) as response:
                if response.status == 202:
                    error = "WakaTime statistics are still being calculated; try again later."
                elif response.status != 200:
                    raise UpdateError(f"WakaTime request failed (HTTP {response.status}).")
                else:
                    try:
                        payload = json.load(response)
                    except (ValueError, UnicodeError):
                        raise UpdateError("WakaTime returned invalid JSON.") from None
                    if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
                        raise UpdateError("WakaTime returned an API error or invalid response.")
                    stats = payload.get("data")
                    if not isinstance(stats, dict) or not stats:
                        raise UpdateError("WakaTime returned no statistics.")
                    if stats.get("is_up_to_date") is not False:
                        return stats
                    error = "WakaTime statistics are still being calculated; try again later."
        except HTTPError as exception:
            exception.close()
            if exception.code == 401:
                raise UpdateError(
                    "WakaTime access token is expired or revoked; renew WAKATIME_ACCESS_TOKEN."
                ) from None
            if exception.code == 403:
                raise UpdateError(
                    "WakaTime denied access; check the read_stats.languages OAuth scope."
                ) from None
            error = f"WakaTime request failed (HTTP {exception.code}); try again later."
            if exception.code not in {429, 500, 502, 503, 504}:
                raise UpdateError(error) from None
        except (URLError, TimeoutError, HTTPException):
            error = "Unable to reach WakaTime; try again later."
        if attempt < 2:
            sleep(10)
    raise UpdateError(error)


def display_text(value):
    if not isinstance(value, str) or not value or "`" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise UpdateError("WakaTime statistics contain an invalid display field.")
    return value


def render_section(stats):
    try:
        dates = [
            datetime.fromisoformat(re.sub(r"([+-]\d{2}):?(\d{2})$|Z$", "", display_text(stats[key]))).strftime("%d %B %Y")
            for key in ("start", "end")
        ]
        total = display_text(stats["human_readable_total_including_other_language"])
        if not isinstance(stats["languages"], list):
            raise ValueError
        languages = []
        for language in stats["languages"]:
            name, duration = display_text(language["name"]), display_text(language["text"])
            if isinstance(language["percent"], bool):
                raise ValueError
            percent = float(language["percent"])
            if not 0 <= percent <= 100:
                raise ValueError
            languages.append((name, duration, percent))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise UpdateError("WakaTime statistics are incomplete or invalid; check OAuth access.") from None

    width = max((len(name) for name, _, _ in languages), default=0)
    rows = []
    for index, (name, duration, percent) in enumerate(languages):
        if name == "Other":
            continue
        # Match athul/waka-readme's 25-cell graph, spacing and LANG_COUNT behavior.
        progress = percent / 100 * 25
        full = int(progress + 1 / 6)
        fraction = int((progress - full) * 3 + 0.5)
        bar = "█" * full + ("░▒▓█"[fraction] if fraction > 0 else "")
        bar += "░" * (25 - len(bar))
        rows.append(f"{name:<{width}}   {duration:<22}{bar}   {percent:05.2f} %")
        if index + 1 >= 10:
            break
    content = f"From: {dates[0]} - To: {dates[1]}\n\nTotal Time: {total}"
    if not languages:
        content += "\n\nNo activity tracked"
    elif rows:
        content += "\n\n" + "\n".join(rows)
    return f"{START}\n\n```txt\n{content}\n```\n\n{END}"


def update_readme(path, stats):
    original = path.read_bytes().decode("utf-8")
    if original.count(START) != 1 or original.count(END) != 1 or original.index(START) >= original.index(END):
        raise UpdateError("README must contain exactly one ordered WakaTime section.")
    updated = original[:original.index(START)] + render_section(stats) + original[original.index(END) + len(END):]
    if updated == original:
        return False
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".README.", delete=False) as file:
            temporary = Path(file.name)
            file.write(updated.encode("utf-8"))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def main():
    token = os.environ.get("WAKATIME_ACCESS_TOKEN", "").strip()
    try:
        if not token or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise UpdateError("Set a valid WAKATIME_ACCESS_TOKEN before running the OAuth updater.")
        stats = fetch_stats(token)
        if token in json.dumps(stats):
            raise UpdateError("WakaTime returned credential-like content; refusing to publish it.")
        changed = update_readme(Path("README.md"), stats)
    except (UpdateError, OSError, UnicodeError) as exception:
        message = str(exception) if isinstance(exception, UpdateError) else "Unable to read or write README safely."
        print(message + " README was not changed.", file=sys.stderr)
        return 1
    print("WakaTime README updated." if changed else "WakaTime README unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

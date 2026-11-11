"""Small multi-source discovery façade.

The existing drama providers remain unchanged. Generic direct HTTP/HTTPS files
are handled by an independent transfer backend, not by video extractors.
"""
import hashlib
import ipaddress
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from dramafren_downloader import discover as discover_drama
from dramafren_downloader import parse_url as parse_drama

DIRECT_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".mp3", ".m4a", ".flac",
    ".ogg", ".wav", ".avi", ".pdf", ".zip", ".7z", ".rar", ".tar",
    ".gz", ".bz2", ".xz", ".epub", ".jpg", ".jpeg", ".png",
    ".webp", ".gif", ".txt", ".csv", ".json", ".iso", ".dmg", ".pkg",
})


def direct_filename(url):
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Une URL HTTP/HTTPS valide est nécessaire")
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Les adresses locales ne sont pas prises en charge")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValueError("Les adresses IP non publiques ne sont pas prises en charge")
    name = unquote(PurePosixPath(parsed.path).name)
    if not name or len(name) > 200 or "/" in name or "\\" in name:
        raise ValueError("Nom de fichier impossible à déterminer depuis l'URL")
    extension = PurePosixPath(name).suffix.lower()
    if extension not in DIRECT_EXTENSIONS:
        raise ValueError("Cette URL n'est pas un fichier direct reconnu")
    stem = re.sub(r"[^\w -]", "_", name[:-len(extension)]).strip(" ._") or "fichier"
    return stem[:120] + extension


def parse_any(url):
    try:
        return parse_drama(url)
    except ValueError:
        filename = direct_filename(url)
        return {"id": hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:24],
                "source": "direct", "server": 1, "lang": "en",
                "filename": filename}


def discover_any(url):
    parsed = parse_any(url)
    if parsed["source"] != "direct":
        return discover_drama(url)
    return {**parsed, "title": parsed["filename"], "title_unknown": False,
            "total": 1, "file_url": url.strip(), "start": 1, "end": 1}

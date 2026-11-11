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


def validate_public_url(url):
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
    return parsed


def direct_filename(url):
    parsed = validate_public_url(url)
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


def discover_ytdlp(url):
    """One accessible video per package for the initial generic extractor."""
    validate_public_url(url)
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp est requis pour analyser cette page") from exc
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "noplaylist": True, "skip_download": True}) as client:
            data = client.extract_info(url, download=False)
    except Exception as exc:
        raise RuntimeError(f"Extraction yt-dlp impossible : {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Aucune vidéo identifiable sur cette page")
    if data.get("_type") in ("playlist", "multi_video") or data.get("entries") is not None:
        raise RuntimeError("Playlist générique non encore prise en charge : connecteur nécessaire")
    if data.get("is_live"):
        raise RuntimeError("Flux en direct non encore pris en charge")
    if data.get("vcodec") == "none":
        raise RuntimeError("La première version générique accepte uniquement les vidéos")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "Vidéo_" + hashlib.sha256(url.encode()).hexdigest()[:10]
    return {"id": hashlib.sha256(url.strip().encode()).hexdigest()[:24],
            "source": "ytdlp", "server": 1, "lang": "en", "total": 1,
            "title": title.strip(), "title_unknown": False,
            "file_url": url.strip(), "start": 1, "end": 1}


def discover_any(url):
    try:
        parsed = parse_any(url)
    except ValueError:
        return discover_ytdlp(url)
    if parsed["source"] != "direct":
        return discover_drama(url)
    return {**parsed, "title": parsed["filename"], "title_unknown": False,
            "total": 1, "file_url": url.strip(), "start": 1, "end": 1}

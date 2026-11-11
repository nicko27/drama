#!/usr/bin/env python3
"""Accès aux vidéos DramaFren accessibles, sans contournement de DRM.

Adapté du script original : les liens sont demandés à watch_stream (IDrama)
ou get_video (DramaBox) pour chaque tentative, jamais enregistrés dans la file.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from html import unescape

API_HOSTS = ["cdn-idramav3.dramafren.org", "cdn-idramav2.dramafren.org"]
DRAMABOX_HOST = "dramabox.dramafren.org"
DRAMABOX_API = "cdn-dramabox.dramafren.org"
REELSHORT_HOST = "reelshort.dramafren.org"
MOBOREELS_HOST = "moboreels.dramafren.org"
REELFREN_HOST = "reelfren.net"
REELFREN_API = "https://api.reelfren.net/api"
KISS18_HOST = "kiss18.vip"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
REFERER = "https://idrama.dramafren.org/"


def referer_for(source):
    if source == "kiss18":
        return f"https://{KISS18_HOST}/"
    if source == "anyreel":
        return f"https://{REELFREN_HOST}/"
    if source == "reelshort":
        return f"https://{REELSHORT_HOST}/"
    if source == "moboreels":
        return f"https://{MOBOREELS_HOST}/"
    return f"https://{DRAMABOX_HOST}/" if source == "dramabox" else REFERER


def parse_url(url):
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"idrama.dramafren.org", "dramafren.org", DRAMABOX_HOST, REELSHORT_HOST, MOBOREELS_HOST, REELFREN_HOST, KISS18_HOST}:
        raise ValueError("URL DramaFren HTTPS invalide")
    q = urllib.parse.parse_qs(parsed.query)
    if parsed.hostname == KISS18_HOST:
        match = re.fullmatch(r"/watch/([a-z0-9]+(?:-[a-z0-9]+)*)/([1-9][0-9]*)/?", parsed.path)
        if not match:
            raise ValueError("URL Kiss18 d'épisode invalide")
        return {"id": match[1], "server": 1, "lang": "en", "source": "kiss18", "episode": int(match[2])}
    if parsed.hostname == REELFREN_HOST:
        match = re.fullmatch(r"/watch/anyreel/([0-9a-fA-F]{24})(?:-[a-zA-Z0-9-]+)?/?", parsed.path)
        if not match:
            raise ValueError("URL de lecture AnyReel invalide")
        server = q.get("server", ["1"])[0]
        lang = q.get("lang", ["en"])[0]
        if not re.fullmatch(r"[1-9][0-9]*", server) or not re.fullmatch(r"[a-zA-Z-]{2,12}", lang):
            raise ValueError("Serveur ou langue invalide")
        return {"id": match[1].lower(), "server": int(server), "lang": lang, "source": "anyreel"}
    source = ("moboreels" if parsed.hostname == MOBOREELS_HOST else
              "reelshort" if parsed.hostname == REELSHORT_HOST else
              "dramabox" if parsed.hostname == DRAMABOX_HOST else "idrama")
    if source == "moboreels":
        if parsed.path not in ("", "/") or q.get("api_route") != ["detail"]:
            raise ValueError("URL MoboReels de détail invalide")
    elif parsed.path != "/index.php":
        raise ValueError("URL DramaFren HTTPS invalide")
    identifier = q.get("id", [""])[0]
    valid_id = re.fullmatch(r"[0-9a-fA-F]{24}" if source == "reelshort" else r"[0-9]+", identifier)
    if (source != "moboreels" and q.get("page") not in (["watch"], ["detail"])) or not valid_id:
        raise ValueError("URL de lecture/détail ou identifiant invalide")
    server = q.get("sv" if source == "dramabox" else "server", ["1"])[0]
    lang = q.get("lang", ["en"])[0]
    if not re.fullmatch(r"[1-9][0-9]*", server) or not re.fullmatch(r"[a-zA-Z-]{2,12}", lang):
        raise ValueError("Serveur ou langue invalide")
    return {"id": identifier.lower() if source == "reelshort" else identifier,
            "server": int(server), "lang": lang, "source": source}


def query_details(drama_id, episode, server, lang, timeout=15, source="idrama"):
    if source == "kiss18":
        return kiss18_video(drama_id, episode, timeout)
    if source == "anyreel":
        return anyreel_video(drama_id, episode, server, lang, timeout)
    if source == "reelshort":
        raise RuntimeError("API vidéo ReelShort de DramaFren non identifiée ; aucun lien vidéo supposé")
    if source == "moboreels":
        raise RuntimeError("MoboReels : api_route=detail n'est pas une API vidéo ; point d'accès vidéo non identifié")
    if source not in ("idrama", "dramabox"):
        raise ValueError("Source inconnue")
    referer = referer_for(source)
    params = urllib.parse.urlencode((
        {"action": "get_video", "id": drama_id, "ep": episode, "lang": lang, "sv": server}
        if source == "dramabox" else
        {"action": "watch_stream", "id": drama_id, "ep": episode, "server": server, "lang": lang}
    ))
    errors = []
    cloudflare_challenge = False
    for host in ([DRAMABOX_API] if source == "dramabox" else API_HOSTS):
        request = urllib.request.Request(f"https://{host}/index.php?{params}", headers={
            "User-Agent": USER_AGENT, "Accept": "application/json",
            "Origin": referer.rstrip("/"), "Referer": referer,
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
            if not isinstance(data, dict) or not data.get("ok"):
                raise ValueError(str(data.get("error", "réponse API invalide")) if isinstance(data, dict) else "réponse JSON invalide")
            urls = []
            if source == "dramabox":
                candidates = [data.get("videoUrl"), *(q.get("url") for q in (data.get("qualities") or []) if isinstance(q, dict)),
                              *(data.get("proxyUrls") or [])]
            else:
                candidates = [data.get("video_url"), *(data.get("video_urls") or [])]
            for candidate in candidates:
                if isinstance(candidate, str) and candidate.startswith("https://") and candidate not in urls:
                    urls.append(candidate)
            if not urls:
                raise ValueError("API sans URL vidéo")
            data["urls"] = urls
            return data
        except urllib.error.HTTPError as err:
            if err.code == 403 and err.headers.get("cf-mitigated", "").lower() == "challenge":
                cloudflare_challenge = True
            errors.append(f"{host}: HTTP {err.code}")
        except (urllib.error.URLError, ValueError, OSError, json.JSONDecodeError) as err:
            errors.append(f"{host}: {err}")
    if source == "dramabox" and cloudflare_challenge:
        raise RuntimeError("DramaBox : API get_video protégée par Cloudflare (HTTP 403) ; accès indisponible")
    if source == "dramabox" and any("HTTP 403" in error for error in errors):
        raise RuntimeError("DramaBox : accès HTTP 403 à l’API get_video")
    raise RuntimeError(" ; ".join(errors))


def query_api(drama_id, episode, server, lang, timeout=15, source="idrama"):
    """Interface conservée du script d'origine."""
    return query_details(drama_id, episode, server, lang, timeout, source)["urls"]


def discover(url):
    info = parse_url(url)
    parsed = urllib.parse.urlsplit(url)
    ep = info.pop("episode") if info["source"] == "kiss18" else int(urllib.parse.parse_qs(parsed.query).get("ep", ["1"])[0])
    if ep < 1:
        raise ValueError("Épisode invalide")
    if info["source"] == "kiss18":
        info.update({key: value for key, value in kiss18_metadata(info["id"]).items() if key != "chunks"})
        if ep > info["total"]:
            raise ValueError(f"Épisode {ep} supérieur au total ({info['total']})")
        query_details(info["id"], ep, 1, "en", source="kiss18")
        return info
    if info["source"] == "anyreel":
        info.update(anyreel_metadata(info["id"], info["lang"]))
        if ep > info["total"]:
            raise ValueError(f"Épisode {ep} supérieur au total ({info['total']})")
        query_details(info["id"], ep, info["server"], info["lang"], source="anyreel")
        return info
    if info["source"] == "dramabox":
        metadata = dramabox_metadata(info["id"])
        info.update(metadata)
        if ep > info["total"]:
            raise ValueError(f"Épisode {ep} supérieur au total ({info['total']})")
        query_details(info["id"], ep, info["server"], info["lang"], source="dramabox")
        return info
    if info["source"] == "reelshort":
        metadata = reelshort_metadata(info["id"])
        raise RuntimeError(f"ReelShort : {metadata['title']} ({metadata['total']} épisodes sur ReelShort). "
                           "API vidéo DramaFren non identifiée ; analyse des téléchargements impossible")
    if info["source"] == "moboreels":
        raise RuntimeError("MoboReels : api_route=detail fournit une fiche, pas les vidéos. "
                           "API vidéo DramaFren non identifiée ; téléchargement indisponible")
    data = query_details(info["id"], ep, info["server"], info["lang"], source=info["source"])
    total = data.get("total_eps")
    if not isinstance(total, int) or isinstance(total, bool) or total < 1 or total > 10000:
        raise ValueError("Nombre total d'épisodes indisponible dans l'API")
    info["total"] = total
    info["title"] = fetch_title(url) or f"Série_{info['id']}"
    info["title_unknown"] = info["title"].startswith("Série_")
    return info


def dramabox_metadata(drama_id):
    """Titre et nombre d'épisodes ; la disponibilité vient de DramaFren, pas de ce catalogue."""
    if not re.fullmatch(r"[0-9]+", str(drama_id)):
        raise ValueError("Identifiant DramaBox invalide")
    url = f"https://www.dramaboxdb.com/movie/{drama_id}/"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read(4_000_000).decode("utf-8", "replace")
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not match:
            raise ValueError("Liste des épisodes indisponible")
        props = json.loads(match.group(1))["props"]["pageProps"]
        chapters = sorted(props["chapterList"], key=lambda c: c["index"])
        if not chapters or len(chapters) > 10000:
            raise ValueError("Liste des épisodes invalide")
        if [chapter["index"] for chapter in chapters] != list(range(len(chapters))):
            raise ValueError("Numérotation des épisodes incomplète")
        title = props["bookInfo"].get("bookName") or f"Série_{drama_id}"
        return {"title": title, "title_unknown": title == f"Série_{drama_id}", "total": len(chapters)}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Métadonnées DramaBox indisponibles : {exc}") from exc


def reelshort_metadata(drama_id):
    """Métadonnées publiques ReelShort ; ne fournit aucune vidéo DramaFren."""
    if not re.fullmatch(r"[0-9a-f]{24}", drama_id):
        raise ValueError("Identifiant ReelShort invalide")
    url = f"https://www.reelshort.com/movie/{drama_id}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read(4_000_000).decode("utf-8", "replace")
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not match:
            raise ValueError("Page sans métadonnées")
        data = json.loads(match.group(1))["props"]["pageProps"]["data"]
        if data.get("book_id", "").lower() != drama_id:
            raise ValueError("Identifiant de série différent dans la réponse")
        total = data.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or not 1 <= total <= 10000:
            raise ValueError("Nombre d'épisodes invalide")
        return {"title": data.get("book_title") or f"Série_{drama_id}", "total": total}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Métadonnées ReelShort indisponibles : {exc}") from exc


def anyreel_request(route, drama_id, lang, timeout=15, **params):
    if not re.fullmatch(r"[0-9a-f]{24}", drama_id):
        raise ValueError("Identifiant AnyReel invalide")
    query = urllib.parse.urlencode({"provider": "anyreel", "id": drama_id, "lang": lang, **params})
    request = urllib.request.Request(f"{REELFREN_API}/{route}?{query}", headers={
        "User-Agent": USER_AGENT, "Accept": "application/json", "Referer": referer_for("anyreel")})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("Réponse JSON invalide")
        return data
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"API AnyReel ({route}) : {exc}") from exc


def anyreel_metadata(drama_id, lang="en"):
    data = anyreel_request("detail", drama_id, lang)
    total = data.get("episodes")
    if data.get("id") != drama_id or data.get("provider") != "anyreel":
        raise RuntimeError("Réponse AnyReel d'une autre série")
    if not isinstance(total, int) or isinstance(total, bool) or not 1 <= total <= 10000:
        raise RuntimeError("Nombre d'épisodes AnyReel indisponible")
    title = data.get("title") or f"Série_{drama_id}"
    return {"title": title, "title_unknown": title == f"Série_{drama_id}", "total": total}


def anyreel_video(drama_id, episode, server, lang, timeout=15):
    data = anyreel_request("video", drama_id, lang, timeout, ep=episode, server=server, cv="v21")
    if data.get("locked") is True:
        raise RuntimeError(f"Épisode {episode} indisponible (accès restreint)")
    if data.get("episodeNumber") != episode:
        raise RuntimeError(f"Réponse AnyReel incorrecte pour l'épisode {episode}")
    candidates = [data.get("videoUrl"), *(q.get("url") for q in (data.get("qualityList") or []) if isinstance(q, dict))]
    urls = []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("https://") and candidate not in urls:
            urls.append(candidate)
    if not urls:
        raise RuntimeError(f"Aucun lien vidéo accessible pour l'épisode {episode}")
    data["urls"] = urls
    return data


@lru_cache(maxsize=2)
def kiss18_metadata(slug):
    """Catalogue public, chargé une fois par série ; seuls les chapitres jouables sont retenus."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Identifiant Kiss18 invalide")
    req = urllib.request.Request(f"https://{KISS18_HOST}/catalogue-browse.mjs", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            script = response.read(12_000_000).decode("utf-8", "replace")
        marker = "export const snapshot = "
        if marker not in script:
            raise ValueError("Catalogue sans liste de séries")
        snapshot, _ = json.JSONDecoder().raw_decode(script.split(marker, 1)[1])
        entries = [item for item in snapshot["items"] if item.get("public_slug") == slug]
        if len(entries) != 1:
            raise ValueError("Série absente ou ambiguë dans le catalogue")
        story = entries[0]
        if story.get("id") != f"chartdrama:shortpeak:{slug}":
            raise ValueError("Identifiant de série inattendu")
        episodes = sorted(story["episodes"], key=lambda ep: ep["ordinal"])
        if not episodes or len(episodes) > 10000 or [ep["ordinal"] for ep in episodes] != list(range(1, len(episodes) + 1)):
            raise ValueError("Numérotation des épisodes incomplète")
        if any(ep.get("id") != f"{story['id']}:ep:{ep['ordinal']}" for ep in episodes):
            raise ValueError("Identifiant d'épisode inattendu")
        chunks = {ep["ordinal"]: ep.get("playbackChunk") for ep in episodes if ep.get("playable") and
                  isinstance(ep.get("playbackChunk"), str) and re.fullmatch(r"[0-9a-f]{64}", ep["playbackChunk"])}
        if not chunks:
            raise ValueError("Aucun épisode jouable dans le catalogue")
        title = story.get("title") or f"Série_{slug}"
        return {"title": title, "title_unknown": title == f"Série_{slug}", "total": len(episodes), "chunks": chunks}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Catalogue Kiss18 indisponible : {exc}") from exc


def kiss18_video(slug, episode, timeout=15):
    chunk = kiss18_metadata(slug)["chunks"].get(episode)
    if not chunk:
        raise RuntimeError(f"Épisode {episode} non disponible dans le catalogue Kiss18")
    req = urllib.request.Request(f"https://{KISS18_HOST}/playback/{chunk}.json", headers={
        "User-Agent": USER_AGENT, "Accept": "application/json", "Referer": referer_for("kiss18")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.load(response)
        candidates = data["playback"].values()
        urls = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("episodeId") != f"chartdrama:shortpeak:{slug}:ep:{episode}":
                continue
            url = candidate.get("url")
            if (candidate.get("format") == "hls" and candidate.get("refreshable") is False and
                    isinstance(url, str) and urllib.parse.urlsplit(url).hostname == "cdn.kiss18.vip" and
                    url.startswith("https://") and url not in urls):
                urls.append(url)
        if not urls:
            raise ValueError("Aucune vidéo HLS directe accessible pour cet épisode")
        return {"urls": urls}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Vidéo Kiss18 épisode {episode} : {exc}") from exc


def fetch_title(url):
    """La page peut refuser l'accès ; le titre ne doit jamais être inventé."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer_for(parse_url(url)["source"])})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read(2_000_000).decode("utf-8", "replace")
        for pattern in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                        r'<title[^>]*>(.*?)</title>'):
            match = re.search(pattern, html, re.I | re.S)
            if match:
                title = unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
                if title and title.lower() not in {"dramafren", "idrama"}:
                    return title
    except (OSError, urllib.error.URLError, ValueError):
        pass
    return None

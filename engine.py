"""Téléchargement, reprise et concaténation ; indépendant de Qt."""
import json
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dramafren_downloader import anyreel_metadata, dramabox_metadata, kiss18_metadata, query_api, referer_for

EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov")


def safe_name(title):
    return re.sub(r"[\s_]+", "_", re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE)).strip("._ ") or "Serie"


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(f"ffprobe : {result.stderr.strip()}")
    data = json.loads(result.stdout)
    if not any(s.get("codec_type") == "video" for s in data.get("streams", [])):
        raise RuntimeError(f"Aucune piste vidéo : {path.name}")
    if float(data.get("format", {}).get("duration", 0)) <= 0:
        raise RuntimeError(f"Durée invalide : {path.name}")
    return data


def verified_episode(folder, ep):
    """Retrouve la vidéo et ses métadonnées avec une seule vérification ffprobe."""
    for ext in EXTENSIONS:
        path = folder / f"Episode_{ep:02d}{ext}"
        if path.is_file() and path.stat().st_size > 0:
            try:
                return path, probe(path)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                continue
    return None, None


def existing_video(folder, ep):
    """Ne reprend que des fichiers multimédias réellement lisibles."""
    return verified_episode(folder, ep)[0]


def compatible(probes):
    def signature(data):
        return [(s.get("codec_type"), s.get("codec_name"), s.get("width"), s.get("height"),
                 s.get("sample_rate"), s.get("channels"), s.get("pix_fmt"))
                for s in data["streams"] if s.get("codec_type") in ("video", "audio")]
    return all(signature(p) == signature(probes[0]) for p in probes[1:])


class Cancelled(Exception):
    pass


class Controller:
    def __init__(self):
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.lock = threading.Lock()
        self.processes = set()

    def cancel(self):
        self.stop.set()
        with self.lock:
            for proc in list(self.processes):
                proc.terminate()

    def wait(self):
        while self.pause.is_set() and not self.stop.is_set():
            self.stop.wait(0.2)
        if self.stop.is_set():
            raise Cancelled()

    def run(self, command, line=None):
        self.wait()
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        with self.lock:
            self.processes.add(proc)
            if self.stop.is_set():
                proc.terminate()
        tail = []
        try:
            for output in proc.stdout:
                output = output.strip()
                if output:
                    tail.append(output)
                    tail = tail[-8:]
                    if line:
                        line(output)
            code = proc.wait()
            if self.stop.is_set():
                raise Cancelled()
            if code:
                raise RuntimeError(" ; ".join(tail) or f"Commande échouée ({code})")
        finally:
            with self.lock:
                self.processes.discard(proc)
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
            proc.stdout.close()


def download_episode(series, episode, folder, controller, update):
    controller.wait()
    found = existing_video(folder, episode)
    if found:
        update(episode, "terminé", 100, "", "Déjà vérifié")
        return found
    error = "Téléchargement impossible"
    for attempt in range(3):
        controller.wait()
        try:
            # Nouvelle URL à chaque tentative : ne jamais conserver les liens signés.
            for url in query_api(series["id"], episode, series["server"], series["lang"],
                                 source=series.get("source", "idrama")):
                controller.wait()
                update(episode, "téléchargement", 0, "", f"Tentative {attempt + 1}/3")

                def on_line(line):
                    match = re.search(r"\[download\]\s+([\d.]+)%.*?at\s+([^\s]+)", line)
                    if match:
                        update(episode, "téléchargement", float(match[1]), match[2], "")

                try:
                    controller.run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--continue", "--no-overwrites",
                                    "--newline", "--retries", "3", "--fragment-retries", "3",
                                    "--referer", referer_for(series.get("source", "idrama")),
                                    "-o", str(folder / f"Episode_{episode:02d}.%(ext)s"), url], on_line)
                    found = existing_video(folder, episode)
                    if not found:
                        raise RuntimeError("yt-dlp a terminé sans fichier vidéo valide")
                    update(episode, "terminé", 100, "", "")
                    return found
                except RuntimeError as exc:
                    error = str(exc)
        except RuntimeError as exc:
            error = str(exc)
        update(episode, "erreur", 0, "", error)
        if attempt < 2 and controller.stop.wait(2):
            raise Cancelled()
    raise RuntimeError(f"Épisode {episode} : {error}")


def series_identity(series):
    """Unique logical series identity across providers."""
    return f"{series.get('source', 'idrama')}:{series['id']}"


def assign_folder(series, destination, reserved=None):
    """Pin a stable folder, preserving old names without sharing two series' files."""
    reserved = set(reserved or ())
    destination = Path(destination)
    if "folder" in series:
        name = series["folder"]
        if not isinstance(name, str) or not name or safe_name(name) != name or name in reserved:
            raise ValueError("Dossier de série invalide ou déjà utilisé dans la file")
        return destination / name
    legacy = safe_name(series["title"] + "_" + series["id"])
    stable = safe_name(series_identity(series).replace(":", "_"))
    if (destination / legacy).is_dir() and legacy not in reserved:
        name = legacy
    else:
        candidates = [p.name for p in destination.glob("*_" + series["id"])
                      if p.is_dir() and safe_name(p.name) == p.name and p.name not in reserved]
        name = candidates[0] if len(candidates) == 1 else stable
    if name in reserved:
        raise ValueError("Deux séries ne peuvent partager le même dossier")
    series["folder"] = name
    return destination / name

def file_fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": Path(path).stat().st_size, "sha256": digest.hexdigest()}


def merge_job(series, episodes):
    return {"source": series.get("source", "idrama"), "id": series.get("id", ""),
            "server": series.get("server", 1), "lang": series.get("lang", "en"),
            "episodes": list(episodes), "format": "mp4"}


def manifest_for(output):
    return Path(str(output) + ".manifest.json")


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False,
                                         encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
            temp = Path(file.name)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def verified_merge(series, folder, episodes, require_sources=False):
    """Only accept a final MP4 with a matching provenance and content checksum."""
    requested = merge_job(series, episodes)
    for sidecar in sorted(folder.glob("*.mp4.manifest.json")):
        output = Path(str(sidecar)[:-len(".manifest.json")])
        try:
            manifest = json.loads(sidecar.read_text(encoding="utf-8"))
            if manifest.get("version") != 1 or manifest.get("job") != requested:
                continue
            inputs = manifest.get("sources")
            if not isinstance(inputs, list) or [x.get("episode") for x in inputs] != list(episodes):
                continue
            if not output.is_file() or file_fingerprint(output) != manifest.get("result"):
                continue
            probe(output)
            for item in inputs:
                path, _ = verified_episode(folder, item["episode"])
                if path is None:
                    if require_sources:
                        break
                    continue
                if path.name != item.get("name") or file_fingerprint(path) != item.get("fingerprint"):
                    break
            else:
                return output
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, json.JSONDecodeError,
                subprocess.TimeoutExpired):
            continue
    return None


def merge(series, folder, episodes, controller, reencode_prompt, keep=True):
    folder = Path(folder)
    existing = verified_merge(series, folder, episodes, require_sources=keep)
    if existing:
        if not keep:
            # Verified input fingerprints were checked by verified_merge.
            for ep in episodes:
                old = existing_video(folder, ep)
                if old:
                    old.unlink()
        return existing
    checked = [verified_episode(folder, ep) for ep in episodes]
    paths = [path for path, _ in checked]
    if not paths or any(p is None for p in paths):
        raise RuntimeError("Fusion interdite : épisode manquant ou invalide")
    probes = [metadata for _, metadata in checked]
    source_data = [{"episode": ep, "name": path.name, "fingerprint": file_fingerprint(path)}
                   for ep, path in zip(episodes, paths)]
    complete = series.get("start", 1) == 1 and series.get("end", len(episodes)) == series.get("total", len(episodes))
    suffix = "Complet" if complete else f"Episodes_{episodes[0]:02d}-{episodes[-1]:02d}"
    output = folder / f"{safe_name(series['title'])}_{suffix}.mp4"
    if output.exists():
        # Never overwrite an untrusted MP4, even if ffprobe considers it readable.
        base = output.stem
        index = 1
        while output.exists() or manifest_for(output).exists():
            output = folder / f"{base}_Corrige_{index}.mp4"
            index += 1
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as listing:
        for path in paths:
            listing.write("file '" + str(path.resolve()).replace("'", "'\\''") + "'\n")
        list_path = Path(listing.name)
    temp = output.with_name(output.stem + ".tmp.mp4")
    try:
        if not compatible(probes):
            if not reencode_prompt("Flux des épisodes incompatibles : réencoder en MP4 ?"):
                raise RuntimeError("Fusion sans réencodage impossible ; réencodage refusé")
            encode = True
        else:
            encode = False
        while True:
            if temp.exists():
                temp.unlink()
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                       "-f", "concat", "-safe", "0", "-i", str(list_path)]
            command += (["-c:v", "libx264", "-c:a", "aac"] if encode else ["-c", "copy"])
            command += [str(temp)]
            try:
                controller.run(command)
                duration = float(probe(temp)["format"]["duration"])
                expected = sum(float(p["format"]["duration"]) for p in probes)
                if abs(duration - expected) > max(2, expected * 0.03):
                    raise RuntimeError(f"Durée fusionnée incorrecte ({duration:.1f}s / {expected:.1f}s)")
                controller.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                                "-xerror", "-i", str(temp), "-f", "null", "-"])
                break
            except RuntimeError:
                if encode or not reencode_prompt("Fusion directe impossible : réencoder en MP4 ?"):
                    raise
                encode = True
        result = file_fingerprint(temp)
        controller.wait()
        os.replace(temp, output)
        write_json_atomic(manifest_for(output), {
            "version": 1, "job": merge_job(series, episodes),
            "sources": source_data, "result": result, "reencoded": encode,
        })
        if not keep:
            for path in paths:
                path.unlink()
        return output
    finally:
        list_path.unlink(missing_ok=True)
        temp.unlink(missing_ok=True)


def run_series(series, destination, parallel, controller, update, log, reencode_prompt):
    if not importlib.util.find_spec("yt_dlp") or not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("yt-dlp, ffmpeg et ffprobe sont requis (voir README)")
    folder = assign_folder(series, destination)
    folder.mkdir(parents=True, exist_ok=True)
    episodes = list(range(series["start"], series["end"] + 1))
    if series.get("merge", True):
        prior = verified_merge(series, folder, episodes, require_sources=series.get("keep", True))
        if prior:
            if not series.get("keep", True):
                for ep in episodes:
                    old = existing_video(folder, ep)
                    if old:
                        old.unlink()
            log(f"Fusion déjà vérifiée : {prior}")
            return
    if series.get("source") == "dramabox":
        controller.wait()
        current = dramabox_metadata(series["id"])
        if series["start"] < 1 or series["end"] > current["total"]:
            raise RuntimeError(f"DramaBox : plage demandée hors des {current['total']} épisodes recensés")
    if series.get("source") == "anyreel":
        controller.wait()
        current = anyreel_metadata(series["id"], series["lang"])
        if series["start"] < 1 or series["end"] > current["total"]:
            raise RuntimeError(f"AnyReel : plage demandée hors des {current['total']} épisodes recensés")
    if series.get("source") == "kiss18":
        controller.wait()
        kiss18_metadata.cache_clear()
        current = kiss18_metadata(series["id"])
        if series["start"] < 1 or series["end"] > current["total"]:
            raise RuntimeError(f"Kiss18 : plage demandée hors des {current['total']} épisodes recensés")
        if any(ep not in current["chunks"] for ep in episodes):
            raise RuntimeError("Kiss18 : la plage contient un épisode non jouable dans le catalogue")
    errors = []
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(download_episode, series, ep, folder, controller, update): ep for ep in episodes}
        for future in as_completed(futures):
            try:
                future.result()
            except Cancelled:
                pass
            except Exception as exc:
                errors.append(str(exc))
                log(str(exc))
    controller.wait()
    if errors:
        raise RuntimeError(" ; ".join(errors))
    if series.get("merge", True):
        log("Fusion des épisodes…")
        output = merge(series, folder, episodes, controller, reencode_prompt, series.get("keep", True))
        log(f"Fusion validée : {output}")


SERIES_STATES = {"en attente", "analyse", "téléchargement", "pause", "erreur",
                 "terminé", "vérification", "assemblage", "intervention requise"}


def validate_queue(data):
    if not isinstance(data, dict) or not isinstance(data.get("series"), list):
        raise ValueError("File invalide : séries absentes ou incorrectes")
    if not isinstance(data.get("destination", str(Path.home() / "Downloads")), str):
        raise ValueError("Destination invalide")
    parallel = data.get("parallel", 1)
    if type(parallel) is not int or not 1 <= parallel <= 3:
        raise ValueError("Nombre de téléchargements parallèles invalide")
    pending = data.get("pending_links", [])
    if not isinstance(pending, list):
        raise ValueError("Liens en attente invalides")
    seen = set()
    folders = set()
    for index, item in enumerate(data["series"]):
        if not isinstance(item, dict):
            raise ValueError(f"Série {index} invalide")
        for field in ("id", "title", "lang"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"Série {index} : champ {field} invalide")
        if not isinstance(item.get("source", "idrama"), str) or not item.get("source", "idrama"):
            raise ValueError(f"Série {index} : source invalide")
        identity = series_identity(item)
        if identity in seen:
            raise ValueError(f"Série en double : {identity}")
        seen.add(identity)
        total = item.get("total")
        start = item.get("start")
        end = item.get("end")
        if (type(total) is not int or type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= total <= 10000):
            raise ValueError(f"Série {identity} : plage invalide")
        if type(item.get("server")) is not int or item["server"] < 1:
            raise ValueError(f"Série {identity} : serveur invalide")
        if item.get("state", "en attente") not in SERIES_STATES:
            raise ValueError(f"Série {identity} : état invalide")
        if "folder" in item:
            name = item["folder"]
            if not isinstance(name, str) or not name or safe_name(name) != name or name in folders:
                raise ValueError(f"Série {identity} : dossier invalide ou dupliqué")
            folders.add(name)
        for flag in ("merge", "keep"):
            if flag in item and type(item[flag]) is not bool:
                raise ValueError(f"Série {identity} : option {flag} invalide")
        episodes = item.get("episodes", {})
        if not isinstance(episodes, dict):
            raise ValueError(f"Série {identity} : épisodes invalides")
        for ep, value in episodes.items():
            if not isinstance(ep, str) or not ep.isdecimal() or not 1 <= int(ep) <= total:
                raise ValueError(f"Série {identity} : numéro d'épisode invalide")
            if not isinstance(value, dict):
                raise ValueError(f"Série {identity} : état d'épisode invalide")
            if value.get("state", "en attente") not in SERIES_STATES:
                raise ValueError(f"Série {identity} : état d'épisode inconnu")
            percent = value.get("percent", 0)
            if type(percent) not in (int, float) or not 0 <= percent <= 100:
                raise ValueError(f"Série {identity} : progression invalide")
    for index, link in enumerate(pending):
        if (not isinstance(link, dict) or not isinstance(link.get("id"), str)
                or not link["id"] or not isinstance(link.get("url"), str)
                or not link["url"] or link.get("state") not in
                {"en attente", "analyse", "erreur"} or not isinstance(link.get("error", ""), str)):
            raise ValueError(f"Lien en attente {index} invalide")
        if "source" in link and (not isinstance(link["source"], str) or not link["source"]):
            raise ValueError(f"Lien en attente {index} : source invalide")
    return data


def load_queue(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        validate_queue(data)
        data.setdefault("destination", str(Path.home() / "Downloads"))
        data.setdefault("parallel", 1)
        data.setdefault("pending_links", [])
        for series in data["series"]:
            series.setdefault("source", "idrama")
            series.setdefault("merge", True)
            series.setdefault("keep", True)
            series.setdefault("episodes", {})
            if series.get("state") in ("téléchargement", "pause", "assemblage", "vérification"):
                series["state"] = "en attente"
            for episode in series["episodes"].values():
                if episode.get("state") in ("téléchargement", "pause", "assemblage", "vérification"):
                    episode["state"] = "en attente"
        return data
    except FileNotFoundError:
        return {"series": [], "pending_links": [],
                "destination": str(Path.home() / "Downloads"), "parallel": 1}


def save_queue(path, data):
    write_json_atomic(path, data)

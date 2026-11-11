"""Téléchargement, reprise et concaténation ; indépendant de Qt."""
import json
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


def merge(series, folder, episodes, controller, reencode_prompt, keep=True):
    checked = [verified_episode(folder, ep) for ep in episodes]
    paths = [path for path, _ in checked]
    if not paths or any(p is None for p in paths):
        raise RuntimeError("Fusion interdite : épisode manquant ou invalide")
    probes = [metadata for _, metadata in checked]
    complete = series.get("start", 1) == 1 and series.get("end", len(episodes)) == series.get("total", len(episodes))
    suffix = "Complet" if complete else f"Episodes_{episodes[0]:02d}-{episodes[-1]:02d}"
    output = folder / f"{safe_name(series['title'])}_{suffix}.mp4"
    if output.exists():
        try:
            probe(output)
            return output
        except (RuntimeError, ValueError):
            raise RuntimeError(f"Fichier final existant non valide : {output}")
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
                # ffprobe ne contrôle que les en-têtes : décoder le fichier entier
                # avant de supprimer éventuellement les épisodes originaux.
                controller.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                                "-xerror", "-i", str(temp), "-f", "null", "-"])
                break
            except RuntimeError:
                if encode or not reencode_prompt("Fusion directe impossible : réencoder en MP4 ?"):
                    raise
                encode = True
        os.replace(temp, output)
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
    folder = Path(destination) / safe_name(series["title"] + "_" + series["id"])
    folder.mkdir(parents=True, exist_ok=True)
    episodes = list(range(series["start"], series["end"] + 1))
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


def load_queue(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("series"), list):
            raise ValueError("État invalide")
        for series in data["series"]:
            if series.get("state") in ("téléchargement", "pause"):
                series["state"] = "en attente"
            for episode in series.get("episodes", {}).values():
                if episode.get("state") in ("téléchargement", "pause"):
                    episode["state"] = "en attente"
        return data
    except FileNotFoundError:
        return {"series": [], "destination": str(Path.home() / "Downloads"), "parallel": 1}


def save_queue(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        temp = Path(file.name)
    os.replace(temp, path)

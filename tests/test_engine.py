import json
import io
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from dramafren_downloader import (anyreel_metadata, anyreel_video, discover, dramabox_metadata,
                                   kiss18_metadata, kiss18_video, parse_url, query_details, reelshort_metadata)
from engine import Controller, compatible, download_episode, existing_video, load_queue, merge, run_series, save_queue

URL = "https://idrama.dramafren.org/index.php?page=watch&id=101000643292&ep=5&server=1&lang=en"
DETAIL_URL = "https://idrama.dramafren.org/index.php?page=detail&id=100000643031&lang=en"
DRAMABOX_URL = "https://dramabox.dramafren.org/index.php?page=watch&id=42000023176&ep=1&lang=en&slug=my-cruel-mate&sv=1"
REELSHORT_URL = "https://reelshort.dramafren.org/index.php?page=detail&id=6a31012f0a4db3ff73065e0f&lang=en"
MOBOREELS_URL = "https://moboreels.dramafren.org/?api_route=detail&lang=en&id=58156322"
ANYREEL_URL = "https://reelfren.net/watch/anyreel/6a1908fe81203cd544a0531d-rejected-by-the-alpha-claimed-by-the-northern-king?ep=2&lang=en"
KISS18_URL = "https://kiss18.vip/watch/forbidden-guest-house/2"
KISS18_ID = "chartdrama:shortpeak:forbidden-guest-house"
KISS18_CHUNK = "a" * 64
PROBE = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240}],
         "format": {"duration": "5"}}


class EngineTests(unittest.TestCase):
    def test_url(self):
        self.assertEqual(parse_url(URL), {"id": "101000643292", "server": 1, "lang": "en", "source": "idrama"})
        self.assertEqual(parse_url(DETAIL_URL), {"id": "100000643031", "server": 1, "lang": "en", "source": "idrama"})
        self.assertEqual(parse_url(DRAMABOX_URL), {"id": "42000023176", "server": 1, "lang": "en", "source": "dramabox"})
        self.assertEqual(parse_url(REELSHORT_URL), {"id": "6a31012f0a4db3ff73065e0f", "server": 1, "lang": "en", "source": "reelshort"})
        self.assertEqual(parse_url(MOBOREELS_URL), {"id": "58156322", "server": 1, "lang": "en", "source": "moboreels"})
        self.assertEqual(parse_url(ANYREEL_URL), {"id": "6a1908fe81203cd544a0531d", "server": 1, "lang": "en", "source": "anyreel"})
        self.assertEqual(parse_url(KISS18_URL), {"id": "forbidden-guest-house", "server": 1,
                                                   "lang": "en", "source": "kiss18", "episode": 2})
        self.assertEqual(parse_url(DRAMABOX_URL.replace("sv=1", "sv=2"))["server"], 2)
        for url in ("http://idrama.dramafren.org/index.php?page=watch&id=1",
                    "https://evil.org/index.php?page=watch&id=1",
                    "https://idrama.dramafren.org/index.php?page=watch&id=x",
                    "https://reelshort.dramafren.org/index.php?page=detail&id=invalid&lang=en",
                    "https://moboreels.dramafren.org/?api_route=home&lang=en&id=58156322",
                    "https://reelfren.net/watch/other/6a1908fe81203cd544a0531d?ep=2",
                    "https://kiss18.vip/watch/forbidden-guest-house/0",
                    "https://kiss18.vip/playback/" + KISS18_CHUNK + ".json",
                    "http://kiss18.vip/watch/forbidden-guest-house/2",
                    "https://dramabox.dramafren.org/index.php?page=watch&id=1&sv=0"):
            with self.assertRaises(ValueError):
                parse_url(url)

    @patch("dramafren_downloader.fetch_title", return_value=None)
    @patch("dramafren_downloader.query_details", return_value={"total_eps": 7})
    def test_discover_total_from_api(self, *_):
        self.assertEqual(discover(URL)["total"], 7)

    @patch("dramafren_downloader.fetch_title", return_value=None)
    @patch("dramafren_downloader.query_details", return_value={"total_eps": 30})
    def test_discover_detail_from_first_episode(self, api, _):
        self.assertEqual(discover(DETAIL_URL)["total"], 30)
        api.assert_called_once_with("100000643031", 1, 1, "en", source="idrama")

    @patch("dramafren_downloader.urllib.request.urlopen")
    def test_dramabox_403_is_reported_without_idrama_fallback(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(DRAMABOX_URL, 403, "Forbidden", {"cf-mitigated": "challenge"}, None)
        with self.assertRaisesRegex(RuntimeError, "Cloudflare.*403"):
            query_details("42000023176", 1, 1, "en", source="dramabox")
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertIn("cdn-dramabox.dramafren.org", request.full_url)
        self.assertIn("action=get_video", request.full_url)
        self.assertIn("sv=1", request.full_url)
        self.assertEqual(request.get_header("Referer"), "https://dramabox.dramafren.org/")

    @patch("dramafren_downloader.urllib.request.urlopen")
    def test_dramabox_video_response(self, urlopen):
        urlopen.return_value = io.BytesIO(json.dumps({
            "ok": True, "videoUrl": "https://video.example/720.mp4",
            "qualities": [{"quality": "1080p", "url": "https://video.example/1080.mp4"}],
            "proxyUrls": [],
        }).encode())
        data = query_details("42000023176", 1, 1, "en", source="dramabox")
        self.assertEqual(data["urls"], ["https://video.example/720.mp4", "https://video.example/1080.mp4"])

    @patch("dramafren_downloader.urllib.request.urlopen")
    def test_dramabox_metadata_counts_all_episodes(self, urlopen):
        payload = {"props": {"pageProps": {"bookInfo": {"bookName": "My Cruel Mate"},
                    "chapterList": [{"index": i, "unlock": i < 2} for i in range(4)]}}}
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        urlopen.return_value = io.BytesIO(html.encode())
        self.assertEqual(dramabox_metadata("42000023176"),
                         {"title": "My Cruel Mate", "title_unknown": False, "total": 4})
        self.assertEqual(urlopen.call_args.args[0].full_url,
                         "https://www.dramaboxdb.com/movie/42000023176/")

    @patch("dramafren_downloader.query_details", return_value={"urls": ["https://video.example/1.mp4"]})
    @patch("dramafren_downloader.dramabox_metadata", return_value={
        "title": "My Cruel Mate", "title_unknown": False, "total": 62})
    def test_discover_dramabox_uses_requested_episode(self, metadata, video):
        info = discover(DRAMABOX_URL.replace("ep=1", "ep=20"))
        self.assertEqual(info["total"], 62)
        video.assert_called_once_with("42000023176", 20, 1, "en", source="dramabox")

    @patch("dramafren_downloader.urllib.request.urlopen")
    def test_reelshort_official_metadata(self, urlopen):
        payload = {"props": {"pageProps": {"data": {
            "book_id": "6a31012f0a4db3ff73065e0f", "book_title": "Mated to the Alpha and His Beta", "total": 50}}}}
        urlopen.return_value = io.BytesIO(
            f'<script id="__NEXT_DATA__">{json.dumps(payload)}</script>'.encode())
        self.assertEqual(reelshort_metadata("6a31012f0a4db3ff73065e0f"),
                         {"title": "Mated to the Alpha and His Beta", "total": 50})
        self.assertIn("www.reelshort.com/movie/", urlopen.call_args.args[0].full_url)

    @patch("dramafren_downloader.reelshort_metadata", return_value={"title": "Mated to the Alpha and His Beta", "total": 50})
    @patch("dramafren_downloader.query_details")
    def test_reelshort_never_uses_idrama_api(self, api, _):
        with self.assertRaisesRegex(RuntimeError, "Mated to the Alpha and His Beta.*API vidéo DramaFren non identifiée"):
            discover(REELSHORT_URL)
        api.assert_not_called()

    @patch("dramafren_downloader.query_details")
    def test_moboreels_detail_is_not_treated_as_video_or_idrama(self, api):
        with self.assertRaisesRegex(RuntimeError, "MoboReels.*fiche.*API vidéo"):
            discover(MOBOREELS_URL)
        api.assert_not_called()

    @patch("dramafren_downloader.anyreel_request", return_value={
        "id": "6a1908fe81203cd544a0531d", "provider": "anyreel",
        "title": "Rejected by the Alpha, Claimed by the Northern King", "episodes": 60})
    def test_anyreel_metadata_uses_detail_not_episode_number(self, request):
        info = anyreel_metadata("6a1908fe81203cd544a0531d", "en")
        self.assertEqual(info["total"], 60)
        request.assert_called_once_with("detail", "6a1908fe81203cd544a0531d", "en")

    @patch("dramafren_downloader.anyreel_request", return_value={
        "episodeNumber": 2, "totalEpisodes": 2, "locked": False,
        "videoUrl": "https://cdn.example/ep2.m3u8", "qualityList": [{"url": "https://cdn.example/ep2.m3u8"}]})
    def test_anyreel_video_deduplicates_urls_and_respects_episode(self, request):
        data = anyreel_video("6a1908fe81203cd544a0531d", 2, 1, "en")
        self.assertEqual(data["urls"], ["https://cdn.example/ep2.m3u8"])
        request.assert_called_once_with("video", "6a1908fe81203cd544a0531d", "en", 15,
                                        ep=2, server=1, cv="v21")

    @patch("dramafren_downloader.anyreel_request", return_value={"episodeNumber": 2, "locked": True,
                                                                   "videoUrl": "https://cdn.example/ep2.m3u8"})
    def test_anyreel_locked_video_is_not_downloaded(self, _):
        with self.assertRaisesRegex(RuntimeError, "accès restreint"):
            anyreel_video("6a1908fe81203cd544a0531d", 2, 1, "en")

    @patch("dramafren_downloader.anyreel_metadata", return_value={
        "title": "Rejected by the Alpha", "title_unknown": False, "total": 60})
    @patch("dramafren_downloader.query_details", return_value={"urls": ["https://cdn.example/ep2.m3u8"]})
    def test_discover_anyreel_rechecks_video(self, video, _):
        self.assertEqual(discover(ANYREEL_URL)["total"], 60)
        video.assert_called_once_with("6a1908fe81203cd544a0531d", 2, 1, "en", source="anyreel")

    @patch("dramafren_downloader.urllib.request.urlopen")
    def test_kiss18_metadata_uses_real_catalogue_count_and_chunk(self, urlopen):
        story = {"id": KISS18_ID, "public_slug": "forbidden-guest-house", "title": "Forbidden Guest House",
                 "episodes": [{"id": f"{KISS18_ID}:ep:{ep}", "ordinal": ep, "playable": ep != 2,
                               "playbackChunk": KISS18_CHUNK} for ep in range(1, 4)]}
        urlopen.return_value = io.BytesIO(("// generated\nexport const snapshot = " +
                                            json.dumps({"items": [story]}) + ";").encode())
        kiss18_metadata.cache_clear()
        info = kiss18_metadata("forbidden-guest-house")
        self.assertEqual(info["total"], 3)
        self.assertEqual(info["chunks"], {1: KISS18_CHUNK, 3: KISS18_CHUNK})
        self.assertIn("catalogue-browse.mjs", urlopen.call_args.args[0].full_url)
        kiss18_metadata.cache_clear()

    @patch("dramafren_downloader.urllib.request.urlopen")
    @patch("dramafren_downloader.kiss18_metadata", return_value={"chunks": {2: KISS18_CHUNK}})
    def test_kiss18_video_selects_requested_episode_only(self, _, urlopen):
        def candidate(ep, url):
            return {"episodeId": f"{KISS18_ID}:ep:{ep}", "format": "hls", "refreshable": False, "url": url}
        urlopen.return_value = io.BytesIO(json.dumps({"playback": {
            "wrong": candidate(1, "https://cdn.kiss18.vip/episode1.m3u8"),
            "right": candidate(2, "https://cdn.kiss18.vip/episode2.m3u8"),
            "other": candidate(2, "https://other.example/episode2.m3u8"),
        }}).encode())
        self.assertEqual(kiss18_video("forbidden-guest-house", 2)["urls"],
                         ["https://cdn.kiss18.vip/episode2.m3u8"])
        self.assertIn(KISS18_CHUNK, urlopen.call_args.args[0].full_url)

    @patch("dramafren_downloader.urllib.request.urlopen")
    @patch("dramafren_downloader.kiss18_metadata", return_value={"chunks": {2: KISS18_CHUNK}})
    def test_kiss18_rejects_missing_or_refreshable_video(self, _, urlopen):
        entry = {"episodeId": f"{KISS18_ID}:ep:2", "format": "hls", "refreshable": True,
                 "url": "https://cdn.kiss18.vip/episode2.m3u8"}
        urlopen.return_value = io.BytesIO(json.dumps({"playback": {"candidate": entry}}).encode())
        with self.assertRaisesRegex(RuntimeError, "Aucune vidéo"):
            kiss18_video("forbidden-guest-house", 2)
        with self.assertRaisesRegex(RuntimeError, "non disponible"):
            kiss18_video("forbidden-guest-house", 3)

    @patch("dramafren_downloader.kiss18_metadata", return_value={
        "title": "Forbidden Guest House", "title_unknown": False, "total": 27, "chunks": {2: KISS18_CHUNK}})
    @patch("dramafren_downloader.query_details", return_value={"urls": ["https://cdn.kiss18.vip/ep2.m3u8"]})
    def test_discover_kiss18_uses_path_episode_and_checks_video(self, video, _):
        self.assertEqual(discover(KISS18_URL)["total"], 27)
        video.assert_called_once_with("forbidden-guest-house", 2, 1, "en", source="kiss18")
        with self.assertRaisesRegex(ValueError, "supérieur au total"):
            discover(KISS18_URL.replace("/2", "/28"))

    def test_queue_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.json"
            save_queue(path, {"series": [{"state": "téléchargement", "episodes": {"1": {"state": "pause"}}}]})
            recovered = load_queue(path)["series"][0]
            self.assertEqual(recovered["state"], "en attente")
            self.assertEqual(recovered["episodes"]["1"]["state"], "en attente")

    def test_invalid_video_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Episode_01.mp4").write_bytes(b"broken")
            with patch("engine.probe", side_effect=RuntimeError("invalid")):
                self.assertIsNone(existing_video(Path(tmp), 1))

    def test_resume_skips_valid_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Episode_01.mp4"
            path.write_bytes(b"video")
            with patch("engine.probe", return_value=PROBE), patch("engine.query_api") as api:
                result = download_episode({}, 1, Path(tmp), Controller(), lambda *args: None)
                self.assertEqual(result, path)
                api.assert_not_called()

    def test_dramabox_does_not_use_third_party_unlock_flag(self):
        series = {"source": "dramabox", "id": "42000023176", "available_end": 2,
                  "server": 1, "lang": "en"}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            controller = Controller()
            def run(command, line=None):
                (folder / "Episode_03.mp4").write_bytes(b"video")
            with patch("engine.query_api", return_value=["https://video.example/3.mp4"]) as api, \
                 patch.object(controller, "run", run), patch("engine.probe", return_value=PROBE):
                self.assertEqual(download_episode(series, 3, folder, controller, lambda *args: None).name,
                                 "Episode_03.mp4")
                api.assert_called_once_with("42000023176", 3, 1, "en", source="dramabox")
        series.update(title="Test", start=1, end=5)
        with tempfile.TemporaryDirectory() as tmp, patch("engine.dramabox_metadata", return_value={"total": 4}):
            with self.assertRaisesRegex(RuntimeError, "hors des 4"):
                run_series(series, tmp, 1, Controller(), lambda *args: None, lambda _: None, lambda _: False)

    def test_renew_on_failed_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            calls = []
            def run(_, command, line=None):
                calls.append(command[-1])
                if len(calls) == 1:
                    raise RuntimeError("HTTP 403")
                (target / "Episode_01.mp4").write_bytes(b"video")
            controller = Controller()
            with patch("engine.query_api", side_effect=[["https://one"], ["https://two"]]) as api, \
                 patch.object(controller, "run", lambda command, line=None: run(None, command, line)), \
                 patch.object(controller.stop, "wait", return_value=False), patch("engine.probe", return_value=PROBE):
                download_episode({"id": "1", "server": 1, "lang": "en"}, 1, target, controller, lambda *args: None)
                self.assertEqual(calls, ["https://one", "https://two"])
                self.assertEqual(api.call_count, 2)

    def test_merge_refuses_missing_and_incompatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "manquant"):
                merge({"title": "Test"}, Path(tmp), [1], Controller(), lambda _: True)
        other = json.loads(json.dumps(PROBE))
        other["streams"][0]["codec_name"] = "hevc"
        self.assertFalse(compatible([PROBE, other]))

    def test_merge_order_and_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            files = [folder / f"Episode_{i:02d}.mp4" for i in (2, 3)]
            for file in files:
                file.write_bytes(b"video")
            controller = Controller()
            def fake_run(command, line=None):
                if "-f" in command and "null" in command:
                    return
                listing = Path(command[command.index("-i") + 1]).read_text()
                self.assertLess(listing.index("Episode_02"), listing.index("Episode_03"))
                Path(command[-1]).write_bytes(b"merged")
            with patch("engine.probe", side_effect=lambda path: {**PROBE, "format": {"duration": "10" if ".tmp." in str(path) else "5"}}) as checked, \
                 patch.object(controller, "run", fake_run):
                result = merge({"title": "Ma série"}, folder, [2, 3], controller, lambda _: False, keep=False)
                self.assertTrue(result.exists())
                self.assertTrue(all(not f.exists() for f in files))
                self.assertEqual([call.args[0] for call in checked.call_args_list].count(files[0]), 1)
                self.assertEqual([call.args[0] for call in checked.call_args_list].count(files[1]), 1)

    def test_partial_merge_is_not_named_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "Episode_01.mp4").write_bytes(b"video")
            controller = Controller()
            def fake_run(command, line=None):
                if "null" not in command:
                    Path(command[-1]).write_bytes(b"merged")
            with patch("engine.probe", return_value=PROBE), patch.object(controller, "run", fake_run):
                output = merge({"title": "My Cruel Mate", "start": 1, "end": 1, "total": 62},
                               folder, [1], controller, lambda _: False)
                self.assertEqual(output.name, "My_Cruel_Mate_Episodes_01-01.mp4")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg indisponible")
    def test_real_ffmpeg_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for ep in (1, 2):
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={'red' if ep == 1 else 'blue'}:s=64x64:r=10",
                                "-t", "1", "-c:v", "mpeg4", str(folder / f"Episode_{ep:02d}.mp4")],
                               check=True, capture_output=True)
            output = merge({"title": "Test"}, folder, [1, 2], Controller(), lambda _: False)
            self.assertTrue(output.exists())
            self.assertTrue((folder / "Episode_01.mp4").exists())


if __name__ == "__main__":
    unittest.main()

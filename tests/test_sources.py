import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import Controller, download_direct, run_series, verified_direct, write_json_atomic
from sources import direct_filename, discover_any, parse_any


class FakeResponse(io.BytesIO):
    def __init__(self, data, status=200, **headers):
        super().__init__(data)
        self.status = status
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class GenericSourceTests(unittest.TestCase):
    URL = "https://example.org/files/important-archive.zip?version=1"

    def test_recognize_dramas_and_direct_files(self):
        drama = parse_any("https://idrama.dramafren.org/index.php?page=watch&id=123&ep=1")
        self.assertEqual(drama["source"], "idrama")
        file_info = discover_any(self.URL)
        self.assertEqual(file_info["source"], "direct")
        self.assertEqual(file_info["total"], 1)
        self.assertEqual(file_info["filename"], "important-archive.zip")
        self.assertEqual(file_info["file_url"], self.URL)
        self.assertNotEqual(parse_any(self.URL)["id"],
                            parse_any(self.URL + "2")["id"])

    def test_reject_local_and_unknown_urls(self):
        for url in (
            "http://localhost/private.zip",
            "http://127.0.0.1/file.zip",
            "http://10.0.0.1/file.zip",
            "file:///tmp/private.zip",
            "https://example.org/page",
            "https://user:password@example.org/file.zip",
            "https://example.org/files/file.exe",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_any(url)

    def test_direct_download_and_reuse_without_network(self):
        series = discover_any(self.URL)
        updates = []
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with patch("engine.urllib.request.urlopen",
                       return_value=FakeResponse(b"hello world", **{
                           "Content-Length": "11", "ETag": '"version1"'
                       })) as fetch:
                result = download_direct(series, folder, Controller(),
                                         lambda *args: updates.append(args))
                self.assertEqual(result.read_bytes(), b"hello world")
                fetch.assert_called_once()
            self.assertEqual(verified_direct(series, folder), result)
            self.assertEqual(updates[-1][1:3], ("terminé", 100))
            with patch("engine.urllib.request.urlopen") as fetch:
                self.assertEqual(download_direct(series, folder, Controller(),
                                                 lambda *args: None), result)
                fetch.assert_not_called()

    def test_direct_http_range_resume(self):
        series = discover_any(self.URL)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            target = folder / direct_filename(self.URL)
            partial = Path(str(target) + ".part")
            partial.write_bytes(b"hello")
            write_json_atomic(str(target) + ".partial.json", {
                "url": self.URL, "etag": '"version1"', "last_modified": None, "total": 11
            })
            with patch("engine.urllib.request.urlopen",
                       return_value=FakeResponse(b" world", status=206, **{
                           "Content-Length": "6", "Content-Range": "bytes 5-10/11",
                           "ETag": '"version1"'
                       })) as fetch:
                output = download_direct(series, folder, Controller(), lambda *args: None)
                self.assertEqual(output.read_bytes(), b"hello world")
                request = fetch.call_args.args[0]
                self.assertEqual(request.get_header("Range"), "bytes=5-")
                self.assertEqual(request.get_header("If-range"), '"version1"')
            self.assertFalse(partial.exists())

    def test_existing_unverified_file_is_not_overwritten(self):
        series = discover_any(self.URL)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            target = folder / direct_filename(self.URL)
            target.write_bytes(b"personal file")
            with patch("engine.urllib.request.urlopen") as fetch:
                with self.assertRaisesRegex(RuntimeError, "non vérifié"):
                    download_direct(series, folder, Controller(), lambda *args: None)
                fetch.assert_not_called()
            self.assertEqual(target.read_bytes(), b"personal file")

    def test_generic_file_uses_common_scheduler_without_ffmpeg(self):
        series = {**discover_any(self.URL), "state": "en attente",
                  "episodes": {}, "merge": False, "keep": True}
        with tempfile.TemporaryDirectory() as tmp:
            with patch("engine.urllib.request.urlopen",
                       return_value=FakeResponse(b"abc", **{"Content-Length": "3"})):
                run_series(series, tmp, 1, Controller(), lambda *args: None,
                           lambda message: None, lambda message: False)
            self.assertEqual((Path(tmp) / series["folder"] /
                              series["filename"]).read_bytes(), b"abc")


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import Controller

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QSignalBlocker
from PySide6.QtWidgets import QApplication
from app import Window, Worker

DRAMA_URL = "https://idrama.dramafren.org/index.php?page=watch&id=101000643292&ep=5&server=1&lang=en"
INFO = {"id": "101000643292", "title": "Exemple", "total": 3,
        "server": 1, "lang": "en", "title_unknown": False}


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.window = Window(Path(self.tmp.name) / "queue.json")

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.tmp.cleanup()

    def test_mvp_tabs_and_generic_file_preview(self):
        from sources import discover_any
        url = "https://example.org/files/test.zip"
        self.assertEqual(self.window.tabs.count(), 2)
        self.assertEqual(self.window.tabs.tabText(0), "Analyseur de liens")
        self.assertEqual(self.window.tabs.tabText(1), "Téléchargements")
        self.window.url.setText(url)
        with patch("app.discover", return_value=discover_any(url)):
            self.window.analyze_url()
            self.assertTrue(self.window.analyzer.wait(5000))
            self.app.processEvents()
        self.assertFalse(self.window.merge_box.isEnabled())
        self.window.add.click()
        self.assertEqual(self.window.data["series"][0]["source"], "direct")
        self.assertFalse(self.window.data["series"][0]["merge"])
        self.assertEqual(self.window.tabs.currentIndex(), 1)

    def test_analyze_add_and_persist(self):
        self.window.url.setText("https://example.org")
        with patch("app.discover", return_value={"id": "12", "title": "Exemple", "total": 3,
                                                    "server": 1, "lang": "en", "title_unknown": False}):
            self.window.analyze_url()
            self.assertTrue(self.window.analyzer.wait(5000))
            self.app.processEvents()
        self.assertTrue(self.window.add.isEnabled())
        self.window.add.click()
        self.assertEqual(self.window.tree.topLevelItemCount(), 1)
        self.assertEqual(self.window.tree.topLevelItem(0).childCount(), 3)
        self.assertTrue((Path(self.tmp.name) / "queue.json").exists())
        self.window.on_progress("12", 1, "terminé", 100, "", "")
        self.assertEqual(self.window.global_bar.value(), 33)

    def test_error_and_buttons(self):
        self.window.analysis_error("HTTP 403")
        self.assertIn("HTTP 403", self.window.info.text())
        self.window.buttons()
        self.assertFalse(self.window.pause_button.isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.start_button.click()
        self.assertIn("Aucune série", self.window.journal.toPlainText())

    def test_analyze_existing_failed_link_does_not_retry_or_duplicate_error(self):
        url = "https://dramabox.dramafren.org/index.php?page=watch&id=42000023176&ep=1&lang=en&sv=1"
        self.window.data["pending_links"].append({"id": "42000023176", "url": url,
                                                   "state": "erreur", "error": "Cloudflare (HTTP 403)"})
        self.window.url.setText(url)
        with patch("app.discover") as discover:
            self.window.analyze.click()
            discover.assert_not_called()
            self.assertIsNone(self.window.analyzer)
            self.assertIn("Cloudflare", self.window.info.text())

    def test_dramabox_queue_uses_full_catalogue_count(self):
        info = {**INFO, "id": "42000023176", "source": "dramabox", "title": "My Cruel Mate",
                "total": 62}
        self.window.analysis_ok(info)
        self.assertEqual(self.window.end_ep.value(), 62)
        self.assertEqual(self.window.end_ep.maximum(), 62)
        self.assertIn("62 épisodes", self.window.info.text())
        self.window.link_found(info["id"], info)
        self.assertEqual(self.window.data["series"][0]["end"], 62)
        self.assertEqual(self.window.tree.topLevelItem(0).childCount(), 62)

    def test_reelshort_link_stays_in_error_without_video_endpoint(self):
        url = "https://reelshort.dramafren.org/index.php?page=detail&id=6a31012f0a4db3ff73065e0f&lang=en"
        with patch("app.discover", side_effect=RuntimeError("API vidéo DramaFren non identifiée")):
            self.window.queue_link(url, "6a31012f0a4db3ff73065e0f")
            self.assertTrue(self.window.pending_analyzer.wait(5000))
            self.app.processEvents()
        self.assertEqual(self.window.data["series"], [])
        self.assertEqual(self.window.data["pending_links"][0]["state"], "erreur")
        self.assertIn("API vidéo", self.window.journal.toPlainText())

    def test_existing_dramabox_queue_can_extend_old_range(self):
        from engine import save_queue
        state_file = Path(self.tmp.name) / "queue.json"
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        save_queue(state_file, {"series": [{**INFO, "id": "42000023176", "source": "dramabox",
                                           "total": 62, "available_end": 9, "start": 1, "end": 9,
                                           "state": "terminé", "episodes": {}}],
                                "destination": self.tmp.name, "parallel": 1})
        self.window = Window(state_file)
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0))
        self.assertEqual(self.window.end_ep.maximum(), 62)
        self.window.end_ep.setValue(62)
        self.window.apply_options.click()
        self.assertEqual(self.window.data["series"][0]["end"], 62)
        self.assertEqual(self.window.data["series"][0]["state"], "en attente")

    def test_pause_resume_stop(self):
        class RunningWorker:
            control = Controller()

            def isRunning(self):
                return True

        self.window.worker = RunningWorker()
        self.window.data["series"].append({"id": "1", "title": "Test", "start": 1, "end": 1,
                                            "state": "téléchargement", "episodes": {}})
        self.window.pause_downloads()
        self.assertEqual(self.window.data["series"][0]["state"], "pause")
        self.assertTrue(self.window.resume_button.isEnabled())
        self.window.resume_downloads()
        self.assertEqual(self.window.data["series"][0]["state"], "téléchargement")
        self.window.stop_downloads()
        self.assertTrue(self.window.worker.control.stop.is_set())
        self.assertEqual(self.window.data["series"][0]["state"], "en attente")
        self.window.worker = None

    def test_clipboard_detects_without_consuming(self):
        clipboard = self.app.clipboard()
        previous = clipboard.text()
        try:
            clipboard.setText("https://example.org/other")
            self.app.processEvents()
            self.assertEqual(self.window.url.text(), "")
            with patch("app.discover", return_value=INFO):
                clipboard.setText(DRAMA_URL)
                self.assertEqual(self.window.data["pending_links"][0]["id"], INFO["id"])
                self.assertTrue(self.window.start_button.isEnabled())
                self.assertTrue(self.window.pending_analyzer.wait(5000))
                self.app.processEvents()
            self.assertEqual(self.window.url.text(), DRAMA_URL)
            self.assertEqual(clipboard.text(), DRAMA_URL)  # partage intact avec JDownloader
            self.assertFalse(self.window.add.isEnabled())  # pas d'ajout automatique
            self.assertEqual(self.window.data["series"][0]["title"], "Exemple")
        finally:
            self.window.watch_clipboard.setChecked(False)
            clipboard.setText(previous)

    def test_clipboard_preserves_draft_and_can_be_disabled(self):
        clipboard = self.app.clipboard()
        previous = clipboard.text()
        try:
            self.window.url.setText("mon brouillon")
            with patch("app.discover", return_value=INFO):
                clipboard.setText(DRAMA_URL)
                self.assertTrue(self.window.pending_analyzer.wait(5000))
                self.app.processEvents()
            self.assertEqual(self.window.url.text(), "mon brouillon")
            self.assertEqual(self.window.pending_url, DRAMA_URL)
            self.window.use_copied_url()
            self.assertEqual(self.window.url.text(), DRAMA_URL)
            self.window.watch_clipboard.setChecked(False)
            self.window.url.clear()
            clipboard.setText(DRAMA_URL + "&ep=6")
            self.app.processEvents()
            self.assertEqual(self.window.url.text(), "")
        finally:
            self.window.watch_clipboard.setChecked(False)
            clipboard.setText(previous)

    def test_clipboard_polling_when_signal_is_not_emitted(self):
        clipboard = self.app.clipboard()
        previous = clipboard.text()
        try:
            with patch("app.discover", return_value=INFO):
                with QSignalBlocker(clipboard):
                    clipboard.setText(DRAMA_URL)
                self.assertEqual(self.window.url.text(), "")
                self.window.clipboard_timer.timeout.emit()
                self.assertTrue(self.window.pending_analyzer.wait(5000))
                self.app.processEvents()
            self.assertEqual(self.window.url.text(), DRAMA_URL)
            self.assertEqual(clipboard.text(), DRAMA_URL)
        finally:
            self.window.watch_clipboard.setChecked(False)
            clipboard.setText(previous)

    def test_queue_runs_all_series_in_order(self):
        first = {**INFO, "id": "1", "title": "Première"}
        second = {**INFO, "id": "2", "title": "Deuxième"}
        self.window.link_found("1", first)
        self.window.link_found("2", second)
        self.assertEqual(self.window.tree.topLevelItemCount(), 2)
        self.window.data["series"][0]["state"] = "terminé"
        with patch("app.Worker") as worker_class:
            worker = worker_class.return_value
            worker.isRunning.return_value = False
            self.window.destination.setText(self.tmp.name)
            self.window.start_button.click()
            jobs = worker_class.call_args.args[0]
            self.assertEqual([job["id"] for job in jobs], ["2"])
            worker.start.assert_called_once()
        self.window.worker = None

    def test_fast_preset_keeps_default_concurrency_configurable(self):
        self.assertEqual(self.window.parallel.value(), 1)
        self.window.fast_button.click()
        self.assertEqual(self.window.parallel.value(), 2)
        self.window.parallel.setValue(3)
        self.assertEqual(self.window.parallel.value(), 3)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.window = Window(Path(self.tmp.name) / "queue.json")
        self.assertEqual(self.window.parallel.value(), 3)

    def test_worker_continues_with_next_series_after_error(self):
        jobs = [{**INFO, "id": "1", "title": "Une"}, {**INFO, "id": "2", "title": "Deux"}]
        worker = Worker(jobs, self.tmp.name, 1)
        visited = []
        states = []
        worker.state.connect(lambda ident, state: states.append((ident, state)))
        def process(series, *_):
            visited.append(series["id"])
            if series["id"] == "1":
                raise RuntimeError("HTTP 403")
        with patch("app.run_series", side_effect=process):
            worker.run()
        self.assertEqual(visited, ["1", "2"])
        self.assertEqual(states, [("idrama:1", "téléchargement"), ("idrama:1", "erreur"),
                                  ("idrama:2", "téléchargement"), ("idrama:2", "terminé")])
        worker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_two_copied_links_analyzed_in_queue_order(self):
        release = threading.Event()
        first_url = DRAMA_URL.replace("101000643292", "1")
        second_url = DRAMA_URL.replace("101000643292", "2")
        def discover_in_order(url):
            ident = "1" if "id=1&" in url else "2"
            if ident == "1":
                release.wait(3)
            return {**INFO, "id": ident, "title": "Série " + ident}

        with patch("app.discover", side_effect=discover_in_order):
            self.window.queue_link(first_url, "1")
            self.window.queue_link(second_url, "2")
            self.assertEqual(self.window.tree.topLevelItemCount(), 2)
            self.assertTrue(self.window.start_button.isEnabled())
            release.set()
            deadline = time.monotonic() + 3
            while len(self.window.data["series"]) < 2 and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual([s["id"] for s in self.window.data["series"]], ["1", "2"])
            self.assertEqual(self.window.data["pending_links"], [])
            self.assertTrue(self.window.start_button.isEnabled())

    def test_failed_link_retry_remove_and_recovery(self):
        self.window.data["pending_links"].append({"id": "42", "url": DRAMA_URL,
                                                   "state": "erreur", "error": "HTTP 403"})
        self.window.persist()
        self.window.render()
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0))
        self.assertTrue(self.window.retry_link.isEnabled())
        with patch.object(self.window, "next_link") as next_link:
            self.window.retry_link.click()
            next_link.assert_called_once()
        self.assertEqual(self.window.data["pending_links"][0]["state"], "en attente")
        self.window.link_failed("42", "Erreur API")
        self.assertEqual(self.window.data["pending_links"][0]["state"], "erreur")
        self.window.remove_selected.click()
        self.assertEqual(self.window.data["pending_links"], [])
        self.window.data["pending_links"].append({"id": "42", "url": DRAMA_URL,
                                                   "state": "analyse", "error": ""})
        self.window.persist()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.window = Window(Path(self.tmp.name) / "queue.json")
        self.assertEqual(self.window.data["pending_links"][0]["state"], "en attente")

    def test_options_change_requeues_completed_series_without_erasing_episodes(self):
        self.window.link_found("77", {**INFO, "id": "77", "source": "idrama"})
        series = self.window.data["series"][0]
        series["state"] = "terminé"
        series["episodes"]["1"] = {"state": "terminé", "percent": 100, "speed": ""}
        self.window.render()
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0))
        self.window.merge_box.setChecked(False)
        self.window.apply_options.click()
        self.assertEqual(series["state"], "en attente")
        self.assertEqual(series["episodes"]["1"]["percent"], 100)
        self.assertFalse(series["merge"])
        series["state"] = "terminé"
        self.window.render()
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0))
        self.window.keep_box.setChecked(False)
        self.window.apply_options.click()
        self.assertEqual(series["state"], "en attente")

    def test_rename_does_not_change_pinned_folder(self):
        self.window.destination.setText(self.tmp.name)
        self.window.link_found("77", {**INFO, "id": "77", "source": "idrama"})
        item = self.window.tree.topLevelItem(0)
        previous = self.window.data["series"][0]["folder"]
        item.setText(0, "Titre modifié")
        self.assertEqual(self.window.data["series"][0]["title"], "Titre modifié")
        self.assertEqual(self.window.data["series"][0]["folder"], previous)

    def test_same_external_id_from_different_sources_is_not_a_duplicate(self):
        self.window.destination.setText(self.tmp.name)
        self.window.link_found("77", {**INFO, "id": "77", "source": "idrama"})
        self.window.link_found("77", {**INFO, "id": "77", "source": "dramabox"})
        self.assertEqual(len(self.window.data["series"]), 2)
        self.assertNotEqual(self.window.data["series"][0]["folder"],
                            self.window.data["series"][1]["folder"])
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1))
        self.window.remove_selected.click()
        self.assertEqual([s["source"] for s in self.window.data["series"]], ["idrama"])

    def test_pending_analysis_does_not_block_ready_downloads(self):
        self.window.destination.setText(self.tmp.name)
        self.window.link_found("7", {**INFO, "id": "7"})
        self.window.data["pending_links"].append({
            "id": "8", "source": "idrama", "url": DRAMA_URL,
            "state": "en attente", "error": ""})
        self.window.buttons()
        self.assertTrue(self.window.start_button.isEnabled())
        with patch("app.Worker") as worker_class:
            worker = worker_class.return_value
            worker.isRunning.return_value = False
            self.window.start_button.click()
            self.assertEqual(len(worker_class.call_args.args[0]), 1)
        self.window.worker = None

    def test_corrupt_queue_is_preserved_instead_of_overwritten(self):
        state_file = Path(self.tmp.name) / "queue.json"
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        state_file.write_text("{not valid json", encoding="utf-8")
        with patch("app.QMessageBox.warning") as warning:
            self.window = Window(state_file)
            self.assertTrue(warning.called)
        self.assertNotEqual(self.window.state_file, state_file)
        self.window.close()
        self.assertEqual(state_file.read_text(encoding="utf-8"), "{not valid json")
        self.assertTrue(self.window.state_file.exists())
    def test_manual_verification_dialog_explains_retry(self):
        from browser_session import BrowserVerificationDialog
        dialog = BrowserVerificationDialog("https://example.org/video")
        self.assertIn("Vérification", dialog.windowTitle())
        self.assertIn("Réessayer", dialog.retry_button.text())
        dialog.retry_button.click()
        self.assertEqual(dialog.result(), 1)
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_cloudflare_error_requires_human_intervention(self):
        jobs = [{**INFO, "id": "1", "source": "idrama", "title": "Un"}]
        worker = Worker(jobs, self.tmp.name, 1)
        states = []
        worker.state.connect(lambda key, state: states.append((key, state)))
        with patch("app.run_series", side_effect=RuntimeError("Cloudflare (HTTP 403)")):
            worker.run()
        self.assertEqual(states[-1], ("idrama:1", "intervention requise"))
        worker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_retry_after_verification_rechecks_the_actual_source(self):
        url = DRAMA_URL
        link = {"id": "101000643292", "source": "idrama",
                "url": url, "state": "erreur", "error": "Cloudflare (HTTP 403)"}
        self.window.data["pending_links"].append(link)
        with patch("app.BrowserVerificationDialog") as dialog_class, \
             patch.object(self.window, "next_link") as next_link:
            dialog_class.return_value.exec.return_value = 1
            self.window.open_verification(url, "idrama:101000643292")
            next_link.assert_called_once()
        self.assertEqual(link["state"], "en attente")
        self.assertEqual(link["error"], "")


if __name__ == "__main__":
    unittest.main()

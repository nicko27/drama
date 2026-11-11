"""Interface Qt du téléchargeur DramaFren."""
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget)

from sources import discover_any as discover, parse_any as parse_url
from engine import (Cancelled, Controller, assign_folder, load_queue, run_series,
                    save_queue, series_identity)

def pending_identity(link):
    """Resolve old queue entries that predate the explicit source field."""
    source = link.get("source")
    if not source:
        try:
            source = parse_url(link["url"])["source"]
        except ValueError:
            source = "idrama"
    return f"{source}:{link['id']}"


STATE_FILE = Path.home() / "Library" / "Application Support" / "DramaFren Downloader" / "queue.json"
ICON_FILE = Path(__file__).resolve().parent / "assets" / "icon.png"


class Analyzer(QThread):
    found = Signal(dict)
    failed = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            self.found.emit(discover(self.url))
        except Exception as exc:
            self.failed.emit(str(exc))


class Worker(QThread):
    progress = Signal(str, int, str, float, str, str)
    state = Signal(str, str)
    log = Signal(str)
    ask = Signal(str)

    def __init__(self, jobs, destination, parallel):
        super().__init__()
        self.jobs = jobs
        self.destination = destination
        self.parallel = parallel
        self.control = Controller()
        self.answer = threading.Event()
        self.approved = False
        self.last_progress = {}

    def report(self, ident, ep, state, percent, speed, message):
        key = (ident, ep)
        now = time.monotonic()
        previous = self.last_progress.get(key)
        if state == "téléchargement" and previous and previous[0] == state and now - previous[1] < 0.5:
            return
        self.last_progress[key] = (state, now)
        self.progress.emit(ident, ep, state, percent, speed, message)

    def prompt(self, text):
        self.answer.clear()
        self.ask.emit(text)
        while not self.control.stop.is_set():
            if self.answer.wait(0.2):
                return self.approved
        raise Cancelled()

    def run(self):
        for series in self.jobs:
            if self.control.stop.is_set():
                break
            ident = series_identity(series)
            self.state.emit(ident, "téléchargement")
            try:
                run_series(series, self.destination, self.parallel, self.control,
                           lambda ep, state, percent, speed, message: self.report(
                               ident, ep, state, percent, speed, message), self.log.emit, self.prompt)
                self.state.emit(ident, "terminé")
            except Cancelled:
                self.state.emit(ident, "en attente")
                break
            except Exception as exc:
                self.log.emit(f"{series['title']} : {exc}")
                self.state.emit(ident, "erreur")


class Window(QMainWindow):
    def __init__(self, state_file=STATE_FILE):
        super().__init__()
        self.state_file = Path(state_file)
        try:
            self.data = load_queue(state_file)
        except (ValueError, OSError) as exc:
            # Do not overwrite the unreadable queue, even when the window closes.
            original = self.state_file
            self.state_file = original.with_name(
                f"{original.stem}.recuperation-{time.strftime('%Y%m%d-%H%M%S')}.json")
            self.data = {"series": [], "pending_links": [],
                         "destination": str(Path.home() / "Downloads"), "parallel": 1}
            QMessageBox.warning(self, "File illisible",
                                f"{exc}\n\nLa file originale reste intacte : {original}\n"
                                f"Cette session sera enregistrée dans : {self.state_file}")
        self.data.setdefault("pending_links", [])
        used_folders = set()
        for series in self.data["series"]:
            if series.get("source") == "dramabox":
                series.pop("available_end", None)  # ancienne limite erronée du catalogue tiers
            assign_folder(series, self.data["destination"], reserved=used_folders)
            used_folders.add(series["folder"])
        for link in self.data["pending_links"]:
            if "source" not in link:
                try:
                    link["source"] = parse_url(link["url"])["source"]
                except ValueError:
                    link["source"] = "idrama"
            if link["state"] == "analyse":
                link["state"] = "en attente"
        self.preview = None
        self.analyzer = None
        self.pending_analyzer = None
        self.pending_current_id = None
        self.closing = False
        self.worker = None
        self.progress_timer = QTimer(self)
        self.progress_timer.setSingleShot(True)
        self.progress_timer.setInterval(650)
        self.progress_timer.timeout.connect(self.flush_progress)
        self.pending_url = None
        self.last_clipboard_text = QApplication.clipboard().text().strip()
        self.setWindowTitle("Drama Downloader")
        self.setWindowIcon(QIcon(str(ICON_FILE)))
        self.resize(1000, 660)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.tabs = QTabWidget()
        analysis_page = QWidget()
        downloads_page = QWidget()
        analysis_layout = QVBoxLayout(analysis_page)
        downloads_layout = QVBoxLayout(downloads_page)
        self.tabs.addTab(analysis_page, "Analyseur de liens")
        self.tabs.addTab(downloads_page, "Téléchargements")
        layout.addWidget(self.tabs)
        top = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("Coller une URL de série ou de fichier HTTP/HTTPS…")
        self.analyze = QPushButton("Analyser")
        self.add = QPushButton("Ajouter à la file")
        self.add.setEnabled(False)
        top.addWidget(self.url, 1)
        top.addWidget(self.analyze)
        top.addWidget(self.add)
        analysis_layout.addLayout(top)
        clipboard_row = QHBoxLayout()
        self.watch_clipboard = QCheckBox("Détecter les liens copiés")
        self.watch_clipboard.setChecked(True)
        self.use_clipboard = QPushButton("Utiliser le lien copié")
        self.use_clipboard.setVisible(False)
        clipboard_row.addWidget(self.watch_clipboard)
        clipboard_row.addWidget(self.use_clipboard)
        clipboard_row.addStretch()
        analysis_layout.addLayout(clipboard_row)
        self.info = QLabel("Collez une URL pour identifier la série.")
        analysis_layout.addWidget(self.info)
        options = QHBoxLayout()
        self.start_ep = QSpinBox()
        self.end_ep = QSpinBox()
        for field in (self.start_ep, self.end_ep):
            field.setRange(1, 10000)
        self.merge_box = QCheckBox("Fusion automatique")
        self.merge_box.setChecked(True)
        self.keep_box = QCheckBox("Conserver les épisodes")
        self.keep_box.setChecked(True)
        self.apply_options = QPushButton("Appliquer à la série")
        for widget in (QLabel("De"), self.start_ep, QLabel("à"), self.end_ep,
                       self.merge_box, self.keep_box, self.apply_options):
            options.addWidget(widget)
        options.addStretch()
        analysis_layout.addLayout(options)
        self.analysis_tree = QTreeWidget()
        self.analysis_tree.setHeaderLabels(["Lien / fichier", "Éléments", "État"])
        analysis_layout.addWidget(self.analysis_tree, 1)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Nom / épisode", "Épisodes", "Progression", "Vitesse", "État"])
        self.tree.setColumnWidth(0, 280)
        self.tree.itemChanged.connect(self.rename_series)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        downloads_layout.addWidget(self.tree, 1)
        queue_controls = QHBoxLayout()
        self.retry_link = QPushButton("Réessayer l’analyse")
        self.remove_selected = QPushButton("Retirer la sélection")
        self.edit_selected = QPushButton("Options de la sélection")
        queue_controls.addWidget(self.retry_link)
        queue_controls.addWidget(self.remove_selected)
        queue_controls.addWidget(self.edit_selected)
        queue_controls.addStretch()
        downloads_layout.addLayout(queue_controls)
        self.global_bar = QProgressBar()
        downloads_layout.addWidget(self.global_bar)
        bottom = QGridLayout()
        self.destination = QLineEdit(self.data.get("destination", str(Path.home() / "Downloads")))
        browse = QPushButton("Parcourir…")
        bottom.addWidget(QLabel("Dossier :"), 0, 0)
        bottom.addWidget(self.destination, 0, 1, 1, 5)
        bottom.addWidget(browse, 0, 6)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 3)
        self.parallel.setValue(self.data.get("parallel", 1))
        self.fast_button = QPushButton("Rapide (2)")
        self.fast_button.setToolTip("Deux épisodes en parallèle ; réglage manuel possible de 1 à 3.")
        bottom.addWidget(QLabel("Épisodes simultanés :"), 1, 0)
        bottom.addWidget(self.parallel, 1, 1)
        bottom.addWidget(self.fast_button, 1, 2)
        self.start_button = QPushButton("Démarrer")
        self.pause_button = QPushButton("Pause")
        self.resume_button = QPushButton("Reprendre")
        self.stop_button = QPushButton("Arrêter")
        for col, button in enumerate((self.start_button, self.pause_button, self.resume_button, self.stop_button), 3):
            bottom.addWidget(button, 1, col)
        downloads_layout.addLayout(bottom)
        downloads_layout.addWidget(QLabel("Journal d’activité"))
        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumHeight(100)
        downloads_layout.addWidget(self.journal)
        self.setStyleSheet("QMainWindow{background:#f4f6f9} QPushButton{padding:6px 10px} "
                           "QTreeWidget,QPlainTextEdit,QLineEdit{background:white;border:1px solid #ccd3dd;border-radius:4px} "
                           "QProgressBar{border:1px solid #ccd3dd;border-radius:4px;text-align:center} "
                           "QProgressBar::chunk{background:#4b88cf}")
        self.analyze.clicked.connect(self.analyze_url)
        self.add.clicked.connect(self.add_series)
        self.apply_options.clicked.connect(self.apply_to_series)
        self.retry_link.clicked.connect(self.retry_selected)
        self.remove_selected.clicked.connect(self.remove_selection)
        self.edit_selected.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        self.url.textChanged.connect(lambda: self.add.setEnabled(False))
        self.watch_clipboard.toggled.connect(self.clipboard_toggled)
        self.use_clipboard.clicked.connect(self.use_copied_url)
        QApplication.clipboard().dataChanged.connect(self.clipboard_changed)
        self.clipboard_timer = QTimer(self)
        self.clipboard_timer.setInterval(1000)
        self.clipboard_timer.timeout.connect(self.clipboard_changed)
        self.clipboard_timer.start()
        browse.clicked.connect(self.browse)
        self.destination.editingFinished.connect(self.persist)
        self.parallel.valueChanged.connect(self.persist)
        self.fast_button.clicked.connect(lambda: self.parallel.setValue(2))
        self.start_button.clicked.connect(self.start_downloads)
        self.pause_button.clicked.connect(self.pause_downloads)
        self.resume_button.clicked.connect(self.resume_downloads)
        self.stop_button.clicked.connect(self.stop_downloads)
        self.render()
        self.buttons()
        QTimer.singleShot(0, self.next_link)

    def clipboard_toggled(self, enabled):
        if not enabled:
            self.pending_url = None
            self.use_clipboard.setVisible(False)
        self.last_clipboard_text = QApplication.clipboard().text().strip()

    def clipboard_changed(self):
        """Lecture seule du presse-papiers : aucun retrait de lien pour JDownloader."""
        text = QApplication.clipboard().text().strip()
        if text == self.last_clipboard_text:
            return
        self.last_clipboard_text = text
        if not self.watch_clipboard.isChecked():
            return
        try:
            parsed = parse_url(text)
        except ValueError:
            return
        self.queue_link(text, parsed["id"])
        if not self.url.text().strip():
            self.url.setText(text)
            self.info.setText("Lien ajouté à la file ; analyse en arrière-plan.")
        elif self.url.text().strip() != text:
            self.pending_url = text
            self.use_clipboard.setVisible(True)
            self.info.setText("Lien ajouté à la file ; le champ actuel est conservé.")

    def queue_link(self, url, ident):
        source = parse_url(url)["source"]
        key = f"{source}:{ident}"
        if any(series_identity(s) == key for s in self.data["series"]) or any(
                pending_identity(l) == key for l in self.data["pending_links"]):
            return
        self.data["pending_links"].append(
            {"id": ident, "source": source, "url": url, "state": "en attente", "error": ""})
        self.persist()
        self.render()
        self.next_link()

    def next_link(self):
        if self.closing or self.pending_current_id is not None:
            return
        link = next((l for l in self.data["pending_links"] if l["state"] == "en attente"), None)
        if not link:
            self.buttons()
            return
        link["state"] = "analyse"
        self.persist()
        self.render()
        self.buttons()
        self.pending_current_id = pending_identity(link)
        self.pending_analyzer = Analyzer(link["url"])
        self.pending_analyzer.found.connect(
            lambda info, key=self.pending_current_id: self.link_found(key, info))
        self.pending_analyzer.failed.connect(
            lambda error, key=self.pending_current_id: self.link_failed(key, error))
        self.pending_analyzer.finished.connect(self.pending_finished)
        self.pending_analyzer.start()

    def pending_finished(self):
        if self.pending_analyzer and self.pending_analyzer.isRunning():
            QTimer.singleShot(50, self.pending_finished)
            return
        self.pending_current_id = None
        self.next_link()

    def link_found(self, ident, info):
        if self.closing:
            return
        key = series_identity(info)
        self.data["pending_links"] = [
            l for l in self.data["pending_links"] if pending_identity(l) != key]
        if not any(series_identity(s) == key for s in self.data["series"]):
            series = {**info, "start": 1, "end": info["total"],
                      "merge": info.get("source") not in ("direct", "ytdlp") and self.merge_box.isChecked(),
                      "keep": self.keep_box.isChecked(),
                      "state": "en attente", "episodes": {}}
            assign_folder(series, self.destination.text(),
                          reserved={item["folder"] for item in self.data["series"]})
            self.data["series"].append(series)
            self.journal.appendPlainText(f"Ajouté à la file : {info['title']} ({info['total']} épisodes)")
            if info.get("title_unknown"):
                self.journal.appendPlainText("Titre indisponible : double-cliquez sur le nom pour le modifier.")
        self.persist()
        self.render()
        self.buttons()

    def link_failed(self, ident, error):
        if self.closing:
            return
        for link in self.data["pending_links"]:
            if pending_identity(link) == ident or (":" not in ident and link["id"] == ident):
                link["state"] = "erreur"
                link["error"] = error
        self.journal.appendPlainText(f"Analyse {ident} : {error}")
        self.persist()
        self.render()
        self.buttons()

    def use_copied_url(self):
        if self.pending_url:
            self.url.setText(self.pending_url)
            self.info.setText("Lien copié chargé ; il est déjà dans la file.")
        self.pending_url = None
        self.use_clipboard.setVisible(False)

    def persist(self):
        self.data["destination"] = self.destination.text()
        self.data["parallel"] = self.parallel.value()
        try:
            save_queue(self.state_file, self.data)
        except OSError as exc:
            self.journal.appendPlainText(f"Sauvegarde impossible : {exc}")

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Dossier de destination", self.destination.text())
        if folder:
            self.destination.setText(folder)
            self.persist()

    def analyze_url(self):
        if self.analyzer and self.analyzer.isRunning():
            return
        url = self.url.text().strip()
        try:
            parsed = parse_url(url)
            wanted = f"{parsed['source']}:{parsed['id']}"
            link = next((link for link in self.data["pending_links"]
                         if pending_identity(link) == wanted), None)
            if link:
                if link["state"] == "erreur":
                    self.info.setText(f"Déjà dans la file : {link['error']} — sélectionnez la ligne pour réessayer.")
                else:
                    self.info.setText("Ce lien est déjà dans la file ; analyse en cours ou en attente.")
                return
        except ValueError:
            pass
        self.preview = None
        self.add.setEnabled(False)
        self.analyze.setEnabled(False)
        self.info.setText("Analyse en cours…")
        self.analyzer = Analyzer(url)
        self.analyzer.found.connect(self.analysis_ok)
        self.analyzer.failed.connect(self.analysis_error)
        self.analyzer.finished.connect(self.analysis_finished)
        self.analyzer.start()

    def analysis_finished(self):
        if not self.closing:
            self.analyze.setEnabled(True)

    def analysis_ok(self, info):
        self.preview = info
        self.merge_box.setEnabled(info.get("source") not in ("direct", "ytdlp"))
        self.merge_box.setChecked(info.get("source") not in ("direct", "ytdlp"))
        self.start_ep.setValue(1)
        maximum = info["total"]
        self.end_ep.setValue(maximum)
        self.start_ep.setMaximum(maximum)
        self.end_ep.setMaximum(maximum)
        label = " (titre indisponible, modifiable dans la file)" if info["title_unknown"] else ""
        detail = ("fichier direct" if info.get("source") == "direct" else
                  "vidéo reconnue par yt-dlp" if info.get("source") == "ytdlp" else
                  f"{info['total']} épisodes — langue {info['lang']} — serveur {info['server']}")
        self.info.setText(f"{info['title']}{label} — {detail}")
        self.analysis_tree.clear()
        self.analysis_tree.addTopLevelItem(QTreeWidgetItem(
            [info["title"], str(info["total"]), "Prêt à ajouter"]))
        self.add.setEnabled(True)

    def analysis_error(self, error):
        self.info.setText(f"Erreur d’analyse : {error}")
        self.journal.appendPlainText(error)

    def add_series(self):
        if not self.preview or self.start_ep.value() > self.end_ep.value() or self.end_ep.value() > self.preview["total"]:
            QMessageBox.warning(self, "Plage invalide", "Vérifiez la plage d’épisodes.")
            return
        if any(series_identity(s) == series_identity(self.preview) for s in self.data["series"]):
            QMessageBox.information(self, "Déjà présent", "Cette série est déjà dans la file.")
            return
        series = {**self.preview, "start": self.start_ep.value(), "end": self.end_ep.value(),
                  "merge": self.preview.get("source") not in ("direct", "ytdlp") and self.merge_box.isChecked(),
                  "keep": self.keep_box.isChecked(),
                  "state": "en attente", "episodes": {}}
        assign_folder(series, self.destination.text(),
                      reserved={item["folder"] for item in self.data["series"]})
        self.data["series"].append(series)
        self.data["pending_links"] = [
            l for l in self.data["pending_links"] if pending_identity(l) != series_identity(series)]
        self.persist()
        self.preview = None
        self.render()
        self.add.setEnabled(False)
        self.tabs.setCurrentIndex(1)

    def render(self):
        self.analysis_tree.clear()
        for link in self.data["pending_links"]:
            self.analysis_tree.addTopLevelItem(QTreeWidgetItem(
                [link["url"], "", link["state"]]))
        if self.preview:
            self.analysis_tree.addTopLevelItem(QTreeWidgetItem(
                [self.preview["title"], str(self.preview["total"]), "Prêt à ajouter"]))
        selected = self.selected_entry()
        expanded = {item.data(0, Qt.ItemDataRole.UserRole) for item in
                    (self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount()))
                    if item.isExpanded()}
        self.tree.blockSignals(True)
        self.tree.clear()
        for link in self.data["pending_links"]:
            item = QTreeWidgetItem(self.tree, [f"Lien {link['id']}", "à analyser", "", "", link["state"]])
            item.setData(0, Qt.ItemDataRole.UserRole, ("pending", pending_identity(link)))
            item.setToolTip(0, link["url"])
            if link["error"]:
                item.setToolTip(4, link["error"])
        for series in self.data["series"]:
            item = QTreeWidgetItem(self.tree, [series["title"], f"{series['start']}–{series['end']}", "", "", series["state"]])
            item.setData(0, Qt.ItemDataRole.UserRole, ("series", series_identity(series)))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            for ep in range(series["start"], series["end"] + 1):
                status = series.get("episodes", {}).get(str(ep), {})
                child = QTreeWidgetItem(item, [f"Épisode {ep:02d}", "", "", status.get("speed", ""), status.get("state", "en attente")])
                bar = QProgressBar()
                bar.setValue(int(status.get("percent", 0)))
                self.tree.setItemWidget(child, 2, bar)
            item.setExpanded(("series", series["id"]) in expanded)
        if selected:
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                if item.data(0, Qt.ItemDataRole.UserRole) == selected:
                    self.tree.setCurrentItem(item)
                    break
        self.update_global()
        self.tree.blockSignals(False)
        self.selection_changed()

    def selected_entry(self):
        item = self.tree.currentItem()
        if item and item.parent():
            item = item.parent()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def selection_changed(self):
        entry = self.selected_entry()
        busy = self.worker is not None and self.worker.isRunning()
        self.remove_selected.setEnabled(bool(entry) and not busy and
                                        (entry[0] != "pending" or not self.link_is_running(entry[1])))
        self.retry_link.setEnabled(bool(entry) and entry[0] == "pending" and any(
            pending_identity(l) == entry[1] and l["state"] == "erreur" for l in self.data["pending_links"]))
        self.apply_options.setEnabled(bool(entry) and entry[0] == "series" and not busy)
        self.edit_selected.setEnabled(bool(entry) and entry[0] == "series" and not busy)
        if entry and entry[0] == "series":
            series = self.series_for(entry[1])
            maximum = series["total"]
            self.start_ep.setMaximum(maximum)
            self.end_ep.setMaximum(maximum)
            self.start_ep.setValue(series["start"])
            self.end_ep.setValue(series["end"])
            self.merge_box.setEnabled(series.get("source") not in ("direct", "ytdlp"))
            self.merge_box.setChecked(series.get("merge", True) and series.get("source") not in ("direct", "ytdlp"))
            self.keep_box.setChecked(series.get("keep", True))

    def link_is_running(self, ident):
        return any(pending_identity(l) == ident and l["state"] == "analyse" for l in self.data["pending_links"])

    def retry_selected(self):
        entry = self.selected_entry()
        if not entry or entry[0] != "pending":
            return
        for link in self.data["pending_links"]:
            if pending_identity(link) == entry[1] and link["state"] == "erreur":
                link.update(state="en attente", error="")
                self.persist()
                self.render()
                self.next_link()
                return

    def remove_selection(self):
        entry = self.selected_entry()
        if not entry or (self.worker and self.worker.isRunning()):
            return
        kind, ident = entry
        if kind == "pending":
            if self.link_is_running(ident):
                return
            self.data["pending_links"] = [l for l in self.data["pending_links"] if pending_identity(l) != ident]
        else:
            self.data["series"] = [s for s in self.data["series"] if series_identity(s) != ident]
        self.persist()
        self.render()
        self.buttons()

    def apply_to_series(self):
        entry = self.selected_entry()
        if not entry or entry[0] != "series" or (self.worker and self.worker.isRunning()):
            return
        if self.start_ep.value() > self.end_ep.value():
            QMessageBox.warning(self, "Plage invalide", "Vérifiez la plage d’épisodes.")
            return
        series = self.series_for(entry[1])
        requested = (self.start_ep.value(), self.end_ep.value(),
                     self.merge_box.isChecked(), self.keep_box.isChecked())
        previous = (series["start"], series["end"],
                    series.get("merge", True), series.get("keep", True))
        if previous != requested:
            # Keep validated episodes; only the necessary operations must be rescheduled.
            series["state"] = "en attente"
        series.update(start=self.start_ep.value(), end=self.end_ep.value(),
                      merge=series.get("source") not in ("direct", "ytdlp") and self.merge_box.isChecked(),
                      keep=self.keep_box.isChecked())
        self.persist()
        self.render()

    def rename_series(self, item, column):
        if item.parent() or column != 0 or item.data(0, Qt.ItemDataRole.UserRole)[0] != "series":
            return
        title = item.text(0).strip()
        if not title or (self.worker and self.worker.isRunning()):
            self.render()
            return
        self.series_for(item.data(0, Qt.ItemDataRole.UserRole)[1])["title"] = title
        # The pinned folder remains unchanged when the displayed title changes.
        self.persist()

    def update_global(self):
        all_eps = [s.get("episodes", {}).get(str(ep), {}) for s in self.data["series"]
                   for ep in range(s["start"], s["end"] + 1)]
        self.global_bar.setValue(int(sum(e.get("percent", 0) for e in all_eps) / len(all_eps)) if all_eps else 0)

    def buttons(self):
        active = self.worker is not None and self.worker.isRunning()
        paused = active and self.worker.control.pause.is_set()
        # Link analysis is independent: a failed/slow link must not block ready jobs.
        self.start_button.setEnabled(not active)
        self.pause_button.setEnabled(active and not paused)
        self.resume_button.setEnabled(paused)
        self.stop_button.setEnabled(active)
        self.selection_changed()

    def start_downloads(self):
        if self.worker and self.worker.isRunning():
            return
        jobs = [s.copy() for s in self.data["series"] if s["state"] != "terminé"]
        if not jobs:
            self.journal.appendPlainText("Aucune série en attente.")
            return
        if not Path(self.destination.text()).is_dir():
            QMessageBox.warning(self, "Dossier invalide", "Choisissez un dossier existant.")
            return
        self.persist()
        self.worker = Worker(jobs, self.destination.text(), self.parallel.value())
        self.worker.progress.connect(self.on_progress)
        self.worker.state.connect(self.on_state)
        self.worker.log.connect(self.journal.appendPlainText)
        self.worker.ask.connect(self.on_prompt)
        self.worker.finished.connect(self.buttons)
        self.worker.start()
        self.buttons()

    def series_for(self, ident):
        matching = [s for s in self.data["series"] if series_identity(s) == ident]
        # Backward compatibility for callers using an unambiguous raw series ID.
        if not matching and ":" not in ident:
            matching = [s for s in self.data["series"] if s["id"] == ident]
        if len(matching) != 1:
            raise KeyError(f"Identifiant de série absent ou ambigu : {ident}")
        return matching[0]

    def on_progress(self, ident, ep, state, percent, speed, message):
        series = self.series_for(ident)
        series.setdefault("episodes", {})[str(ep)] = {"state": state, "percent": percent, "speed": speed}
        if message and state == "erreur":
            self.journal.appendPlainText(f"Épisode {ep} : {message}")
        self.update_global()
        if not self.progress_timer.isActive():
            self.progress_timer.start()

    def flush_progress(self):
        self.persist()
        self.render()

    def on_state(self, ident, state):
        self.series_for(ident)["state"] = state
        if self.progress_timer.isActive():
            self.progress_timer.stop()
        self.persist()
        self.render()

    def on_prompt(self, text):
        self.worker.approved = QMessageBox.question(self, "Réencodage", text) == QMessageBox.StandardButton.Yes
        self.worker.answer.set()

    def pause_downloads(self):
        self.worker.control.pause.set()
        self.journal.appendPlainText("Pause demandée : les transferts actifs se terminent d’abord.")
        for series in self.data["series"]:
            if series["state"] == "téléchargement":
                series["state"] = "pause"
        self.persist()
        self.render()
        self.buttons()

    def resume_downloads(self):
        self.worker.control.pause.clear()
        for series in self.data["series"]:
            if series["state"] == "pause":
                series["state"] = "téléchargement"
        self.persist()
        self.render()
        self.buttons()

    def stop_downloads(self):
        self.worker.control.cancel()
        for series in self.data["series"]:
            if series["state"] in ("pause", "téléchargement"):
                series["state"] = "en attente"
        self.persist()
        self.render()
        self.journal.appendPlainText("Arrêt demandé ; fichiers partiels conservés.")
        self.buttons()

    def closeEvent(self, event):
        self.closing = True
        self.progress_timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.control.cancel()
            self.worker.wait(15000)
            if self.worker.isRunning():
                self.closing = False
                event.ignore()
                return
        if self.analyzer and self.analyzer.isRunning():
            self.analyzer.wait(15000)
            if self.analyzer.isRunning():
                self.closing = False
                event.ignore()
                return
        if self.pending_analyzer and self.pending_analyzer.isRunning():
            self.pending_analyzer.wait(15000)
            if self.pending_analyzer.isRunning():
                self.closing = False
                event.ignore()
                return
        self.persist()
        self.clipboard_timer.stop()
        QApplication.clipboard().dataChanged.disconnect(self.clipboard_changed)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ICON_FILE)))
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

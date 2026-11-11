"""User-driven website verification, with an optional embedded Qt browser.

Browser checks do not promise to unlock independent urllib/yt-dlp sessions.
"""
import re
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)


class BrowserVerificationDialog(QDialog):
    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url
        self.setWindowTitle("Vérification du site")
        self.resize(780, 560)
        layout = QVBoxLayout(self)
        host = urlsplit(url).hostname or "site inconnu"
        layout.addWidget(QLabel(
            f"Le site {host} demande peut-être une vérification humaine. "
            "Effectuez-la normalement, puis réessayez l'accès."))
        self.embedded = False
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except ImportError:
            layout.addWidget(QLabel(
                "Navigateur intégré indisponible (module optionnel PySide6-Addons). "
                "Utilisez « Ouvrir dans le navigateur »."))
        else:
            try:
                safe_host = re.sub(r"[^a-zA-Z0-9.-]", "_", host)[:120]
                storage = Path.home() / "Library" / "Application Support" / "Drama Downloader" / "browser" / safe_host
                storage.mkdir(parents=True, exist_ok=True)
                self.profile = QWebEngineProfile(f"drama-{safe_host}", self)
                self.profile.setPersistentStoragePath(str(storage))
                self.profile.setCachePath(str(storage / "cache"))
                self.profile.setPersistentCookiesPolicy(
                    QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
                self.browser = QWebEngineView(self)
                self.browser.setPage(QWebEnginePage(self.profile, self.browser))
                self.browser.load(QUrl(url))
                layout.addWidget(self.browser, 1)
                self.embedded = True
            except (RuntimeError, OSError, ValueError):
                layout.addWidget(QLabel(
                    "Impossible d'ouvrir le navigateur intégré. "
                    "Utilisez « Ouvrir dans le navigateur »."))
        note = QLabel(
            "Important : valider la page ne garantit pas que le moteur de téléchargement "
            "pourra réutiliser la session. Le nouveau test vérifiera l'accès réel.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.external_button = QPushButton("Ouvrir dans le navigateur")
        self.external_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.url)))
        self.retry_button = QPushButton("Vérification effectuée — Réessayer")
        self.retry_button.clicked.connect(self.accept)
        self.later_button = QPushButton("Plus tard")
        self.later_button.clicked.connect(self.reject)
        buttons.addWidget(self.external_button)
        buttons.addStretch()
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.retry_button)
        layout.addLayout(buttons)

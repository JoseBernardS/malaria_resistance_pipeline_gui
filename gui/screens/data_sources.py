"""Data sources: locally update Clair3 models and reference sets.

There are no over-the-air updates, so this page lets a lab keep the two things
that drift over time current, on their own machine:

* **Clair3 models** — import a downloaded model folder (the two ``.pt`` weight
  files) so a newer basecaller model can be used without a rebuild.
* **Reference sets** — the offered sets live in a local config file
  (:mod:`gui.ref_catalog`): bundled builtins plus whatever the user last pulled
  in via the manual "Update from cloud" action (never automatic). Each run
  picks its own set in the Add-job dialog, so the set used is recorded per run;
  the default chosen here is only the pre-selection.

Imported models live in the writable user-data dir (:mod:`gui.paths`) so they
survive a read-only packaged ``.app``. Built-in models are shown read-only and
never touched; only user imports can be removed. The reference-set list and
default are persisted in the local catalog file, kept out of any per-job record.

The page follows the app's flat idiom (mirrors ``report_settings.py``): a
``#PageTitle``/``#PageHint`` header over ``#Card`` sections inside a
``QScrollArea``. The Jobs dialog rebuilds its combos on open, so newly imported
items appear the next time a job is configured — no live signal wiring needed.
"""

import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QDialog,
                             QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout,
                             QLabel, QLineEdit, QMessageBox, QPushButton,
                             QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from .. import config_bridge, paths, ref_catalog, refsets, theme
from ..widgets import card


class _ChooseReferenceSetDialog(QDialog):
    """Pick one reference set (by name) from the offered presets."""

    def __init__(self, names, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose reference set")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)

        intro = QLabel("Select the reference set to use as the default for new "
                       "analysis jobs.")
        intro.setObjectName("PageHint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self._group = QButtonGroup(self)
        self._buttons = {}
        for name in names:
            btn = QRadioButton(name)
            btn.setCursor(Qt.PointingHandCursor)
            if name == current:
                btn.setChecked(True)
            self._group.addButton(btn)
            self._buttons[name] = btn
            lay.addWidget(btn)
        # Default the selection to the first entry when nothing matched.
        if names and self._group.checkedButton() is None:
            self._buttons[names[0]].setChecked(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Set as default")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def chosen(self):
        btn = self._group.checkedButton()
        return btn.text() if btn is not None else None


class _ImportModelDialog(QDialog):
    """Collect a Clair3 model as a name plus its two weight files.

    A Clair3 model *is* exactly two files — ``pileup.pt`` and
    ``full_alignment.pt``. The old flow (pick a folder, then a separate name
    prompt) hid that requirement and let users point at the wrong folder. This
    dialog makes the contract explicit: a required Name field and one labelled
    Browse slot per file, with Import disabled until both files and a name are
    present. Picking either file auto-fills the other from the same folder when
    its sibling is there (the common case where both live together), and the
    name defaults to that folder's basename.
    """

    def __init__(self, existing_names=None, parent=None):
        super().__init__(parent)
        self._existing = {n.lower() for n in (existing_names or [])}
        self._name_edited = False
        self.setWindowTitle("Import Clair3 model")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        intro = QLabel(
            "A Clair3 model is two weight files. Select each one below; they "
            "usually sit together in the same downloaded model folder.")
        intro.setObjectName("PageHint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        name_cap = QLabel("Model name")
        name_cap.setObjectName("FieldCaption")
        self._name = QLineEdit()
        self._name.setMinimumHeight(32)
        self._name.setPlaceholderText("e.g. r1041_e82_400bps_sup_v500")
        self._name.textEdited.connect(self._on_name_edited)
        lay.addWidget(name_cap)
        lay.addWidget(self._name)

        self._pileup_path = ""
        self._full_path = ""
        self._pileup_status = self._file_row(
            lay, "pileup.pt", lambda: self._browse("pileup.pt"))
        self._full_status = self._file_row(
            lay, "full_alignment.pt",
            lambda: self._browse("full_alignment.pt"))

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.button(QDialogButtonBox.Ok).setText("Import")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        lay.addWidget(self._buttons)
        self._revalidate()

    def _file_row(self, lay, filename, on_browse):
        cap = QLabel(filename)
        cap.setObjectName("FieldCaption")
        lay.addWidget(cap)
        row = QHBoxLayout()
        row.setSpacing(8)
        status = QLabel("Not selected")
        status.setStyleSheet("font-size:12px; color:%s;" % theme.FAINT)
        browse = QPushButton("Browse\u2026")
        browse.setObjectName("Ghost")
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(on_browse)
        row.addWidget(status, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        return status

    def _browse(self, filename):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select %s" % filename, "",
            "%s (%s);;PyTorch weights (*.pt);;All files (*)"
            % (filename, filename))
        if not path:
            return
        if filename == "pileup.pt":
            self._set_pileup(path)
        else:
            self._set_full(path)
        self._autofill_sibling(path, filename)
        self._maybe_default_name(path)
        self._revalidate()

    def _autofill_sibling(self, path, filename):
        """When one file is picked, grab its sibling from the same folder."""
        folder = os.path.dirname(path)
        if filename == "pileup.pt" and not self._full_path:
            sib = os.path.join(folder, "full_alignment.pt")
            if os.path.isfile(sib):
                self._set_full(sib)
        elif filename == "full_alignment.pt" and not self._pileup_path:
            sib = os.path.join(folder, "pileup.pt")
            if os.path.isfile(sib):
                self._set_pileup(sib)

    def _maybe_default_name(self, path):
        if self._name_edited or self._name.text().strip():
            return
        folder = os.path.basename(os.path.dirname(path.rstrip("/")))
        if folder:
            self._name.setText(folder)

    def _set_pileup(self, path):
        self._pileup_path = path
        self._pileup_status.setText(path)
        self._pileup_status.setStyleSheet(
            "font-size:12px; color:%s;" % theme.HEADING)

    def _set_full(self, path):
        self._full_path = path
        self._full_status.setText(path)
        self._full_status.setStyleSheet(
            "font-size:12px; color:%s;" % theme.HEADING)

    def _on_name_edited(self, _text):
        self._name_edited = True
        self._revalidate()

    def _revalidate(self):
        name = self._name.text().strip()
        ok = bool(name and self._pileup_path and self._full_path
                  and name.lower() not in self._existing)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def values(self):
        return (self._name.text().strip(), self._pileup_path, self._full_path)


class DataSourcesScreen(QWidget):
    """Import Clair3 models and choose the default reference set."""

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        # Optional cloud session: used only for the user-initiated "Update from
        # cloud" action. Absent or offline, the page still works from the local
        # reference-set config file (offline-first, no automatic sync).
        self._session = session
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        header = QVBoxLayout()
        header.setSpacing(2)
        title = QLabel("Data sources")
        title.setObjectName("PageTitle")
        hint = QLabel("Keep variant-calling models and reference data current "
                      "on this machine. Imports are stored in your user data "
                      "folder; built-in items are read-only.")
        hint.setObjectName("PageHint")
        hint.setWordWrap(True)
        header.addWidget(title)
        header.addWidget(hint)
        root.addLayout(header)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }")
        body = QWidget()
        # Scope the transparent background to this widget by object name. A bare
        # ``background:transparent`` cascades onto descendants and clobbers the
        # accent fill of #Primary buttons inside the scroll area, so the "Import
        # model…"/"Add reference set…" buttons render as empty boxes.
        body.setObjectName("DataSourcesBody")
        body.setStyleSheet("#DataSourcesBody { background:transparent; }")
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 6, 4)
        self._body.setSpacing(16)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._build_models_card()
        self._build_refsets_card()
        self._body.addStretch(1)

    # -- Clair3 models ---------------------------------------------------
    def _build_models_card(self):
        frame, lay = card("Clair3 models")
        intro = QLabel(
            "The neural-network model used for variant calling. Each model is "
            "two weight files \u2014 'pileup.pt' and 'full_alignment.pt' \u2014 "
            "which you name and select individually on import.")
        intro.setObjectName("PageHint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self._models_list = QVBoxLayout()
        self._models_list.setSpacing(6)
        lay.addLayout(self._models_list)

        import_btn = self._black_button("Import model\u2026")
        import_btn.clicked.connect(self._on_import_model)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(import_btn)
        lay.addLayout(btn_row)

        self._body.addWidget(frame)
        self._refresh_models()

    def _refresh_models(self):
        self._clear_layout(self._models_list)
        bundled = set()
        base = paths.clair3_models_dir()
        if os.path.isdir(base):
            for name in os.listdir(base):
                if paths._is_valid_model_dir(os.path.join(base, name)):
                    bundled.add(name)
        user = set()
        ubase = paths.user_clair3_models_dir()
        if os.path.isdir(ubase):
            for name in os.listdir(ubase):
                if paths._is_valid_model_dir(os.path.join(ubase, name)):
                    user.add(name)

        names = sorted(bundled | user)
        if not names:
            self._models_list.addWidget(self._empty("No models found."))
            return
        for name in names:
            is_user = name in user
            # A user import shadows a bundled model of the same name.
            tag = "user" if is_user else "built-in"
            self._models_list.addWidget(
                self._item_row(name, tag,
                               removable=is_user,
                               on_remove=lambda _=None, n=name:
                               self._on_remove_model(n)))

    def _on_import_model(self):
        dlg = _ImportModelDialog(paths.list_clair3_models(), self)
        if dlg.exec_() != QDialog.Accepted:
            return
        name, pileup, full = dlg.values()
        try:
            paths.import_clair3_model_files(pileup, full, name)
        except ValueError as e:
            QMessageBox.warning(self, "Could not import model", str(e))
            return
        self._refresh_models()

    def _on_remove_model(self, name):
        if QMessageBox.question(
                self, "Remove model",
                "Remove the imported model '%s'? This deletes its files from "
                "your user data folder." % name,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        paths.delete_clair3_model(name)
        self._refresh_models()

    # -- reference sets --------------------------------------------------
    def _build_refsets_card(self):
        frame, lay = card("Reference sets")
        intro = QLabel(
            "The offered reference sets are kept in a local config file. "
            "\"Update from cloud\" refreshes the published list; \"Download\" "
            "pulls a set's data files onto this machine so runs can use it "
            "offline. Each run picks its own set in the Add-job dialog, so the "
            "set used is recorded per run; the default below is only the "
            "pre-selection.")
        intro.setObjectName("PageHint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self._refsets_list = QVBoxLayout()
        self._refsets_list.setSpacing(6)
        lay.addLayout(self._refsets_list)

        update_btn = QPushButton("Update from cloud")
        update_btn.setObjectName("Ghost")
        update_btn.setCursor(Qt.PointingHandCursor)
        update_btn.clicked.connect(self._on_update_from_cloud)
        default_btn = self._black_button("Set default\u2026")
        default_btn.clicked.connect(self._on_set_default)
        btn_row = QHBoxLayout()
        btn_row.addWidget(update_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(default_btn)
        lay.addLayout(btn_row)

        self._body.addWidget(frame)
        self._refresh_refsets()

    def _available_refset_names(self):
        """Offered names, read from the local config file (offline, no fetch).

        Populated from the bundled builtins plus whatever the last cloud update
        wrote to the catalog (:mod:`gui.ref_catalog`). Refreshing from the cloud
        is a user-initiated action (:meth:`_on_update_from_cloud`), never
        automatic.
        """
        return list(config_bridge.reference_set_names())

    def _refresh_refsets(self):
        self._clear_layout(self._refsets_list)
        default = config_bridge.default_reference_set_name()
        builtin = set(config_bridge.REFERENCE_SETS)
        downloaded = set(refsets.list_names())
        for name in self._available_refset_names():
            is_default = name == default
            if name in builtin:
                # Bundled set: always present, read-only.
                tag = "default" if is_default else "built-in"
                self._refsets_list.addWidget(self._item_row(name, tag))
            elif name in downloaded:
                # Cloud preset whose data files are on this machine.
                tag = "downloaded (default)" if is_default else "downloaded"
                self._refsets_list.addWidget(self._item_row(
                    name, tag, removable=True,
                    on_remove=lambda _=None, n=name: self._on_remove_refset(n)))
            else:
                # Published preset not yet pulled down. Offer Download when we
                # know its backend id (a legacy name-only entry can't be fetched).
                set_id = ref_catalog.id_for(name)
                on_dl = None
                if set_id:
                    on_dl = (lambda _=None, n=name, i=set_id:
                             self._on_download_refset(n, i))
                self._refsets_list.addWidget(
                    self._item_row(name, "cloud", on_download=on_dl))

    def _on_set_default(self):
        names = self._available_refset_names()
        if not names:
            QMessageBox.information(
                self, "No reference sets",
                "No reference sets are available to choose from.")
            return
        current = config_bridge.default_reference_set_name()
        dlg = _ChooseReferenceSetDialog(names, current, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        chosen = dlg.chosen()
        if chosen:
            config_bridge.set_default_reference_set(chosen)
            self._refresh_refsets()

    def _on_update_from_cloud(self):
        """Fetch the service's published reference sets into the local file.

        User-initiated (never automatic). Offline/signed-out/unconfigured it
        reports why and leaves the local catalog untouched; an empty result is
        treated as "nothing published" and does not clear existing entries.
        """
        from ..cloud_client import CloudClient, CloudApiError, is_configured
        if not is_configured():
            QMessageBox.information(
                self, "Cloud not configured",
                "No cloud endpoint is configured, so there is nothing to "
                "update from.")
            return
        if self._session is None or not self._session.is_authenticated():
            QMessageBox.information(
                self, "Not signed in",
                "Sign in to update the reference-set list from the cloud.")
            return
        try:
            items = CloudClient(self._session).list_reference_sets()
        except (CloudApiError, Exception) as e:
            QMessageBox.warning(
                self, "Could not update",
                "Failed to fetch reference sets from the cloud:\n%s" % e)
            return
        entries = [{"name": i.get("name"), "id": i.get("id")}
                   for i in items if i.get("name")]
        if not entries:
            QMessageBox.information(
                self, "Nothing to update",
                "The cloud has not published any reference sets.")
            return
        ref_catalog.set_entries(entries)
        self._refresh_refsets()
        QMessageBox.information(
            self, "Updated",
            "Reference-set list updated (%d from the cloud)." % len(entries))

    def _on_download_refset(self, name, set_id):
        """Pull a published set's bundle down and register it for offline use.

        Fetches a presigned URL, streams ``bundle.tar.gz`` to a temp file, then
        extracts + registers it under the backend name via
        :func:`gui.refsets.save_bundle`. Synchronous with a wait cursor (matches
        the page's other cloud actions); the temp archive is always removed.
        """
        from ..cloud_client import (CloudApiError, CloudClient, download,
                                    is_configured)
        if not is_configured():
            QMessageBox.information(
                self, "Cloud not configured",
                "No cloud endpoint is configured, so there is nothing to "
                "download from.")
            return
        if self._session is None or not self._session.is_authenticated():
            QMessageBox.information(
                self, "Not signed in",
                "Sign in to download reference sets from the cloud.")
            return

        dest = os.path.join(paths.uploads_dir(), "refbundle_%s.tar.gz" % set_id)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            info = CloudClient(self._session).reference_set_bundle_url(set_id)
            url = (info or {}).get("bundle_download_url")
            if not url:
                raise CloudApiError(
                    "the service did not return a download URL.")
            download(url, dest)
            refsets.save_bundle(name, dest)
        except (CloudApiError, Exception) as e:
            QApplication.restoreOverrideCursor()
            self._remove_quietly(dest)
            QMessageBox.warning(
                self, "Download failed",
                "Could not download the reference set '%s':\n%s" % (name, e))
            return
        QApplication.restoreOverrideCursor()
        self._remove_quietly(dest)
        self._refresh_refsets()
        QMessageBox.information(
            self, "Downloaded",
            "Reference set '%s' is now available for analysis on this "
            "machine." % name)

    def _on_remove_refset(self, name):
        """Delete a downloaded set's local files (it can be pulled again later).

        Leaves the published list and any ``default`` pointer untouched: if this
        was the default, new jobs fall back to the bundled reference until it is
        re-downloaded or another default is chosen.
        """
        if QMessageBox.question(
                self, "Remove reference set",
                "Remove the downloaded files for '%s'? They are deleted from "
                "your user data folder; you can download the set again from "
                "the cloud later." % name,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        refsets.delete_set(name)
        self._refresh_refsets()

    @staticmethod
    def _remove_quietly(path):
        try:
            os.remove(path)
        except OSError:
            pass

    # -- shared row helpers ----------------------------------------------
    def _black_button(self, text):
        """A solid black call-to-action button (styled on the button itself so
        it never depends on ancestor cascade)."""
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton { background:#000000; border:1px solid #000000;"
            " border-radius:3px; padding:7px 13px; color:#ffffff;"
            " font-size:13px; font-weight:600; }"
            "QPushButton:hover { background:#1a1a1a; border-color:#1a1a1a; }")
        return btn

    def _item_row(self, name, tag, removable=False, on_remove=None,
                  on_download=None):
        row = QFrame()
        row.setObjectName("Card")
        row.setStyleSheet(
            "QFrame#Card { border:1px solid %s; border-radius:6px; }"
            % theme.BORDER)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            "font-size:13px; font-weight:600; color:%s;" % theme.HEADING)
        tag_lbl = QLabel(tag)
        tag_lbl.setStyleSheet("font-size:11px; color:%s;" % theme.FAINT)
        lay.addWidget(name_lbl)
        lay.addWidget(tag_lbl)
        lay.addStretch(1)
        if on_download is not None:
            btn = QPushButton("Download")
            btn.setObjectName("Ghost")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(on_download)
            lay.addWidget(btn)
        if removable and on_remove is not None:
            btn = QPushButton("Remove")
            btn.setObjectName("Ghost")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(on_remove)
            lay.addWidget(btn)
        return row

    def _empty(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color:%s; font-size:12px;" % theme.FAINT)
        return lbl

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    # -- theme -----------------------------------------------------------
    def restyle(self):
        """Re-apply the palette after a live theme switch by rebuilding the
        colour-baked rows."""
        self._refresh_models()
        self._refresh_refsets()

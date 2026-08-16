"""Sign-in dialog for cloud execution.

Collects credentials and hands them to an ``AuthClient`` (injected, so tests
and the future real client swap in cleanly). On success it records the identity
on the shared ``Session``; while the cloud backend is still a stub the client
raises ``NotImplementedError`` and the dialog surfaces an inline "not enabled
yet" notice instead of closing, so the seam is fully exercised today.
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel,
                             QLineEdit, QVBoxLayout)

from .. import theme
from ..auth import AuthClient, AuthError
from ..widgets import hrule


class LoginDialog(QDialog):
    """Modal credential form that populates a ``Session`` on success."""

    def __init__(self, session, client=None, parent=None):
        super().__init__(parent)
        self._session = session
        self._client = client or AuthClient()
        self.setWindowTitle("Sign in to cloud")
        self.setMinimumWidth(380)

        intro = QLabel(
            "Sign in to run jobs on the cloud pipeline. Local runs never "
            "require an account.")
        intro.setObjectName("DialogHint")
        intro.setWordWrap(True)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@lab.org")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("Password")

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        form.addRow("Email", self.email_edit)
        form.addRow("Password", self.password_edit)

        # Inline status line: hidden until there is something to say (an error
        # or the stub's "coming soon"). Styled with the danger token so a failed
        # attempt reads clearly without a separate message box.
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        self.status.setStyleSheet("color: %s;" % theme.DANGER_TEXT)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok = buttons.button(QDialogButtonBox.Ok)
        self._ok.setText("Sign in")
        self._ok.setObjectName("Primary")
        self._ok.setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self._attempt)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(14)
        lay.addWidget(intro)
        lay.addLayout(form)
        lay.addWidget(self.status)
        lay.addWidget(hrule())
        lay.addWidget(buttons)

    def _set_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _attempt(self):
        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        if not email or not password:
            self._set_status("Enter your email and password.")
            return
        try:
            result = self._client.login(email, password) or {}
        except NotImplementedError:
            # Client is wired, but no server URL is configured. Point the user at
            # the one setting that turns cloud on rather than implying it's unbuilt.
            self._set_status(
                "No cloud server configured \u2014 set PF_CLOUD_API_URL to your "
                "pipeline API (e.g. http://localhost:8000/api/v1) and relaunch.")
            return
        except AuthError as e:
            self._set_status(str(e))
            return
        except Exception as e:  # network/other; surface it, stay open
            self._set_status("Sign-in failed: %s" % e)
            return
        self._session.sign_in(
            email=email,
            token=result.get("token"),
            org=result.get("org"),
            expires_at=result.get("expires_at"),
            refresh_token=result.get("refresh_token"))
        self.accept()

from qt.core import QMessageBox  # type: ignore


class AlertMessage:
    icons = {
        'info': QMessageBox.Information,
        'warning': QMessageBox.Warning,
        'ask': QMessageBox.Question,
        'error': QMessageBox.Critical,
    }

    actions = {
        QMessageBox.Yes: 'yes',
        QMessageBox.No: 'no',
    }

    def __init__(self, parent=None):
        self.parent = parent

    def pop(self, text, level='info'):
        alert = QMessageBox(self.parent)
        alert.setIcon(self.icons.get(level))
        alert.setText(text)
        return alert.exec_()

    def ask(self, text, level='ask'):
        alert = QMessageBox(self.parent)
        alert.setIcon(self.icons.get(level))
        alert.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        alert.setDefaultButton(QMessageBox.No)
        alert.setText(text)
        return self.actions.get(alert.exec_())

    def ask_save(self, text):
        """Save, discard or stay: 'save', 'discard' or 'cancel'. Enter
        saves and Esc stays, so a stray key never throws work away."""
        alert = QMessageBox(self.parent)
        alert.setIcon(QMessageBox.Question)
        alert.setStandardButtons(
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        alert.setDefaultButton(QMessageBox.Save)
        alert.setEscapeButton(QMessageBox.Cancel)
        alert.setText(text)
        return {QMessageBox.Save: 'save', QMessageBox.Discard: 'discard'}.get(
            alert.exec_(), 'cancel')

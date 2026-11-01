"""Настройки подключения без чтения и отображения секрета."""
from datetime import datetime, timezone
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTableWidgetItem
from .access import load_access, source_permission
from .business_dates import MOSCOW
from .ui_tables import read_only_table


class AccessView(QWidget):
    def __init__(self, source_labels, parent=None):
        super().__init__(parent)
        self.source_labels = source_labels
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = read_only_table(['Источник', 'Требуемая категория', 'Заявленные права'])
        self.table.setMinimumHeight(170)
        self.table.setMaximumHeight(240)
        layout.addWidget(self.table)

    def refresh(self, ledger, shop, *, now=None):
        now = now or datetime.now(timezone.utc)
        try:
            claims = load_access(ledger, shop, now=now)
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            claims = None
        if claims is None:
            text = 'Сведения о правах токена ещё не сохранены. Они появятся после сохранения токена или начала синхронизации; вводить ранее сохранённый токен повторно не требуется.'
            self.summary.setStyleSheet('')
        else:
            expiry = datetime.fromtimestamp(claims.expires_at, timezone.utc).astimezone(MOSCOW)
            remaining = (expiry - now).total_seconds()
            warning = 'Срок истёк — замените токен.' if claims.expired else 'Срок истекает в ближайшие 7 дней.' if remaining <= 7 * 86400 else ''
            text = f'Срок действия: {expiry:%d.%m.%Y %H:%M} МСК. {warning}'
            self.summary.setStyleSheet('color: #c0392b' if claims.expired else 'color: #a66c00' if warning else '')
        self.summary.setText(text + '\nЭто заявленные свойства последнего сохранённого или использованного токена, а не проверка подписи, отзыва или доступа на сервере. Состояние загрузки каждого источника показано в «Обзоре». Токен в этой таблице и базе не хранится.')
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.source_labels))
        for row, (source, label) in enumerate(self.source_labels.items()):
            requirement, status = source_permission(claims, source)
            for column, value in enumerate((label, requirement, status)):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.setSortingEnabled(True)

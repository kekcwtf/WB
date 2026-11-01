"""Ограниченные страницы истории; разницы версий не являются новым расходом."""
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidgetItem, QDialog, QPlainTextEdit, QDialogButtonBox)
from .analytics import LABELS
from .storage import operation_payload
from .revision_history import revision_page
from .ui_tables import NumericItem, read_only_table


class RevisionHistoryView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ledger = None
        self.rows = ()
        self.cursors = [None]
        layout = QVBoxLayout(self)
        title = QLabel('История изменений финансового реестра')
        layout.addWidget(title)
        notice = QLabel('Прежние версии сравниваются с текущей записью. Разницы разных версий нельзя складывать: это не новые расходы и не изменение чистой прибыли. Время обнаружения программой не означает время исправления WB; изменения могут возникнуть при обновлении классификатора или внешнего расхода.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        self.table = read_only_table(['Обнаружено, МСК', 'Дата прежней операции', 'Источник', 'ID операции',
            'Прежний SKU', 'Прежний тип', 'Прежняя сумма, ₽', 'Текущая сумма, ₽',
            'Текущая − прежняя, ₽', 'Состояние'])
        self.table.setMinimumHeight(210)
        self.table.setMaximumHeight(400)
        layout.addWidget(self.table)
        bar = QHBoxLayout()
        self.previous = QPushButton('Новее')
        self.next = QPushButton('Старее')
        bar.addWidget(self.previous)
        bar.addWidget(self.next)
        bar.addStretch()
        layout.addLayout(bar)
        self.previous.clicked.connect(self.newer)
        self.next.clicked.connect(self.older)
        self.table.cellDoubleClicked.connect(self.open_details)
        self.clear()

    def clear(self, message='История не загружена. Отсутствие версий не доказывает отсутствие изменений WB.'):
        self.ledger = None
        self.rows = ()
        self.cursors = [None]
        self.table.setRowCount(0)
        self.summary.setText(message)
        self.previous.setEnabled(False)
        self.next.setEnabled(False)

    def set_context(self, ledger, shop, start, end):
        self.clear()
        self.ledger, self.shop, self.start, self.end = ledger, shop, start, end
        self.load_page()

    def load_page(self):
        try:
            page = revision_page(self.ledger, self.shop, self.start, self.end, before_id=self.cursors[-1])
        except (ValueError, TypeError, KeyError):
            self.clear('История не прошла проверку формата. Прежние записи сохранены в базе.')
            return
        self.rows = page.rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(page.rows))
        for index, revision in enumerate(page.rows):
            old, current = revision.previous, revision.current
            state = 'Нет в текущем реестре' if current is None else 'Есть текущая версия'
            if current is not None and (current.occurred, current.kind, current.sku) != (old.occurred, old.kind, old.sku):
                state += '; изменены дата, тип или SKU'
            if revision.observed_later:
                state += '; обнаружено позже даты операции'
            values = [revision.observed_at.strftime('%Y-%m-%d %H:%M:%S') if revision.observed_at else 'Дата неизвестна',
                      old.occurred.isoformat(), old.source, old.identifier, old.sku or 'Без SKU', LABELS[old.kind],
                      old.amount, current.amount if current else None, revision.amount_difference, state]
            for column, value in enumerate(values):
                item = NumericItem(value) if value is None or column in (6, 7, 8) else QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, revision)
                item.setToolTip('Двойной щелчок — точные прежняя и текущая записи. Отбор по дате прежней или текущей операции; сортировка только этой страницы.')
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)
        self.previous.setEnabled(len(self.cursors) > 1)
        self.next.setEnabled(page.has_more)
        self.summary.setText(f'Страница {len(self.cursors)}; сохранённых версий на странице: {len(page.rows)}. '
                             'Отбор по дате прежней или текущей операции выбранного периода. Сортировка — внутри страницы, до 200 версий. '
                             'Нет в текущем реестре не означает автоматического возврата денег.')

    def older(self):
        if self.ledger is not None and self.rows and self.next.isEnabled():
            self.cursors.append(self.rows[-1].identifier)
            self.load_page()

    def newer(self):
        if self.ledger is not None and len(self.cursors) > 1:
            self.cursors.pop()
            self.load_page()

    def make_details(self, row):
        item = self.table.item(row, 0)
        if item is None:
            return None
        revision = item.data(Qt.ItemDataRole.UserRole)
        dialog = QDialog(self)
        dialog.setWindowTitle('Прежняя и текущая версии операции')
        dialog.resize(850, 620)
        layout = QVBoxLayout(dialog)
        for label, operation in [('Сохранённая прежняя версия', revision.previous),
                                 ('Текущая версия на момент загрузки страницы', revision.current)]:
            layout.addWidget(QLabel(label))
            text = json.dumps(operation_payload(operation), ensure_ascii=False, indent=2, default=str) if operation else 'Запись отсутствует в текущем реестре.'
            editor = QPlainTextEdit(text)
            editor.setReadOnly(True)
            layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog

    def open_details(self, row, _column):
        dialog = self.make_details(row)
        if dialog:
            dialog.exec()
            dialog.deleteLater()

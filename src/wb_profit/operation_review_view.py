"""Разбор проблемных проводок без ручного изменения данных Wildberries."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QLineEdit, QDialog, QPlainTextEdit, QDialogButtonBox, QTabWidget)
from .finance import Kind, reconcile_versions
from .analytics import LABELS
from .operations_view import OperationsView
from .operation_review import needs_attention, review_operation


class OperationReviewView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ledger = None
        self.operations = []
        layout = QVBoxLayout(self)
        title = QLabel('Операции, требующие внимания')
        layout.addWidget(title)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        notice = QLabel('Без SKU — допустимые общие операции магазина. Неизвестная запись с суммой 0 ₽ может быть маркером непроверенных сумм. Двойной щелчок — причины и исходная строка; финансовые данные WB здесь не редактируются.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('SKU, источник, ID или тип операции')
        self.filter = QComboBox()
        self.filter.addItems(['Требуют внимания', 'Неизвестные операции', 'Корректировки',
                             'Без SKU · кроме выплат', 'Предварительные', 'Все операции'])
        bar.addWidget(self.search)
        bar.addWidget(self.filter)
        layout.addLayout(bar)
        self.table = OperationsView()
        self.table.setMinimumHeight(240)
        self.table.setMaximumHeight(420)
        layout.addWidget(self.table)
        self.search.textChanged.connect(self.apply_filter)
        self.filter.currentIndexChanged.connect(self.apply_filter)
        self.table.doubleClicked.connect(lambda index: self.open_details(index.row()))
        self.clear()

    def clear(self, message='Нет загруженных операций. Пустой список не подтверждает отсутствие ошибок.'):
        self.operations = []
        self.table.set_operations([])
        self.summary.setText(message)
        self.ledger = None

    def set_context(self, ledger, shop, operations, *, financial_complete=False):
        self.clear()
        selected = list(operations)
        if any(op.store != shop for op in selected):
            raise ValueError('Проверка операций выполняется отдельно по магазину')
        selected = reconcile_versions(selected)
        self.ledger, self.operations = ledger, selected
        counts = [sum(op.kind == Kind.UNKNOWN for op in selected),
                  sum(op.kind == Kind.ADJUSTMENT for op in selected),
                  sum(op.sku is None and op.kind != Kind.PAYOUT for op in selected),
                  sum(not op.confirmed for op in selected)]
        self._summary = (f'Операций: {len(selected)}; неизвестных: {counts[0]}; корректировок: {counts[1]}; '
                         f'без SKU, кроме выплат: {counts[2]}; предварительных: {counts[3]}. '
                         'Категории могут пересекаться. ' +
                         ('Финансовый период сверён.' if financial_complete else 'Финансовый период пока не сверён полностью.'))
        self.apply_filter()

    def apply_filter(self, *_):
        mode = self.filter.currentIndex()
        needle = self.search.text().strip().casefold()
        checks = [needs_attention, lambda op: op.kind == Kind.UNKNOWN,
                  lambda op: op.kind == Kind.ADJUSTMENT,
                  lambda op: op.sku is None and op.kind != Kind.PAYOUT,
                  lambda op: not op.confirmed, lambda op: True]
        selected = [op for op in self.operations if checks[mode](op) and
                    (not needle or needle in ' '.join((op.sku or 'Без SKU', op.source,
                                                       op.identifier, LABELS[op.kind])).casefold())]
        self.table.set_operations(selected)
        if self.ledger is not None:
            self.summary.setText(self._summary + f' Показано: {len(selected)}.')

    def make_details(self, row):
        operation = self.table.model().operation_at(row)
        if operation is None or self.ledger is None:
            return None
        report = review_operation(self.ledger, operation)
        dialog = QDialog(self)
        dialog.setWindowTitle('Проверка финансовой операции')
        dialog.resize(850, 650)
        layout = QVBoxLayout(dialog)
        reasons = QPlainTextEdit('\n\n'.join(report.reasons))
        reasons.setReadOnly(True)
        reasons.setMaximumHeight(180)
        layout.addWidget(reasons)
        tabs = QTabWidget()
        normalized = QPlainTextEdit(report.normalized_json)
        normalized.setReadOnly(True)
        tabs.addTab(normalized, 'Запись реестра')
        raw = QPlainTextEdit(report.raw_json or 'Исходная строка для этой записи недоступна.')
        raw.setReadOnly(True)
        tabs.addTab(raw, 'Исходный ответ WB')
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog

    def open_details(self, row):
        dialog = self.make_details(row)
        if dialog:
            dialog.exec()
            dialog.deleteLater()

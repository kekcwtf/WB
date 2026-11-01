import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPersistentModelIndex, Qt
from PySide6.QtWidgets import QApplication
from wb_profit.finance import Operation, Kind
from wb_profit.operations_view import OperationsModel, OperationsView
from wb_profit.desktop import Window


class OperationsViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def operation(self, identifier, amount, day=date(2026, 1, 1), **kwargs):
        return Operation('shop', kwargs.pop('source', 'wb_finance'), identifier, day,
                         Kind.EXTERNAL if kwargs.get('external') else Kind.COMMISSION,
                         D(amount), kwargs.pop('sku', '42:small'),
                         confirmed=kwargs.pop('confirmed', True))

    def test_sort_uses_exact_decimal_and_keeps_selected_operation(self):
        model = OperationsModel()
        ops = [self.operation('large', '-9007199254740992.01'),
               self.operation('small', '-9007199254740992.02'),
               self.operation('fraction', '-0.00001'), self.operation('zero', '0')]
        model.set_operations(ops)
        selected = QPersistentModelIndex(model.index(1, 2))
        model.sort(5)
        self.assertEqual([model.operation_at(i).identifier for i in range(4)],
                         ['small', 'large', 'fraction', 'zero'])
        self.assertEqual(selected.data(), 'small')
        model.sort(5, Qt.SortOrder.DescendingOrder)
        self.assertEqual(model.operation_at(selected.row()).identifier, 'small')
        self.assertEqual(model.data(model.index(1, 5), Qt.ItemDataRole.ToolTipRole),
                         '-0.00001 ₽ · исходная точная сумма')

    def test_dates_sort_by_calendar_not_formatted_text_and_refresh_keeps_sort(self):
        model = OperationsModel()
        ops = [self.operation('jan', '-10', date(2026, 1, 31)),
               self.operation('feb', '-20', date(2026, 2, 1))]
        model.set_operations(ops)
        model.sort(0, Qt.SortOrder.DescendingOrder)
        self.assertEqual(model.operation_at(0).identifier, 'feb')
        self.assertEqual(model.index(0, 0).data(), '01.02.2026')
        model.set_operations(reversed(ops))
        self.assertEqual(model.operation_at(0).identifier, 'feb')
        model.set_operations([])
        self.assertEqual(model.rowCount(), 0)
        self.assertIsNone(model.operation_at(0))
        self.assertEqual(model.columnCount(model.index(0, 0)), 7)  # невалидный родитель

    def test_large_journal_does_not_format_offscreen_cells(self):
        # Синтетические данные существуют только в тесте. Считаем обращения,
        # а не задаём нестабильный порог времени для разных машин.
        view = OperationsView()
        self.addCleanup(view.close)
        ops = [self.operation(str(i), str(-i), date(2026, 1, 1) + timedelta(days=i % 30))
               for i in range(100000)]
        model = view.model()
        real_data = model.data
        calls = []
        def count(index, role=Qt.ItemDataRole.DisplayRole):
            calls.append(index.row())
            return real_data(index, role)
        with patch.object(model, 'data', count):
            view.set_operations(ops)
            self.assertEqual(calls, [])
            view.resize(900, 400)
            view.show()
            self.app.processEvents()
            self.assertLess(len(calls), 5000)
        self.assertEqual(model.rowCount(), 100000)
        model.sort(5)
        self.assertEqual(model.operation_at(0).amount, D('-99999'))
        self.assertEqual(model.operation_at(99999).amount, D('0'))
        self.assertEqual(len(ops), 100000)
        self.assertEqual(ops[0].identifier, '0')

    def test_double_click_after_sort_edits_exact_current_external_expense(self):
        with tempfile.TemporaryDirectory() as folder:
            window = Window(Path(folder) / 'db.sqlite')
            self.addCleanup(window.close)
            window.store.addItem('Магазин', 'shop')
            external = self.operation('external', '-20', source='external_expenses', external=True)
            normal = self.operation('normal', '-10')
            window.ledger.upsert([external, normal])
            window.operations.set_operations([normal, external])
            window.operations.model().sort(5)
            with patch.object(window, 'external_expense_dialog') as edit:
                window.edit_external_expense(0, 5)
                edit.assert_called_once_with(external)
                edit.reset_mock()
                window.edit_external_expense(1, 5)
                edit.assert_not_called()
            window.close()

from decimal import Decimal
from PySide6.QtWidgets import QTableWidget,QTableWidgetItem,QAbstractItemView,QHeaderView

class NumericItem(QTableWidgetItem):
    def __init__(self,value: Decimal | None):
        super().__init__('—' if value is None else f'{value:,.2f}'.replace(',',' '))
        self.value=value
    def __lt__(self,other):
        if isinstance(other,NumericItem):
            return (self.value is not None,self.value or Decimal('0'))<(other.value is not None,other.value or Decimal('0'))
        return super().__lt__(other)


def read_only_table(columns):
    table=QTableWidget(0,len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSortingEnabled(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.setAlternatingRowColors(True)
    return table

"""Просмотр ручных ставок, не подтверждение налоговых обязательств."""
from datetime import datetime
from decimal import Decimal
from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QTableWidgetItem,QDialogButtonBox
from .tax_changes import changes
from .business_dates import MOSCOW
from .ui_tables import read_only_table,NumericItem


def description(row):
    if row is None:return 'Нет версии; ставка не предполагается'
    return (f"УСН {row['usn']}%; НДС {row['vat']}%; включён в цену: "+('да' if row['included'] else 'нет')+
            '; подтверждены: '+('да' if row['confirmed'] else 'нет'))


class TaxChangesDialog(QDialog):
    def __init__(self,ledger,shop,parent=None):
        super().__init__(parent)
        self.setWindowTitle('Журнал налоговых настроек');self.resize(1100,450)
        layout=QVBoxLayout(self)
        notice=QLabel('До 200 последних изменений. Это история пользовательских настроек расчётной модели, не налоговая декларация. Старые изменения задним числом не восстанавливаются; отсутствие версии не означает нулевой налог.')
        notice.setWordWrap(True);layout.addWidget(notice)
        self.table=read_only_table(['№','Изменено, МСК','Действуют с','Прежние настройки','Новые настройки','Изменение','Отмена'])
        layout.addWidget(self.table)
        events=changes(ledger,shop)
        self.table.setSortingEnabled(False);self.table.setRowCount(len(events))
        for index,(_name,event) in enumerate(events):
            old,new=event['before'],event['after']
            stamp=datetime.fromisoformat(event['changed_at']).astimezone(MOSCOW)
            values=[Decimal(event['sequence']),stamp.strftime('%Y-%m-%d %H:%M:%S'),(new or old)['date'],
                    description(old),description(new),'Удаление' if new is None else 'Назначение' if old is None else 'Исправление',
                    'Отменено' if event['undone_at'] else 'Не отменено']
            for column,value in enumerate(values):
                self.table.setItem(index,column,NumericItem(value) if isinstance(value,Decimal) else QTableWidgetItem(value))
        self.table.setSortingEnabled(True)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject);layout.addWidget(buttons)

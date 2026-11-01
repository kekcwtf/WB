"""Представления сохранённых ответов WB; оперативные данные не являются прибылью."""
import json
from datetime import date
from decimal import Decimal
from .finance import money
from .business_dates import moscow_operation_day


def records(ledger, shop, source):
    return [json.loads(row[0]) for row in ledger.db.execute(
        'SELECT payload FROM source_records WHERE store=? AND source=?',(shop,source))]


def stock_rows(ledger,shop,today: date):
    from .inventory import inventory_report
    report=inventory_report(ledger,shop,today)
    return [(r.nm_id,r.vendor,r.size,r.barcode,r.stock,r.sales,r.daily_sales,r.estimated_days) for r in report.rows]


def advertising_rows(ledger,shop,start,end):
    result=[]
    for row in records(ledger,shop,'advertising'):
        stamp=row.get('updTime')
        day=moscow_operation_day(stamp)
        if day is not None and not start <= day <= end:
            continue
        result.append((day.isoformat() if day else 'Дата/часовой пояс не указаны',str(row['advertId']),row.get('campName',''),
                       money(row['updSum']),row['paymentType']))
    return result


def product_metadata(ledger,shop):
    return {str(card['nmID']):card for card in records(ledger,shop,'products')}

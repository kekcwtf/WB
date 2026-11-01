"""Оперативный запас и фактические проводки хранения: разные периоды и достоверность."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from .analytics import stock_estimate
from .business_dates import MOSCOW,statistics_day
from .finance import Kind, ZERO, money, reconcile_versions
from .data_views import records, product_metadata
from .coverage import financial_period_reasons


def source_state(ledger, shop, source):
    row = ledger.db.execute('SELECT completed,last_success,status FROM sync_state WHERE store=? AND source=?', (shop, source)).fetchone()
    return (bool(row[0]), row[1], row[2]) if row else (False, None, 'not_started')


def fresh(stamp, now):
    try:
        parsed = datetime.fromisoformat(stamp)
        return parsed.tzinfo is not None and -timedelta(minutes=5) <= now - parsed <= timedelta(hours=24)
    except (ValueError, TypeError):
        return False


def stock_key(row):
    nm, barcode = row.get('nmId'), row.get('barcode')
    if (isinstance(nm, bool) or not isinstance(nm, (str, int)) or not str(nm).isdigit() or int(nm) <= 0 or
            isinstance(barcode, bool) or not isinstance(barcode, (str, int)) or not str(barcode).strip()):
        return None
    return str(nm), str(barcode)


@dataclass(frozen=True)
class InventoryRow:
    nm_id: str
    vendor: str
    size: str
    barcode: str
    stock: int | None
    sales: int | None
    daily_sales: Decimal | None
    estimated_days: Decimal | None
    title: str
    category: str
    storage_loaded: Decimal
    storage_ratio: Decimal | None
    no_sales: bool | None
    potential_illiquid: bool | None
    high_storage: bool | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class InventoryReport:
    rows: tuple[InventoryRow, ...]
    sales_start: date
    sales_end: date
    stocks_loaded: str | None
    sales_loaded: str | None
    sales_complete: bool
    storage_complete: bool
    storage_loaded: Decimal
    storage_outside_rows: Decimal
    reasons: tuple[str, ...]


def inventory_report(ledger, shop, today, start=None, end=None, *, now=None,
                     max_stock_days=90, storage_warning_percent=Decimal('10')):
    now = now or datetime.now(MOSCOW)
    if now.tzinfo is None:
        raise ValueError('Нужен часовой пояс времени обновления')
    if type(max_stock_days) is not int or max_stock_days <= 0:
        raise ValueError('Порог запаса должен быть положительным числом дней')
    threshold = money(storage_warning_percent)
    if not ZERO < threshold <= 100:
        raise ValueError('Порог расходов хранения должен быть от 1 до 100 процентов')
    sales_start = today - timedelta(days=29)
    start, end = start or sales_start, end or today
    if start > end:
        raise ValueError('Неверный период расходов хранения')
    stocks_state = source_state(ledger, shop, 'stocks')
    sales_state = source_state(ledger, shop, 'sales')
    reasons = []
    ranges = ledger.setting(shop, 'source_coverage:sales', [])
    sales_complete = any(left <= sales_start.isoformat() and right >= today.isoformat() for left, right in ranges)
    if not sales_complete:
        reasons.append('Не полностью загружена история оперативных продаж за 30 дней')
    if not sales_state[0] or sales_state[2] != 'success' or not fresh(sales_state[1], now):
        sales_complete = False
        reasons.append('Продажи обновляются, недоступны или загружены более 24 часов назад')
    stock_fresh = fresh(stocks_state[1], now)
    if not stock_fresh:
        reasons.append('Остатки не загружены либо снимок старше 24 часов: сигналы запаса отключены')
    elif stocks_state[2] == 'running':
        reasons.append('Остатки обновляются; показан последний завершённый снимок')
    counts = defaultdict(int)
    malformed = 0
    for row in records(ledger, shop, 'sales'):
        if row.get('warehouseType') == 'Склад продавца':
            continue  # FBS не подмешивается в FBW
        day = statistics_day(row.get('date'))
        if day is None:
            malformed += 1
            continue
        if not sales_start <= day <= today:
            continue
        key = stock_key(row)
        sale_id = row.get('saleID')
        if row.get('warehouseType') != 'Склад WB' or key is None or not isinstance(sale_id, str) or not sale_id.startswith(('S', 'R')):
            malformed += 1
            continue
        if sale_id.startswith('S'):
            counts[key] += 1  # официальная схема: одна строка = одна единица
    if malformed:
        sales_complete = False
        reasons.append(f'Оперативные записи с неизвестной датой, SKU или типом: {malformed}')
    operations = reconcile_versions(ledger.operations(shop, start, end))
    storage_reasons = list(financial_period_reasons(ledger, shop, start, end))
    if any(not op.confirmed or op.kind == Kind.UNKNOWN for op in operations):
        storage_reasons.append('Есть предварительные или неизвестные финансовые операции')
    storage_complete = not storage_reasons
    if not storage_complete:
        reasons.append('Хранение: показаны только загруженные проводки выбранного периода; полнота расходов не подтверждена')
    storage = defaultdict(lambda: ZERO)
    revenue = defaultdict(lambda: ZERO)
    for op in operations:
        if not op.confirmed:continue
        if op.kind == Kind.STORAGE:
            storage[op.sku] -= op.amount
        elif op.kind in {Kind.SALE, Kind.RETURN}:
            revenue[op.sku] += op.amount
    raw_stocks = records(ledger, shop, 'stocks')
    duplicates = Counter(stock_key(row) for row in raw_stocks)
    metadata = product_metadata(ledger, shop)
    result, linked_storage = [], ZERO
    for row in raw_stocks:
        key = stock_key(row)
        warnings = []
        warehouses = row.get('warehouses', [])
        totals = [w.get('quantity') for w in warehouses if isinstance(w, dict) and w.get('warehouseName') == 'Всего находится на складах']
        stock = totals[0] if len(totals) == 1 and type(totals[0]) is int and totals[0] >= 0 else None
        if stock is None:
            warnings.append('Нет однозначного общего складского остатка; склады и товары в пути не суммируются')
        if key is None or duplicates[key] != 1:
            warnings.append('SKU отсутствует или повторяется в снимке; строки не суммируются')
            stock = None
        count = counts[key] if key is not None and sales_complete else None
        estimate = stock_estimate(stock, count, 30) if stock is not None and count is not None and stock_fresh else None
        no_sales = stock > 0 and count == 0 if estimate else None
        potential = stock > 0 and (count == 0 or estimate.estimated_days > max_stock_days) if estimate else None
        sku = ':'.join(key) if key is not None and duplicates[key] == 1 else None
        loaded = storage[sku] if sku else ZERO
        if sku:
            linked_storage += loaded
        ratio = loaded / revenue[sku] * 100 if sku and storage_complete and revenue[sku] > ZERO else None
        high = (loaded > ZERO and (revenue[sku] <= ZERO or ratio >= threshold)) if sku and storage_complete else None
        card = metadata.get(key[0], {}) if key else {}
        result.append(InventoryRow(key[0] if key else '—', str(row.get('vendorCode') or card.get('vendorCode') or ''),
                                   str(row.get('techSize') or ''), key[1] if key else '—', stock, count,
                                   estimate.average_daily_sales if estimate else None,
                                   estimate.estimated_days if estimate else None, str(card.get('title') or ''),
                                   str(row.get('subjectName') or card.get('subjectName') or ''), loaded, ratio,
                                   no_sales, potential, high, tuple(warnings)))
    total_storage = sum(storage.values(), ZERO)
    return InventoryReport(tuple(result), sales_start, today, stocks_state[1], sales_state[1], sales_complete,
                           storage_complete, total_storage, total_storage - linked_storage, tuple(reasons))

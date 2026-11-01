"""Покрытие дат загрузки не подменяет финансовую сверку или полноту кампаний."""
from dataclasses import dataclass
from datetime import date, timedelta

PERIOD_SOURCES = {'finance_weekly', 'report_totals', 'orders', 'sales',
                  'advertising', 'ad_payments', 'ad_statistics'}


@dataclass(frozen=True)
class SourceCoverage:
    label: str
    note: str
    gaps: tuple[tuple[date, date], ...] = ()


def missing_ranges(ranges, start, end):
    if start > end:
        raise ValueError('Неверный выбранный период')
    if not isinstance(ranges, list):
        raise ValueError('Неверный формат покрытия дат')
    intervals = []
    for item in ranges:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError('Неверный интервал покрытия')
        left, right = (date.fromisoformat(value) for value in item)
        if left > right:
            raise ValueError('Неверные границы покрытия')
        if left <= end and right >= start:
            intervals.append((max(start, left), min(end, right)))
    cursor, gaps = start, []
    for left, right in sorted(intervals):
        if left > cursor:
            gaps.append((cursor, left - timedelta(days=1)))
        if right >= end:
            return tuple(gaps)
        cursor = max(cursor, right + timedelta(days=1))
    if cursor <= end:
        gaps.append((cursor, end))
    return tuple(gaps)


def source_coverage(ledger, shop, source, start, end):
    if start > end:
        return SourceCoverage('Неверный период', 'Дата начала позже даты окончания.')
    if source not in PERIOD_SOURCES:
        return SourceCoverage('Снимок, не история периода', 'Источник сохраняет текущий снимок. Он не подтверждает исторические значения за произвольный период; дату и статус снимка смотрите в соседних столбцах.')
    try:
        gaps = missing_ranges(ledger.setting(shop, 'source_coverage:'+source, []), start, end)
    except (ValueError, TypeError):
        return SourceCoverage('Покрытие не определено', 'Сохранённые границы не прошли проверку. Полнота данных не предполагается.')
    note = ('Это только даты успешно законченных выгрузок, а не подтверждение закрытия отчёта, '
            'денежных сумм или отсутствия неизвестных операций. Проверки финансовой сверки и рекламы выполняются отдельно.')
    if source == 'ad_statistics':
        note += ' Покрытие всех нужных кампаний проверяется отдельно; одних дат недостаточно.'
    state = ledger.db.execute('SELECT completed,status FROM sync_state WHERE store=? AND source=?', (shop, source)).fetchone()
    if state and (not state[0] or state[1] in {'running', 'failed', 'cancelled'}):
        note += ' Повторная загрузка не завершена; часть прежнего кеша могла сохраниться.'
    if gaps:
        stamps = [f'{left:%d.%m.%Y}–{right:%d.%m.%Y}' for left, right in gaps]
        note += '\nНет завершённого покрытия дат:\n' + '\n'.join(stamps)
        if source in {'orders', 'sales'}:
            note += '\nAPI оперативной статистики ограничивает доступную глубину истории; старые даты могут быть недоступны.'
        return SourceCoverage(f'Пробелов в покрытии дат: {len(gaps)}', note, gaps)
    return SourceCoverage('Есть покрытие дат · не сверка', note)

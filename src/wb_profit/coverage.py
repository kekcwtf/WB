"""Полнота выбранного периода; нет ручного переключателя «считать всё полным»."""
from datetime import date, timedelta
from .data_views import records
from .finance import money
from .business_dates import moscow_operation_day


def merge_ranges(ranges,start,end):
    ordered=sorted([*ranges,[start.isoformat(),end.isoformat()]])
    merged=[]
    for left,right in ordered:
        if merged and date.fromisoformat(left)<=date.fromisoformat(merged[-1][1])+timedelta(days=1):
            merged[-1][1]=max(merged[-1][1],right)
        else:merged.append([left,right])
    return merged


def financial_period_reasons(ledger,shop,start,end):
    reasons=[]
    for source,label in [('finance_weekly','финансовой детализации'),('report_totals','контрольных сумм')]:
        state=ledger.db.execute('SELECT completed FROM sync_state WHERE store=? AND source=?',(shop,source)).fetchone()
        if state and not state[0]:reasons.append('Обновление '+label+' ещё не завершено')
        ranges=ledger.setting(shop,'source_coverage:'+source,[])
        if not any(left<=start.isoformat() and right>=end.isoformat() for left,right in ranges):
            reasons.append('Не полностью загружен период '+label)
    totals=records(ledger,shop,'report_totals')
    closed_dates=[]
    for report in totals:
        try:
            value=report.get('dateTo')
            if not isinstance(value,str):raise ValueError()
            closed_dates.append(date.fromisoformat(value[:10]).isoformat())
        except ValueError:
            reasons.append('Не подтверждена дата закрытия одного из финансовых отчётов')
    closed=max(closed_dates,default='')
    if closed<end.isoformat():reasons.append('Период ещё не закрыт финансовым отчётом WB')
    related={str(row[0]) for row in ledger.db.execute("SELECT DISTINCT json_extract(payload,'$.reportId') FROM source_records WHERE store=? AND source='finance_weekly' AND substr(json_extract(payload,'$.rrDate'),1,10) BETWEEN ? AND ?",(shop,start.isoformat(),end.isoformat()))}
    audits={r['report_id']:r for r in ledger.setting(shop,'report_audits',[])}
    if not related or any(not audits.get(report,{}).get('verified',False) for report in related):
        reasons.append('Независимая сверка финансовых отчётов не завершена')
    return tuple(reasons)


def period_coverage(ledger,shop,start,end):
    reasons=list(financial_period_reasons(ledger,shop,start,end))
    identity=ledger.setting(shop,'verified_api_identity')
    if identity and identity.get('country')!='ru':
        reasons.append('Налоговая модель для страны продавца не подтверждена')
    ranges=ledger.setting(shop,'source_coverage:advertising',[])
    if not any(left<=start.isoformat() and right>=end.isoformat() for left,right in ranges):
        reasons.append('Не полностью загружен период рекламных затрат')
    ad_state=ledger.db.execute("SELECT completed FROM sync_state WHERE store=? AND source='advertising'",(shop,)).fetchone()
    if ad_state and not ad_state[0]:reasons.append('Обновление рекламных затрат ещё не завершено')
    for row in records(ledger,shop,'advertising'):
        if not money(row['updSum']):continue
        stamp=moscow_operation_day(row.get('updTime'))
        if stamp is None:
            reasons.append('Есть рекламные затраты без даты: их период не установлен')
            break
        if start<=stamp<=end:
            reasons.append('Рекламные затраты требуют сопоставления с удержаниями: исключено повторное списание')
            break
    for entry in records(ledger,shop,'ad_statistics'):
        value=entry.get('data')
        if value and any(start.isoformat()<=day['date'][:10]<=end.isoformat() and money(day['sum']) for day in value['days']):
            reasons.append('Потребление рекламы отражено в статистике; связь с финансовыми удержаниями не подтверждена')
            break
    return not reasons,tuple(reasons)

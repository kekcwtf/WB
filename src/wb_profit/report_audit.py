"""Сверка детализации с независимо запрошенным списком отчётов WB."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from .finance import ZERO, money
from .normalization import financial_row

@dataclass(frozen=True)
class ReportAudit:
    report_id: str
    control_payable: Decimal | None
    ledger_payable: Decimal
    difference: Decimal | None
    issues: tuple[str,...]
    verified: bool


def audit_reports(shop, details, totals):
    grouped=defaultdict(list)
    for row in details:grouped[str(row['reportId'])].append(row)
    controls={str(r['reportId']):r for r in totals}
    result=[]
    for report in sorted(grouped.keys()|controls.keys()):
        issues=[]
        amount=ZERO
        for row in grouped[report]:
            normalized=financial_row(shop,row)
            amount+=sum((op.amount for op in normalized.operations),ZERO)
            issues.extend(normalized.issues)
        control=controls.get(report)
        payable=None
        if not control or control.get('bankPaymentSum') is None:
            issues.append('Нет независимой контрольной суммы Итого к оплате')
        elif control.get('currency')!='RUB':
            issues.append('Валюта контрольного отчёта не RUB')
        else:
            payable=money(control['bankPaymentSum'])
        if not grouped[report]:issues.append('Нет детализации отчёта')
        difference=amount-payable if payable is not None else None
        if difference:issues.append('Расхождение с контрольной суммой WB')
        result.append(ReportAudit(report,payable,amount,difference,tuple(sorted(set(issues))),not issues and difference==ZERO))
    return result

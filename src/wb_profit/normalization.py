"""Нормализация современной finance/v1 детализации. Исходные строки сохраняются отдельно.

Сумма продажи и встроенных удержаний строго равна forPay. Необъяснённый
остаток выделяется как корректировка, а не вторая комиссия. Рекламные
отчёты не добавляются сюда: связь с удержаниями WB пока не доказана.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from .finance import Operation, Kind, ZERO, money
from .api.finance_fields import FINANCE_FIELDS


@dataclass(frozen=True)
class Normalized:
    operations: tuple[Operation, ...]
    issues: tuple[str, ...]


def financial_row(shop: str, row: dict) -> Normalized:
    base = f"{row['reportId']}:{row['rrdId']}"
    occurred = date.fromisoformat(str(row['rrDate'])[:10])
    nm = row.get('nmId')
    sku = f"{nm}:{row.get('sku') or row.get('techSize') or '-'}" if nm else None
    ops, issues = [], []
    if set(row)-FINANCE_FIELDS:
        issues.append('В ответе появились поля, отсутствующие в проверенной схеме: '+', '.join(sorted(set(row)-FINANCE_FIELDS)))
    def emit(kind, suffix, amount, units=0):
        ops.append(Operation(shop,'wb_finance',base+':'+suffix,occurred,kind,amount,sku,units))
    def field(name):
        if name not in row or row[name] is None or row[name] == '':
            issues.append('Нет обязательного денежного поля ' + name)
            return None
        try:
            return money(row[name])
        except (ValueError, TypeError, ArithmeticError):
            issues.append('Неверный формат денежного поля ' + name)
            return None
    if row.get('currency') != 'RUB':
        raise ValueError('Финансовый отчёт не в RUB; смешивание валют запрещено')
    reason = row.get('sellerOperName', '')
    doc = row.get('docTypeName', '')
    if reason in {'Продажа', 'Возврат'}:
        if doc not in {'Продажа','Возврат'} or doc != reason:
            issues.append('Неоднозначный тип документа и обоснование оплаты')
        else:
            sign = Decimal('1') if doc == 'Продажа' else Decimal('-1')
            gross, payable = field('retailAmount'), field('forPay')
            vw, vat, acquiring = field('vw'), field('vwNds'), field('acquiringFee')
            quantity = row.get('quantity')
            if isinstance(quantity, bool) or not isinstance(quantity,int) or quantity < 0:
                issues.append('Неизвестное количество')
                quantity = 0
            if gross is not None:
                if gross < ZERO:
                    issues.append('Отрицательная сумма реализации требует отдельной классификации')
                else:
                    emit(Kind.SALE if sign > 0 else Kind.RETURN, 'revenue', sign*gross, quantity if sign > 0 else -quantity)
                    embedded = ZERO
                    if vw is not None and vat is not None:
                        commission = sign*(vw+vat)
                        emit(Kind.COMMISSION, 'commission', -commission)
                        embedded += commission
                    if acquiring is not None:
                        emit(Kind.ACQUIRING, 'acquiring', -sign*acquiring)
                        embedded += sign*acquiring
                    if payable is not None:
                        residual = sign*payable - (sign*gross - embedded)
                        if residual:
                            emit(Kind.ADJUSTMENT, 'settlement_residual', residual)
                            issues.append('Необъяснённая разница между реализацией, удержаниями и forPay')
    elif reason not in {'Логистика','Хранение','Штраф','Удержание','Платная приемка','Платная приёмка','Корректировка логистики'}:
        issues.append('Неизвестное обоснование оплаты: ' + str(reason))
    logistics = field('deliveryService')
    if logistics:
        reverse = row.get('returnAmount',0) > 0 and row.get('deliveryAmount',0) == 0
        emit(Kind.REVERSE_LOGISTICS if reverse else Kind.LOGISTICS, 'logistics', -logistics)
        if row.get('returnAmount',0) and row.get('deliveryAmount',0):
            issues.append('Прямая и обратная логистика в одной сумме; разделение недоступно')
    for field_name,kind in [('paidStorage',Kind.STORAGE),('paidAcceptance',Kind.ACCEPTANCE),
                            ('penalty',Kind.PENALTY),('deduction',Kind.DEDUCTION)]:
        amount = field(field_name)
        if amount:
            emit(kind,field_name,-amount)
    # У этих полей есть особые условия начисления. До проверки правил знаков
    # и включения в forPay они остаются в исходной строке и блокируют чистую прибыль.
    for name in ['additionalPayment','rebillLogisticCost','installmentCofinancingAmount',
                 'cashbackAmount','cashbackDiscount','cashbackCommissionChange']:
        value = field(name)
        if value:
            issues.append('Требует классификации ' + name)
    method = row.get('deliveryMethod','')
    if reason in {'Продажа','Возврат'} and not method:
        issues.append('Схема продажи не установлена как FBW')
    if method and not str(method).startswith(('FBW','FBO')):
        issues.append('Схема продажи не подтверждена как FBW: ' + str(method))
    if issues:
        emit(Kind.UNKNOWN, 'unclassified', ZERO)
    return Normalized(tuple(ops),tuple(issues))

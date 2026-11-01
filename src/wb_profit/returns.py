"""Связь финансового возврата с единственной исходной продажей по srid и SKU.

Поиск выполняется по всей сохранённой истории магазина, а не только текущему
экранному периоду. Не выбираем «ближайшую» продажу при неоднозначности.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ReturnLink:
    sale_identifier: str
    sale_date: date


@dataclass(frozen=True)
class ReturnLinks:
    linked: dict[str, ReturnLink]
    issues: dict[str, str]


def resolve_returns(rows: list[dict]) -> ReturnLinks:
    sales, returns = defaultdict(list), defaultdict(list)
    issues = {}
    seen = set()
    for row in rows:
        identifier=f"{row['reportId']}:{row['rrdId']}"
        if identifier in seen:
            raise ValueError('Повтор финансовой строки при сопоставлении возвратов')
        seen.add(identifier)
        reason=row.get('sellerOperName')
        if reason not in {'Продажа','Возврат'}:
            continue
        if row.get('docTypeName') != reason:
            if reason=='Возврат':issues[identifier]='Тип документа возврата неоднозначен'
            continue
        if not str(row.get('deliveryMethod','')).startswith(('FBW','FBO')):
            if reason=='Возврат':issues[identifier]='Схема продажи возврата не подтверждена как FBW'
            continue
        srid=row.get('srid')
        barcode=row.get('sku')
        nm=row.get('nmId')
        quantity=row.get('quantity')
        if (not isinstance(srid,str) or not srid.strip() or not isinstance(barcode,str) or not barcode.strip()
                or isinstance(nm,bool) or not isinstance(nm,int) or nm<=0
                or isinstance(quantity,bool) or not isinstance(quantity,int) or quantity<=0
                or row.get('currency')!='RUB'):
            if reason=='Возврат':issues[identifier]='Не хватает srid, баркода, артикула, количества или валюты для связи возврата'
            continue
        key=(srid,str(nm),str(barcode))
        target=sales if reason=='Продажа' else returns
        target[key].append((identifier,row))
    linked={}
    for key, items in returns.items():
        candidates=sales.get(key,[])
        if len(candidates)!=1:
            explanation='Исходная продажа не найдена' if not candidates else 'Найдено несколько исходных продаж'
            for identifier,_ in items:issues[identifier]=explanation
            continue
        sale_id,sale=candidates[0]
        try:
            sale_date=date.fromisoformat(str(sale['rrDate'])[:10])
            return_dates=[date.fromisoformat(str(row['rrDate'])[:10]) for _,row in items]
        except (ValueError,KeyError):
            for identifier,_ in items:issues[identifier]='Неверная дата продажи или возврата'
            continue
        if sum(row['quantity'] for _,row in items)>sale['quantity']:
            for identifier,_ in items:issues[identifier]='Общее количество возвратов превышает исходную продажу'
            continue
        for (identifier,_),returned in zip(items,return_dates):
            if returned<sale_date:
                issues[identifier]='Возврат датирован раньше исходной продажи'
            else:
                linked[identifier]=ReturnLink(sale_id+':revenue',sale_date)
    return ReturnLinks(linked,issues)

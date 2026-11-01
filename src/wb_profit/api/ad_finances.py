"""Счёт продвижения и его пополнения — отдельные источники, не расходы прибыли."""
import calendar
import re
from datetime import date,timedelta
from ..sync import Page,Record
from ..finance import money
from .transport import ApiError


class AdAccount:
    name='ad_account'
    category='promotion'
    def __init__(self,client):self.client=client
    async def pages(self,cursor):
        value=await self.client.request('GET','/adv/v1/balance')
        if not isinstance(value,dict) or not isinstance(value.get('currency'),str) or not re.fullmatch('[A-Z]{3}',value['currency']):
            raise ApiError('В ответе рекламного счёта нет валюты')
        try:
            for field in ('balance','net','bonus'):money(value[field])
        except (KeyError,TypeError,ValueError,ArithmeticError):
            raise ApiError('Изменилась схема рекламного счёта') from None
        yield Page((Record('account',value),),None,True,snapshot=True)


class AdPayments:
    name='ad_payments'
    category='promotion'
    def __init__(self,client,start,end):self.client,self.start,self.end=client,start,end
    async def pages(self,cursor):
        current=date.fromisoformat(cursor) if cursor else self.start.replace(day=1)
        while current<=self.end:
            end=min(self.end,current.replace(day=calendar.monthrange(current.year,current.month)[1]))
            rows=await self.client.request('GET','/adv/v1/payments',params={'from':current.isoformat(),'to':end.isoformat()})
            if not isinstance(rows,list):raise ApiError('Изменилась схема пополнений продвижения')
            records=[];seen=set();scope=current.strftime('%Y-%m')
            for row in rows:
                if (not isinstance(row,dict) or isinstance(row.get('id'),bool) or not isinstance(row.get('id'),int)
                        or row['id'] in seen or any(key not in row for key in ('date','sum','type','statusId','currency'))):
                    raise ApiError('Неверный или повторяющийся ID пополнения продвижения')
                if isinstance(row['statusId'],bool) or row['statusId'] not in (0,1) or isinstance(row['type'],bool) or row['type'] not in (0,1,3):
                    raise ApiError('Неизвестный статус или источник пополнения')
                try:money(row['sum'])
                except (ValueError,TypeError,ArithmeticError):raise ApiError('Неверная сумма пополнения') from None
                if not isinstance(row['currency'],str):raise ApiError('Неизвестна валюта пополнения')
                seen.add(row['id']);records.append(Record(f"{scope}:{row['id']}",row))
            current=end+timedelta(days=1)
            yield Page(tuple(records),current.isoformat(),current>self.end,snapshot_scope=scope)


class AdCampaigns:
    name='ad_campaigns'
    category='promotion'
    def __init__(self,client):self.client=client
    async def pages(self,cursor):
        response=await self.client.request('GET','/adv/v1/promotion/count')
        if not isinstance(response,dict) or isinstance(response.get('all'),bool) or not isinstance(response.get('all'),int) or response['all']<0:
            raise ApiError('Изменилась схема списка рекламных кампаний')
        groups=response.get('adverts')
        if groups is None and response['all']==0:groups=[]
        if not isinstance(groups,list):raise ApiError('Нет списка рекламных кампаний')
        seen=set()
        for group in groups:
            if not isinstance(group,dict) or not isinstance(group.get('status'),int) or not isinstance(group.get('advert_list'),list):
                raise ApiError('Неверная группа рекламных кампаний')
            for campaign in group['advert_list']:
                identifier=campaign.get('advertId') if isinstance(campaign,dict) else None
                if isinstance(identifier,bool) or not isinstance(identifier,int) or identifier<=0 or identifier in seen:
                    raise ApiError('Неоднозначный ID рекламной кампании')
                seen.add(identifier)
        if len(seen)!=response['all']:raise ApiError('Количество кампаний не совпадает со списком')
        yield Page((Record('campaigns',response),),None,True,snapshot=True)


def eligible_campaigns(response):
    return sorted(campaign['advertId'] for group in (response.get('adverts') or [])
                  if group['status'] in {7,9,11} for campaign in group['advert_list'])

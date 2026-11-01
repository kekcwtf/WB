"""Статистика рекламных кампаний: 50 ID и не более календарного месяца в запросе."""
import calendar
import re
import json
from datetime import date,timedelta
from ..sync import Page,Record
from ..finance import money
from .transport import ApiError


def validate_statistics(value,start,end):
    if not isinstance(value,dict) or not isinstance(value.get('currency'),str) or not re.fullmatch('[A-Z]{3}',value['currency']) or not isinstance(value.get('days'),list):
        raise ApiError('Неизвестная валюта или формат рекламной статистики')
    try:
        money(value['sum']);money(value['sum_price'])
        seen=set()
        for day in value['days']:
            when=date.fromisoformat(day['date'][:10])
            if when in seen or not start<=when<=end:raise ValueError()
            seen.add(when)
            money(day['sum']);money(day['sum_price'])
            if not isinstance(day['apps'],list):raise ValueError()
            for field in ('views','clicks','orders'):
                if isinstance(day[field],bool) or not isinstance(day[field],int) or day[field]<0:raise ValueError()
            for app in day['apps']:
                money(app['sum'])
                if not isinstance(app['nms'],list):raise ValueError()
                for item in app['nms']:
                    if isinstance(item['nmId'],bool) or not isinstance(item['nmId'],int) or item['nmId']<=0:raise ValueError()
                    money(item['sum'])
                    if money(item['sum'])<0:raise ValueError()
            if money(day['sum'])<0:raise ValueError()
    except (KeyError,ValueError,TypeError,ArithmeticError,AttributeError):
        raise ApiError('Изменилась схема или даты рекламной статистики') from None


class AdvertisingStatistics:
    name='ad_statistics'
    category='promotion'
    def __init__(self,client,ids,start,end):
        if start>end or any(isinstance(v,bool) or not isinstance(v,int) or v<=0 for v in ids):
            raise ValueError('Неверные ID или период рекламной статистики')
        self.client,self.ids,self.start,self.end=client,sorted(set(ids)),start.replace(day=1),end
    async def pages(self,cursor):
        state=json.loads(cursor) if cursor else {'month':self.start.isoformat(),'end':self.end.isoformat(),'batch':0,'ids':self.ids,'start':self.start.isoformat()}
        if state['ids']!=self.ids:
            state={'month':self.start.isoformat(),'end':self.end.isoformat(),'batch':0,'ids':self.ids,'start':self.start.isoformat()}
        self.start,self.end=date.fromisoformat(state['start']),date.fromisoformat(state['end'])
        if not state['ids']:
            yield Page((),json.dumps(state,sort_keys=True),True);return
        while date.fromisoformat(state['month'])<=self.end:
            current=date.fromisoformat(state['month'])
            end=min(self.end,current.replace(day=calendar.monthrange(current.year,current.month)[1]))
            ids=state['ids'][state['batch']:state['batch']+50]
            response=await self.client.request('GET','/adv/v3/fullstats',params={
                'ids':','.join(str(identifier) for identifier in ids),'beginDate':current.isoformat(),'endDate':end.isoformat()})
            if not isinstance(response,list):raise ApiError('Рекламная статистика не является списком')
            found={}
            for value in response:
                identifier=value.get('advertId') if isinstance(value,dict) else None
                if isinstance(identifier,bool) or not isinstance(identifier,int) or identifier not in ids or identifier in found:raise ApiError('Неожиданная или повторяющаяся кампания в статистике')
                validate_statistics(value,current,end);found[identifier]=value
            # Отсутствующая кампания — отсутствие данных, а не нулевой расход.
            # Обёртка сохраняет исходный ответ и метаданные запроса раздельно.
            records=tuple(Record(f'{current:%Y-%m}:{identifier}',{
                'request':{'advertId':identifier,'from':current.isoformat(),'to':end.isoformat()},
                'data':found.get(identifier)}) for identifier in ids)
            batch=state['batch']+len(ids)
            next_month=end+timedelta(days=1)
            if batch>=len(state['ids']):state={**state,'month':next_month.isoformat(),'batch':0}
            else:state={**state,'batch':batch}
            completed=date.fromisoformat(state['month'])>self.end
            yield Page(records,json.dumps(state,sort_keys=True),completed)
            if completed:return

import json
from datetime import date
from ..sync import Page, Record
from .transport import ApiError

class ReportTotals:
    name='report_totals'
    category='finance'
    def __init__(self,client,start,end):self.client,self.start,self.end=client,start,end
    async def pages(self,cursor):
        state=json.loads(cursor) if cursor else {'start':self.start.isoformat(),'end':self.end.isoformat(),'offset':0}
        self.start,self.end=date.fromisoformat(state['start']),date.fromisoformat(state['end'])
        while True:
            rows=await self.client.request('POST','/api/finance/v1/sales-reports/list',body={
                'dateFrom':state['start'],'dateTo':state['end'],'offset':state['offset'],'limit':1000,'period':'weekly'})
            if rows is None:
                yield Page((),json.dumps(state,sort_keys=True),True);return
            if not isinstance(rows,list) or any(not isinstance(r,dict) or not isinstance(r.get('reportId'),int) for r in rows):
                raise ApiError('Изменилась схема контрольных сумм финансовых отчётов')
            state={**state,'offset':state['offset']+len(rows)}
            complete=len(rows)<1000
            yield Page(tuple(Record(str(r['reportId']),r) for r in rows),json.dumps(state,sort_keys=True),complete)
            if complete:return

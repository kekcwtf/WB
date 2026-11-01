"""Нативный график с разрывами. Float применяется только к координатам Qt."""
from datetime import date,timedelta
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor,QCursor
from PySide6.QtWidgets import QToolTip
from PySide6.QtCharts import QChart,QChartView,QLineSeries,QCategoryAxis,QValueAxis
from .daily_finance import daily_finances
from .finance import ZERO


class DailyChart(QChartView):
    def __init__(self,parent=None):
        super().__init__(QChart(),parent)
        self.setMinimumHeight(230)
        self.chart().legend().hide()
        self.balances={}
        self.segments=()
        self.report=None
        self.mode='before_tax'
        self.set_operations([],[])

    def set_operations(self,operations,costs,rules=(),*,start=None,end=None,coverage_confirmed=False,mode='before_tax'):
        if mode not in {'before_tax','net'}:raise ValueError('Неизвестный режим графика')
        self.mode=mode
        report=daily_finances(operations,costs,rules,start=start,end=end,coverage_confirmed=coverage_confirmed)
        self.report=report
        self.balances={row.day:(row.net if mode=='net' else row.before_tax) for row in report.rows
                       if (row.net if mode=='net' else row.before_tax) is not None}
        segments=[]
        for day in self.balances:
            if not segments or day-segments[-1][-1] != timedelta(days=1):segments.append([])
            segments[-1].append(day)
        self.segments=tuple(tuple(days) for days in segments)
        chart=self.chart()
        chart.removeAllSeries()
        for axis in list(chart.axes()):
            chart.removeAxis(axis);axis.deleteLater()
        if not self.balances:
            chart.setTitle('Чистая прибыль по дням недоступна: требуется полный расчёт периода' if mode=='net'
                           else 'График недоступен: нет достаточно полных операций по дням')
            return
        chart.setTitle('Чистая прибыль · расчёт с распределением налогов периода' if mode=='net'
                       else 'До НДС и УСН · загруженные проводки, пропуски не соединяются')
        positions={day:day.toordinal() for day in self.balances}
        days_by_position={value:day for day,value in positions.items()}
        horizontal=QCategoryAxis()
        horizontal.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
        first,last=min(positions.values()),max(positions.values())
        if start is not None and end is not None and (end-start).days<=3660:
            first,last=start.toordinal(),end.toordinal()
        horizontal.setStartValue(first-1)
        ticks=min(7,last-first+1)
        for index in range(ticks):
            value=first+index*(last-first)//max(1,ticks-1)
            horizontal.append(date.fromordinal(value).strftime('%d.%m.%Y'),float(value))
        horizontal.setRange(float(first-1 if first==last else first),float(last+1 if first==last else last))
        vertical=QValueAxis()
        low,high=min(ZERO,*self.balances.values()),max(ZERO,*self.balances.values())
        vertical.setRange(float(low)-1 if low==high else float(low),float(high)+1 if low==high else float(high))
        vertical.setTitleText('₽ · чистая прибыль, расчёт' if mode=='net' else '₽ · до НДС и УСН')
        chart.addAxis(horizontal,Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(vertical,Qt.AlignmentFlag.AlignLeft)
        def hovered(point,visible):
            day=days_by_position.get(round(point.x()))
            if visible and day is not None:
                QToolTip.showText(QCursor.pos(),f'{day:%d.%m.%Y}: {self.balances[day]:.2f} ₽',self)
            else:QToolTip.hideText()
        for segment in self.segments:
            series=QLineSeries()
            series.setColor(QColor('#356bb3'))
            series.setPointsVisible(True)
            for day in segment:series.append(float(positions[day]),float(self.balances[day]))
            series.hovered.connect(hovered)
            chart.addSeries(series)
            series.attachAxis(horizontal);series.attachAxis(vertical)

"""Explain date-bound actions without changing the existing business-day rules."""


def continue_today(day, today):
    return (f'当前作业日期是 {day}，今天是 {today}。\n'
            '请到“草稿记录”选中原记录，点击“用此配置新建今日草稿”，核对今日输入并重新计算后继续。原日期草稿保留。')


def export_previous(day, today, signed):
    reason = ('当前内容与原已签发版本不同，旧日期不能新增签发。若要原单，请从订单跟踪导出原已签发版本。'
              if signed else '这份旧日期草稿尚未签发，不能直接导出为正式签发单。')
    return reason + '\n' + continue_today(day, today)

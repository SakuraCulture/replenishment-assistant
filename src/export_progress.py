"""Measured row progress; no estimated time percentage or synthetic completion."""
class RowProgress(str):
    def __new__(cls,text,current,total):
        value=super().__new__(cls,text);value.current=current;value.total=total;return value


def rows(progress,label,current,total):
    progress(RowProgress(f'{label}：已写 {current:,} / {total:,} 行',current,total))


def prefixed(progress,prefix):
    def emit(message):
        text=prefix+' · '+str(message)
        progress(RowProgress(text,message.current,message.total) if isinstance(message,RowProgress) else text)
    return emit

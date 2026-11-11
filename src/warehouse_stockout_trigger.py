"""Independent opt-in schedule marker, read without opening analysis data."""
import stockout_trigger as shared
NAME='warehouse-stockout-schedule.json'
def load(folder):return shared.load(folder,filename=NAME)
def save(folder,enabled,at='08:00',clock=None):return shared.save(folder,enabled,at,clock,filename=NAME)
def due(folder,clock=None):return shared.due(folder,clock,filename=NAME)

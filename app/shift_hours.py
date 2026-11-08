"""Working day rule: everyone starts at 09:00 and works until the last sale.

Plans are stored as 09:00–23:59 local time. 23:59 marks an open end: there is
no planned leaving time, so leaving "early" does not exist for such a shift;
the employee closes the shift in the app after the last sale.
"""
WORK_START = '09:00'
OPEN_END = '23:59'


def is_open_end(local_end):
    """local_end: an aware datetime in the business time zone."""
    return local_end is not None and local_end.strftime('%H:%M') >= OPEN_END

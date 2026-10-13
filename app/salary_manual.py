"""Manual payroll ledger for existing employees."""
from decimal import Decimal

PERIOD = '14.09-27.09.2026'
SALARY = 'manual_salary'
PAYMENT = 'manual_salary_payment'


def remaining(accrued: Decimal, paid: Decimal) -> Decimal:
    return accrued - paid

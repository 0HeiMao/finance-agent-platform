"""Validate interview fixtures locally, without importing data or calling a model."""
import csv
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.api_financial import validate_row


def main():
    totals = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
    with (ROOT / 'data/demo/interview/finance_demo.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        validate_row(row)
        key = (row['部门'], row['交易日期'][:7])
        for index, column in enumerate(['收入金额', '成本金额', '费用金额']):
            totals[key][index] += Decimal(row[column])
    assert len(rows) == 12
    assert totals['INTERVIEW_DEMO_A', '2026-02'] == [Decimal(10000), Decimal(4500), Decimal(6500)]
    assert sum((values[0] for (department, _), values in totals.items() if department == 'INTERVIEW_DEMO_A'), Decimal(0)) == 30000
    assert not any(period >= '2026-04' for _, period in totals)
    print(json.dumps({'rows': len(rows), 'status': 'passed', 'writes_database': False, 'external_model_calls': 0}))


if __name__ == '__main__':
    main()

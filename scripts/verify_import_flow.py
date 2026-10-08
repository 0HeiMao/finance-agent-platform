"""真实 PostgreSQL + ASGI 接口回归；数据库外层事务最终回滚，原始文件写入临时目录。"""
import asyncio
import io
import os
import secrets
import sys
import tempfile
from contextlib import chdir
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import engine, get_db
from backend.main import app
from backend.models import FinancialMetric


async def main():
    with engine.connect() as connection:
        transaction = connection.begin()
        db = Session(bind=connection, join_transaction_mode='create_savepoint')
        def test_db():
            yield db
        app.dependency_overrides[get_db] = test_db
        cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory(prefix='finance-regression-') as directory, chdir(directory):
                os.chdir(directory)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    credentials = {'username': 'regression_' + secrets.token_hex(8), 'password': secrets.token_urlsafe(20)}
                    assert (await client.post('/api/v1/auth/register', json=credentials)).status_code == 200
                    login = await client.post('/api/v1/auth/login', json=credentials)
                    assert login.status_code == 200
                    client.headers['Authorization'] = 'Bearer ' + login.json()['access_token']
                    assert (await client.get('/api/v1/financial/template')).status_code == 200
                    header = '交易日期,科目名称,收入金额,成本金额,费用金额\n'
                    content = (header + '2099-01-01,测试,100.005,90,20\n').encode('utf-8-sig')
                    async def upload(action, value, name='test.csv'):
                        return await client.post('/api/v1/financial/' + action, files={'file': (name, value)})
                    preview = await upload('preview', content)
                    assert preview.status_code == 200 and preview.json()['valid']
                    imported = await upload('import', content)
                    assert imported.status_code == 200 and imported.json()['imported'] == 1, imported.text
                    # department IS NULL 才是全公司汇总行；部门和汇总行会同时存在，不过滤会多行报错。
                    metric = db.scalar(select(FinancialMetric).where(
                        FinancialMetric.period == '2099-01', FinancialMetric.department.is_(None)))
                    assert str(metric.revenue_amount) == '100.01'
                    assert str(metric.net_profit_amount) == '-9.99'
                    assert (await upload('import', content)).status_code == 409
                    # Duplicate detection rolls back its savepoint only, leaving prior inserts available.
                    invalid = (header + ',测试,NaN,0,0\n').encode('utf-8-sig')
                    check = await upload('preview', invalid)
                    assert not check.json()['valid'] and check.json()['error_count'] == 1
                    check = await upload('import', invalid)
                    assert check.status_code == 200 and check.json()['imported'] == 0
                    import pandas as pd
                    excel = io.BytesIO()
                    pd.DataFrame([{'交易日期':'2099-02-01','科目名称':'Excel测试','收入金额':'200.00'}]).to_excel(excel, index=False)
                    assert (await upload('preview', excel.getvalue(), 'test.xlsx')).json()['valid']
                    assert (await upload('import', excel.getvalue(), 'test.xlsx')).json()['imported'] == 1
                    alerts = (await client.get('/api/v1/analysis/alerts', params={'period':'2099-01'})).json()['data']
                    assert alerts
                    details = await client.get(f"/api/v1/analysis/alerts/{alerts[0]['id']}/transactions")
                    assert details.status_code == 200 and len(details.json()['data']) == 1
                    dashboard = await client.get('/api/v1/dashboard', params={'period':'year'})
                    assert dashboard.status_code == 200
                    print('PASS: registration, login, template, CSV/XLSX preview and import, Decimal rounding, duplicate rejection, invalid row rejection, alerts, dashboard')
        finally:
            os.chdir(cwd)
            app.dependency_overrides.clear()
            db.close()
            transaction.rollback()


if __name__ == '__main__':
    asyncio.run(main())

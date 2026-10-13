"""Seed only the explicitly isolated interview database via the real import service."""
import asyncio
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def seed():
    from fastapi import HTTPException, UploadFile
    from sqlalchemy import select
    from starlette.requests import Request

    from backend.api_financial import import_excel
    from backend.auth import pwd_context
    from backend.db import SessionLocal, engine
    from backend.models import User

    if os.environ.get('FINANCE_DEMO_CONTAINER') != '1' or engine.url.database != 'finance_interview_demo':
        raise RuntimeError('Refusing to seed outside the isolated demo database.')
    password = os.environ.get('DEMO_ADMIN_PASSWORD')
    if not password:
        raise RuntimeError('DEMO_ADMIN_PASSWORD is required.')
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == 'interview_admin'))
        if user is None:
            user = User(username='interview_admin', password_hash=pwd_context.hash(password), is_admin=True)
            db.add(user)
            db.commit()
            db.refresh(user)
        request = Request({'type': 'http', 'method': 'POST', 'path': '/demo-seed', 'headers': [],
                           'client': ('127.0.0.1', 0)})
        source = ROOT / 'data/demo/interview/finance_demo.csv'
        upload = UploadFile(filename=source.name, file=io.BytesIO(source.read_bytes()))
        try:
            result = await import_excel(request=request, file=upload, db=db, user=user)
            if not result.get('success'):
                raise RuntimeError('Demo import validation failed.')
            print('Synthetic finance data imported using the application import service.')
        except HTTPException as error:
            if error.status_code != 409:
                raise
            print('Synthetic finance data already imported; no duplicate rows added.')
        finally:
            await upload.close()


if __name__ == '__main__':
    asyncio.run(seed())

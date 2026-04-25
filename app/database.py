from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()

# Railway injects DATABASE_URL as postgres:// but SQLAlchemy 2.x requires postgresql://
_db_url = settings.database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(_db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

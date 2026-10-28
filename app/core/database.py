from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)

# Habilitar WAL mode e foreign keys para SQLite
if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    # Importar modelos para registro no metadata
    import app.models  # noqa
    Base.metadata.create_all(bind=engine)

    # Garantir que novas colunas existam em bancos SQLite já criados
    try:
        inspector = inspect(engine)
        if "automations" in inspector.get_table_names():
            columns = {col["name"] for col in inspector.get_columns("automations")}
            with engine.begin() as conn:
                if "cleanup_mode" not in columns:
                    conn.execute(text("ALTER TABLE automations ADD COLUMN cleanup_mode VARCHAR(30) NOT NULL DEFAULT 'none'"))
                if "target_worker" not in columns:
                    conn.execute(text("ALTER TABLE automations ADD COLUMN target_worker VARCHAR(100) NOT NULL DEFAULT 'local'"))
    except Exception:
        pass

    # Criação do usuário admin inicial se não existir
    from app.models.user import User
    from app.core.security import hash_password

    with SessionLocal() as db:
        admin_user = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if not admin_user:
            admin_user = User(
                username=settings.ADMIN_USERNAME,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
                is_admin=True,
            )
            db.add(admin_user)
            db.commit()

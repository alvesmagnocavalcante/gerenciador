import logging
from datetime import datetime, timezone
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
    with SessionLocal() as db:
        db_engine = db.get_bind()
    Base.metadata.create_all(bind=db_engine)

    # Garantir que novas colunas existam em bancos SQLite já criados
    inspector = inspect(db_engine)
    if "automations" in inspector.get_table_names():
        columns = {col["name"] for col in inspector.get_columns("automations")}
        with db_engine.begin() as conn:
            if "cleanup_mode" not in columns:
                conn.execute(text("ALTER TABLE automations ADD COLUMN cleanup_mode VARCHAR(30) NOT NULL DEFAULT 'none'"))
            if "target_worker" not in columns:
                conn.execute(text("ALTER TABLE automations ADD COLUMN target_worker VARCHAR(100) NOT NULL DEFAULT 'local'"))
    execution_columns = {col["name"] for col in inspector.get_columns("executions")}
    with db_engine.begin() as conn:
        if "owner_id" not in execution_columns:
            conn.execute(text("ALTER TABLE executions ADD COLUMN owner_id VARCHAR(150)"))
        if "heartbeat_at" not in execution_columns:
            conn.execute(text("ALTER TABLE executions ADD COLUMN heartbeat_at DATETIME"))
        if "cancel_requested" not in execution_columns:
            conn.execute(text("ALTER TABLE executions ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT 0"))
        if "progress_percent" not in execution_columns:
            conn.execute(text("ALTER TABLE executions ADD COLUMN progress_percent INTEGER"))
        if "progress_message" not in execution_columns:
            conn.execute(text("ALTER TABLE executions ADD COLUMN progress_message VARCHAR(255)"))
        if "progress_updated_at" not in execution_columns:
            conn.execute(text("ALTER TABLE executions ADD COLUMN progress_updated_at DATETIME"))
        if "heartbeat_at" not in execution_columns:
            # Uma janela de graça permite que agentes legados enviem o primeiro heartbeat.
            conn.execute(text("UPDATE executions SET heartbeat_at=:now WHERE status IN ('PENDING','RUNNING')"),
                         {"now": datetime.now(timezone.utc)})
            conn.execute(text("""UPDATE executions SET owner_id='remote:' ||
                (SELECT target_worker FROM automations WHERE automations.id=executions.automation_id)
                WHERE status IN ('PENDING','RUNNING') AND automation_id IN
                (SELECT id FROM automations WHERE lower(target_worker) != 'local')"""))
    from app.models.execution import Execution
    for index in Execution.__table__.indexes:
        index.create(bind=db_engine, checkfirst=True)

    # Criação do usuário admin inicial se não existir
    from app.models.user import User
    from app.core.security import hash_password, verify_password

    with SessionLocal() as db:
        admin_user = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if not admin_user:
            if not settings.ADMIN_PASSWORD:
                raise RuntimeError("Defina ADMIN_PASSWORD (mínimo 12 caracteres) para criar o administrador.")
            admin_user = User(
                username=settings.ADMIN_USERNAME,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
                is_admin=True,
            )
            db.add(admin_user)
            db.commit()
        elif verify_password(admin_user.password_hash, "admin123"):
            if not settings.ADMIN_PASSWORD:
                raise RuntimeError("Defina ADMIN_PASSWORD para substituir a senha padrão insegura.")
            admin_user.password_hash = hash_password(settings.ADMIN_PASSWORD)
            db.commit()
            logging.getLogger("pyautomation").warning("Senha legada padrão do administrador substituída pela configuração segura.")

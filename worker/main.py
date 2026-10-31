import time
import signal
import sys
import logging

from app.core.database import init_db
from app.services.executor import cleanup_orphaned_executions, start_executor, stop_executor
from app.services.scheduler import start_scheduler, shutdown_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Worker] %(name)s: %(message)s"
)
# O reconciliador roda a cada dois segundos; manter apenas avisos e erros.
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logger = logging.getLogger("pyautomation.worker")

def handle_exit(signum, frame):
    logger.info("Recebido sinal de encerramento do worker...")
    sys.exit(0)

def main():
    logger.info("Iniciando Worker de Agendamento do PyAutomation...")
    init_db()
    cleanup_orphaned_executions()
    start_executor()
    try:
        start_scheduler()
        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)
        logger.info("Worker ativo e aguardando execuções de agendamentos...")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_scheduler()
        stop_executor()

if __name__ == "__main__":
    main()

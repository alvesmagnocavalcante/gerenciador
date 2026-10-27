import time
import signal
import sys
import logging

from app.core.database import init_db
from app.services.scheduler import start_scheduler, shutdown_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Worker] %(name)s: %(message)s"
)
logger = logging.getLogger("pyautomation.worker")

def handle_exit(signum, frame):
    logger.info("Recebido sinal de encerramento do worker...")
    shutdown_scheduler()
    sys.exit(0)

def main():
    logger.info("Iniciando Worker de Agendamento do PyAutomation...")
    init_db()
    start_scheduler()

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    logger.info("Worker ativo e aguardando execuções de agendamentos...")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        handle_exit(None, None)

if __name__ == "__main__":
    main()

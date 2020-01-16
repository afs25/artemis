import logging
import sys

# region Logging
def get_logger():
    logger = logging.getLogger('artemis')
    log_handler = logging.StreamHandler(stream=sys.stdout)
    formatter = logging.Formatter('[%(asctime)s - %(levelname)-8s - %(module)-20s:%(lineno)4s - %(funcName)-45s] - %(message)s')
    formatter.default_msec_format = '%s.%03d'
    log_handler.setFormatter(formatter)
    if not logger.handlers:
        logger.addHandler(log_handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger
# endregion

import logging
import runpy
logging.getLogger("httpx").setLevel(logging.WARNING)
runpy.run_path("bot music.py", run_name="__main__")

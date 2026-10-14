import sys
from pathlib import Path

# _app must import under its plain name in spawned pool workers too: they unpickle
# `_app.pool_domains` by reference, and a spawned child inherits the parent's sys.path.
sys.path.insert(0, str(Path(__file__).parent))

import os
import tempfile

# No test reaches the core of the machine running it through its core.env:
# the home is empty unless a test writes one.
os.environ["LOCALITAS_HOME"] = tempfile.mkdtemp(prefix="localitas-home-")

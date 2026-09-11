"""The legacy target application: a fixture, not part of the system.

Loads .env here rather than in a submodule because auth.py reads its
configuration at import time, and this package's __init__ is the only thing
guaranteed to run before it. Values already present in the real environment win.
"""

from dotenv import load_dotenv

load_dotenv()

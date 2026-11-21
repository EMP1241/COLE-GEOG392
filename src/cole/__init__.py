"""COLE: local county household-cost exploration.

Project attribution: Brandon Santor <brandon.santor@tamu.edu>
Source: https://github.com/EMP1241/cole-cost-of-living-explorer
"""

# ---------------------------------------------------------------------------
# WHAT THIS FILE IS
# ---------------------------------------------------------------------------
# In Python, a folder becomes an importable "package" when it contains a file
# named exactly __init__.py. That is this file's main job: its existence tells
# Python "this folder is a unit of code you can load."
#
# The only thing it actually defines is the application's version number.
# Other files read it with:
#
#     from . import __version__
#
# The leading dot means "from this same package" (this same folder), as opposed
# to some library installed elsewhere on the computer.
#
# WHY THE VERSION MATTERS HERE
# The running app stamps this number into HTTP response headers and into the
# web page itself. If the number the browser page was loaded with stops
# matching the number the server is reporting, COLE refuses to keep going and
# asks the user to reload. That guards against a half-updated session where the
# old page is talking to a new server (see app.py, protect_local_service).
#
# So: bumping this number is not cosmetic. It deliberately invalidates any
# browser tab still running the previous build.
# ---------------------------------------------------------------------------

__version__ = "1.0.1"
__author__ = "Brandon Santor"
__email__ = "brandon.santor@tamu.edu"
__source__ = "https://github.com/EMP1241/cole-cost-of-living-explorer"

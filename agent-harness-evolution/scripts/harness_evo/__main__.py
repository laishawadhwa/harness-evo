import sys

if sys.version_info < (3, 9):  # keep this file free of newer syntax: it must run on any Python 3
    if sys.argv[1:2] == ["hook"]:
        sys.stdin.read()  # hooks fail open
        sys.exit(0)
    sys.stderr.write("harness-evo needs Python 3.9 or newer; set HARNESS_EVO_PYTHON to one.\n")
    sys.exit(1)

from harness_evo.cli import main  # noqa: E402

sys.exit(main())

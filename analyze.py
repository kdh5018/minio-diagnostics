#!/usr/bin/env python3
"""R1 entrypoint. Standard library only; never collects or connects to a server."""
from aistordoctor import DEFAULT_OUTPUT_DIR, main

if __name__ == '__main__':
    raise SystemExit(main(default_out_dir=DEFAULT_OUTPUT_DIR))

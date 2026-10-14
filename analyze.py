#!/usr/bin/env python3
"""R1 entrypoint. Standard library only; never collects or connects to a server."""
import sys
from aistordoctor import DEFAULT_OUTPUT_DIR, main

if __name__ == '__main__':
    if any(arg == '--health' or arg.startswith('--health=') for arg in sys.argv[1:]):
        from r2_health import main as health_main
        raise SystemExit(health_main())
    raise SystemExit(main(default_out_dir=DEFAULT_OUTPUT_DIR))

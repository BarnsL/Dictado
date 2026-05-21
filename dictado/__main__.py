"""Allow `python -m dictado` to launch the daemon (or send IPC commands)."""
from .daemon import main

if __name__ == "__main__":
    main()

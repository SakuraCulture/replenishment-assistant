"""Read-only validator before PyInstaller's Qt runtime hooks, no QApplication/user settings."""
import sys
if '--catalog-validate' in sys.argv:
    from catalog_sync_validation import cli
    raise SystemExit(cli(sys.argv[sys.argv.index('--catalog-validate')+1:]))

"""Headless workers/validator before PyInstaller's Qt relocation hooks."""
import sys
if len(sys.argv)>1 and sys.argv[1]=='--multiprocessing-fork':
    # Windows frozen spawn must dispatch before the standard Qt hook imports
    # QtCore. All process targets are headless; the foreground path is unchanged.
    import multiprocessing
    multiprocessing.freeze_support()
if '--catalog-validate' in sys.argv:
    from catalog_sync_validation import cli
    raise SystemExit(cli(sys.argv[sys.argv.index('--catalog-validate')+1:]))

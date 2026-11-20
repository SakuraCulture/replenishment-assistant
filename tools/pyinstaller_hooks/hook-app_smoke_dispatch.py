"""Include registered smoke modules in the frozen application."""
from PyInstaller.utils.hooks import get_module_attribute

hiddenimports = list(get_module_attribute('app_smoke_dispatch', 'SMOKE_MODULES'))

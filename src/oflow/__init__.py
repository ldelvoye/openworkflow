from importlib.metadata import version

# oflow only ever runs installed — uv installs the project before running
# anything — so an uninstalled import has nothing sensible to report.
__version__ = version("oflow")

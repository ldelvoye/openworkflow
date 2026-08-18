from importlib.metadata import version

# smorg only ever runs installed — uv installs the project before running
# anything — so an uninstalled import has nothing sensible to report.
__version__ = version("smorg")

"""kryten-economy — Channel engagement currency microservice."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kryten-economy")
except PackageNotFoundError:
    __version__ = "0.0.0"

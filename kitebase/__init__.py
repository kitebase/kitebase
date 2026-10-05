
__version__ = "0.6.0"

# Import only autoimport for internal use
# Other utilities available via kitebase.utils
from .utils import autoimport

# Auto-import internal modules
autoimport(__file__, __package__)

# Public API
__all__ = ['autoimport']

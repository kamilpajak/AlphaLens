"""Research test suite.

Arms the no-live-network guard at import, which is the moment discovery loads
this package — so it covers every test in the tree, not only the ones a future
author remembers to protect. Rationale and escape hatches: ``_net_guard``.
"""

from tests._net_guard import install_network_guard

install_network_guard()

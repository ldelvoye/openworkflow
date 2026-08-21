"""The ctrl+p menu's management surface, one module per concern: `base` is what every management
screen shares, `remove` and `connect` are the two screen flows, and `commands` is the palette
provider that offers them.
"""

from smorg.shell.menu.base import ManagementScreen
from smorg.shell.menu.commands import ADD_COMMAND, REMOVE_COMMAND, MenuCommands
from smorg.shell.menu.connect import (
    AddableIntegration,
    AddConnectionList,
    AddIntegrationList,
    ClientIdModal,
    ConnectModal,
    TokenModal,
    addable_integrations,
    connect_screen_for,
    open_tab_for,
)
from smorg.shell.menu.remove import (
    RemovableTab,
    RemoveConfirmModal,
    RemoveIntegrationList,
    removable_tabs,
)

__all__ = [
    "ADD_COMMAND",
    "REMOVE_COMMAND",
    "AddConnectionList",
    "AddIntegrationList",
    "AddableIntegration",
    "ClientIdModal",
    "ConnectModal",
    "ManagementScreen",
    "MenuCommands",
    "RemovableTab",
    "RemoveConfirmModal",
    "RemoveIntegrationList",
    "TokenModal",
    "addable_integrations",
    "connect_screen_for",
    "open_tab_for",
    "removable_tabs",
]

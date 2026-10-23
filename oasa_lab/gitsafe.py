"""How the lab runs git on a commit it does not trust. Standard library only.

No user or system config, no hooks, no prompts, no helper programs. The one global file allowed is a root-owned
one that only names the mirror as safe (`safe.directory`; git ignores that setting anywhere else).
"""

from __future__ import annotations

import os

# git settings that stop a checkout from running anything or reading a config someone planted
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never",
            "-c", "advice.detachedHead=false")


def git_env(global_config: str | None = None) -> dict[str, str]:
    cfg = global_config if global_config and os.path.isfile(global_config) else "/dev/null"
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": cfg, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C"}

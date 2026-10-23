from __future__ import annotations

from typing import Any


def menu_actions(bot: Any, message: dict, *actions: str) -> Any:
    """Open a menu and exercise its real callback routes in integration tests."""
    bot.handlers.handle_message({**message, "text": "/menu"})
    session = list(bot.handlers.menu._sessions.values())[-1]
    for action in actions:
        bot.handlers.handle_callback(
            {
                "from": message["from"],
                "data": f"m:{session.token}:{action}",
                "message": {**message, "message_id": session.message_id},
            }
        )
    return session

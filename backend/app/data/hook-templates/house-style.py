"""Add this group's rules to every prompt a member sends.

Installed from the template gallery as a draft, switched off. `pre_prompt` is the one event that
can only *append*: nothing returned here can replace or remove what the app itself assembled, so a
hook in this position cannot quietly take the group's memory or its settings out of the prompt.

Edit RULES to whatever your group actually needs — this is your file now.
"""

RULES = """House rules for this group:
- Answer in Chinese. Lead with the conclusion, then the reasoning.
- Keep every unit and every figure exactly as it was given; never invent a number.
- Say plainly when something is not known instead of guessing."""


def handle(event, payload):
    if payload.get("scene") == "integration":
        # The consolidation is the answer the user reads, so the reminder about unfinished work
        # belongs there rather than on every task instruction.
        return {"append": RULES + "\n- When consolidating, name the tasks that did not finish and what is missing."}
    return {"append": RULES}

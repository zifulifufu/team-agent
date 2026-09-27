"""Provider failure signals shared by chat and media adapters."""

import re


QUOTA_RE = re.compile(
    r"insufficient[_ ]quota|exceeded your current API quota|purchase the API points|"
    r"insufficient (?:balance|credits)|credit balance.*too low|余额不足|额度耗尽",
    re.I,
)


def quota_exhausted(error: BaseException | str) -> bool:
    return bool(QUOTA_RE.search(str(error)))

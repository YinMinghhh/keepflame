"""The two explicitly supported official reward tasks, never arbitrary rules."""

from dataclasses import dataclass
import re


class TaskConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    name: str
    rule: str
    action_kind: str
    button: str
    card_title: str
    article_url: str | None = None
    article_id: str | None = None
    wait_seconds: int = 30


def task_from_settings(settings) -> Task:
    name = settings.get("task", "checkin")
    if name == "checkin":
        return Task("checkin", "login", "COMPLETE_OFFICIAL_SIGNIN", "签到", "签到")
    if name != "reading":
        raise TaskConfigurationError("task_configuration")
    url = settings.get("article_url")
    match = re.fullmatch(r"https://www\.joinquant\.com/view/community/detail/([1-9][0-9]{0,19})", url) if isinstance(url, str) else None
    wait = settings.get("reading_wait_seconds", 30)
    if match is None or type(wait) is not int or not 30 <= wait <= 120:
        raise TaskConfigurationError("reading_configuration")
    return Task("reading", "post_view", "CLAIM_OFFICIAL_READING_REWARD", "立即领取", "浏览社区文章", url, match[1], wait)

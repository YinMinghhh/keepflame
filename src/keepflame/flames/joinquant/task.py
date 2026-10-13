"""The two explicitly supported official reward tasks, never arbitrary rules."""

from dataclasses import dataclass, replace
import re


class TaskConfigurationError(ValueError):
    pass


ARTICLE_PATTERN = re.compile(r"https://www\.joinquant\.com/view/community/detail/([1-9][0-9]{0,19})")
DEFAULT_READING_WAIT_SECONDS = 50
MAX_ARTICLE_CANDIDATES = 200


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
    # Ordered (url, id) candidates. A day reads the first one never read before.
    article_candidates: tuple = ()
    scroll: bool = True


def _article(url):
    match = ARTICLE_PATTERN.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        raise TaskConfigurationError("reading_configuration")
    return url, match[1]


def task_from_settings(settings) -> Task:
    name = settings.get("task", "checkin")
    if name == "checkin":
        return Task("checkin", "login", "COMPLETE_OFFICIAL_SIGNIN", "签到", "签到")
    if name != "reading":
        raise TaskConfigurationError("task_configuration")
    urls = []
    # The legacy single article stays valid and is tried first.
    if "article_url" in settings:
        urls.append(settings.get("article_url"))
    if "article_urls" in settings:
        many = settings.get("article_urls")
        if not isinstance(many, list) or not many or len(many) > MAX_ARTICLE_CANDIDATES:
            raise TaskConfigurationError("reading_configuration")
        urls.extend(many)
    if not urls:
        raise TaskConfigurationError("reading_configuration")
    candidates = tuple(_article(url) for url in urls)
    if len({article_id for _, article_id in candidates}) != len(candidates):
        raise TaskConfigurationError("reading_configuration")
    wait = settings.get("reading_wait_seconds", DEFAULT_READING_WAIT_SECONDS)
    if type(wait) is not int or not 30 <= wait <= 120:
        raise TaskConfigurationError("reading_configuration")
    scroll = settings.get("reading_scroll", True)
    if type(scroll) is not bool:
        raise TaskConfigurationError("reading_configuration")
    url, article_id = candidates[0]
    return Task("reading", "post_view", "CLAIM_OFFICIAL_READING_REWARD", "立即领取", "浏览社区文章",
                url, article_id, wait, candidates, scroll)


def choose_article(task: Task, read_ids) -> Task:
    """Pick the first candidate never read. Exhaustion leaves no article."""
    if task.name != "reading":
        return task
    seen = set(read_ids or ())
    for url, article_id in task.article_candidates:
        if article_id not in seen:
            return replace(task, article_url=url, article_id=article_id)
    return replace(task, article_url=None, article_id=None)

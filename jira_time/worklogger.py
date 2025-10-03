#!/bin/env python3

import configparser
import dataclasses
import datetime
import logging
import pathlib
import random
import re
import sys
import time
from collections.abc import Callable
from dataclasses import field
from typing import Any, Protocol

import questionary
from halo import Halo
from jira import JIRA
from jira.client import ResultList
from jira.exceptions import JIRAError
from jira.resources import Issue
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout as RequestsReadTimeout
from requests.exceptions import RequestException
from urllib3.exceptions import ReadTimeoutError as Urllib3ReadTimeout

logging.basicConfig(level=logging.INFO)


DEFAULT_ISSUE_JQL = "assignee=currentUser() AND statusCategory not in (Done)"
DEFAULT_TEAM_ISSUE_JQL = ""  # Optional, user can configure later
REQUEST_TIMEOUT_SECONDS = 3
SEARCH_RESULT_LIMIT = 50
SEARCH_BY_TEXT_VALUE = "__search_by_text__"
SEARCH_BY_JQL_VALUE = "__search_by_jql__"
MANUAL_ENTRY_VALUE = "__manual_entry__"
RETURN_TO_VIEWS_VALUE = "__return_to_views__"
RETURN_TO_LOG_METHOD_VALUE = "__return_to_log_method__"
VIEW_MY_ISSUES = "__view_my_issues__"
VIEW_TEAM_ISSUES = "__view_team_issues__"
VIEW_PROJECT_ISSUES = "__view_project_issues__"

ISSUE_FIELDS = [
    "id",
    "key",
    "summary",
    "statusCategory",
    "status",
    "description",
]
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")


class QuestionaryIO:
    """Thin wrapper over questionary to allow dependency injection."""

    def text(self, **kwargs: Any) -> str:
        return questionary.text(**kwargs).unsafe_ask()

    def password(self, **kwargs: Any) -> str:
        return questionary.password(**kwargs).unsafe_ask()

    def select(self, **kwargs: Any) -> Any:
        return questionary.select(**kwargs).unsafe_ask()

    def press_any_key(self, **kwargs: Any) -> Any:
        return questionary.press_any_key_to_continue(**kwargs).unsafe_ask()

    def print(self, *args: Any, **kwargs: Any) -> None:
        questionary.print(*args, **kwargs)


@dataclasses.dataclass(kw_only=True)
class Server:
    auth_type: str = "pat"
    url: str
    name: str
    pat: str = ""
    email: str = ""
    api_token: str = ""
    issue_jql: str = DEFAULT_ISSUE_JQL
    team_issue_jql: str = DEFAULT_TEAM_ISSUE_JQL
    project_keys: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.auth_type = self.auth_type.strip()
        self.url = self.url.strip()
        self.name = self.name.strip()
        self.pat = self.pat.strip()
        self.email = self.email.strip()
        self.api_token = self.api_token.strip()
        self.issue_jql = (self.issue_jql or DEFAULT_ISSUE_JQL).strip()
        self.team_issue_jql = (self.team_issue_jql or "").strip()
        normalized_project_keys: list[str] = []
        seen_projects: set[str] = set()
        for key in self.project_keys:
            if not key:
                continue
            normalized = key.strip().upper()
            if not normalized or normalized in seen_projects:
                continue
            seen_projects.add(normalized)
            normalized_project_keys.append(normalized)
        self.project_keys = normalized_project_keys


class Config:
    def __init__(self) -> None:
        self.servers: list[Server] = []
        self.config_dir = pathlib.Path.home().joinpath(
            pathlib.Path(".config/jira-worklogger/")
        )
        self.config_path = self.config_dir.joinpath("jira-worklogger.conf")
        self._parser: configparser.ConfigParser = None

    def load(self) -> None:
        # Ensure config file exists and if not create it
        self.config_dir.mkdir(exist_ok=True)
        self.config_path.touch(exist_ok=True)

        self._parser = configparser.ConfigParser()
        self._parser.read(filenames=self.config_path, encoding="utf-8")

        self.servers: list[Server] = []
        for section in self._parser.sections():
            url = self._parser.get(section=section, option="url")
            auth_type = self._parser.get(
                section=section, option="auth_type", fallback="pat"
            )
            issue_jql = self._parser.get(
                section=section,
                option="issue_jql",
                fallback=DEFAULT_ISSUE_JQL,
            )
            team_issue_jql = self._parser.get(
                section=section,
                option="team_issue_jql",
                fallback=DEFAULT_TEAM_ISSUE_JQL,
            )
            project_keys_raw = self._parser.get(
                section=section,
                option="project_keys",
                fallback="",
            )
            project_keys = [key for key in project_keys_raw.split(",") if key]

            if auth_type == "pat":
                pat = self._parser.get(section=section, option="pat", fallback="")
                if not pat:
                    raise Exception(
                        f'The config file {self.config_path} must define a non-empty PAT for section "{section}".'
                    )
                self.servers.append(
                    Server(
                        auth_type=auth_type,
                        url=url,
                        name=section,
                        pat=pat,
                        issue_jql=issue_jql,
                        team_issue_jql=team_issue_jql,
                        project_keys=project_keys,
                    )
                )
                continue

            if auth_type == "cloud_token":
                email = self._parser.get(section=section, option="email", fallback="")
                api_token = self._parser.get(
                    section=section, option="api_token", fallback=""
                )
                if not email or not api_token:
                    raise Exception(
                        f'The config file {self.config_path} must define both an email and API token for section "{section}".'
                    )
                self.servers.append(
                    Server(
                        auth_type=auth_type,
                        url=url,
                        name=section,
                        email=email,
                        api_token=api_token,
                        issue_jql=issue_jql,
                        team_issue_jql=team_issue_jql,
                        project_keys=project_keys,
                    )
                )
                continue

            raise Exception(
                f"""The config file {self.config_path} has set the "auth_type" for section "{section}" to "{auth_type}" but only "pat" and "cloud_token" are supported now."""
            )

    def write(self, autoreload: bool = True) -> None:
        with open(self.config_path, "w") as f:
            self._parser.write(f)
        if autoreload:
            self.load()

    def add_server(self, s: Server) -> None:
        section = s.name
        if self._parser.has_section(section):
            self._parser.remove_section(section)
        self._parser.add_section(section=section)
        self._parser.set(section=section, option="url", value=s.url)
        self._parser.set(section=section, option="auth_type", value=s.auth_type)
        self._parser.set(section=section, option="issue_jql", value=s.issue_jql)
        self._parser.set(
            section=section, option="team_issue_jql", value=s.team_issue_jql
        )
        self._parser.set(
            section=section,
            option="project_keys",
            value=",".join(s.project_keys),
        )

        if s.auth_type == "pat":
            self._parser.set(section=section, option="pat", value=s.pat)
        elif s.auth_type == "cloud_token":
            self._parser.set(section=section, option="email", value=s.email)
            self._parser.set(section=section, option="api_token", value=s.api_token)
        else:
            raise ValueError(
                f"Unsupported auth_type '{s.auth_type}' for server '{s.name}'"
            )
        self.write()


class ServerPrompter:
    def __init__(self, prompt: QuestionaryIO) -> None:
        self._prompt = prompt

    def prompt_for_new_server(self, config: "Config") -> Server:
        url = self._prompt.text(
            message="Which JIRA server to connect to?",
            default="https://your-instance.atlassian.net",
            validate=lambda text: True
            if len(text) > 0
            else "Please, enter a JIRA server",
        ).strip()
        auth_type = self._prompt.select(
            message="Which authentication method do you want to configure?",
            default="cloud_token",
            choices=[
                questionary.Choice(
                    title="Jira Cloud - Email and API token",
                    value="cloud_token",
                ),
                questionary.Choice(
                    title="Jira Server / Data Center - Personal Access Token",
                    value="pat",
                ),
            ],
        )
        name = self._prompt.text(
            message="What name to give your server?",
            default="Red Hat",
            validate=self._validate_server_name(config),
        ).strip()

        issue_jql = self._prompt.text(
            message="Which JQL should be used to list issues by default?",
            default=DEFAULT_ISSUE_JQL,
        ).strip()
        if not issue_jql:
            issue_jql = DEFAULT_ISSUE_JQL

        team_issue_jql = self._prompt.text(
            message="Optional JQL for shared/team buckets (leave blank to skip):",
            default=DEFAULT_TEAM_ISSUE_JQL,
        ).strip()

        project_keys_input = self._prompt.text(
            message="Optional Jira project keys for broader searches (comma separated):",
            default="",
        ).strip()
        project_keys = [
            key.strip() for key in project_keys_input.split(",") if key.strip()
        ]

        if auth_type == "pat":
            pat = self._prompt.password(
                message="What is your JIRA Personal Access Token (PAT)?",
                validate=lambda text: True if len(text) > 0 else "Please enter a value",
            ).strip()
            self._prompt.print(
                "The token is stored unencrypted in ~/.config/jira-worklogger/jira-worklogger.conf.",
                style="fg:ansiyellow",
            )
            return Server(
                auth_type="pat",
                url=url,
                name=name,
                pat=pat,
                issue_jql=issue_jql,
                team_issue_jql=team_issue_jql,
                project_keys=project_keys,
            )

        email = self._prompt.text(
            message="What is your Atlassian account email?",
            validate=lambda text: True if len(text) > 0 else "Please enter a value",
        ).strip()
        api_token = self._prompt.password(
            message="What is your Jira Cloud API token?",
            instruction="Create one at https://id.atlassian.com/manage-profile/security/api-tokens",
            validate=lambda text: True if len(text) > 0 else "Please enter a value",
        ).strip()
        self._prompt.print(
            "The email and API token are stored unencrypted in ~/.config/jira-worklogger/jira-worklogger.conf.",
            style="fg:ansiyellow",
        )
        return Server(
            auth_type="cloud_token",
            url=url,
            name=name,
            email=email,
            api_token=api_token,
            issue_jql=issue_jql,
            team_issue_jql=team_issue_jql,
            project_keys=project_keys,
        )

    def _validate_server_name(self, config: "Config") -> Callable[[str], str | bool]:
        def validate(name: str) -> str | bool:
            if len(name) <= 0:
                return "Please, enter a name for the server!"
            if config._parser.has_section(name):
                return "Name is already taken, please choose another one!"
            return True

        return validate


def add_new_server(config: "Config", prompter: ServerPrompter) -> None:
    server = prompter.prompt_for_new_server(config)
    config.add_server(server)


class AuthStrategy(Protocol):
    def supports(self, server: Server) -> bool: ...

    def authenticate(
        self,
        server: Server,
        connector: Callable[..., tuple[JIRA, dict[str, Any]]],
    ) -> tuple[JIRA, dict[str, Any]]: ...


class PatAuthStrategy:
    def supports(self, server: Server) -> bool:
        return server.auth_type == "pat"

    def authenticate(
        self,
        server: Server,
        connector: Callable[..., tuple[JIRA, dict[str, Any]]],
    ) -> tuple[JIRA, dict[str, Any]]:
        if not server.pat:
            raise ValueError(
                f"No personal access token configured for server '{server.name}'."
            )
        return connector(token_auth=server.pat)


class CloudTokenAuthStrategy:
    def supports(self, server: Server) -> bool:
        return server.auth_type == "cloud_token"

    def authenticate(
        self,
        server: Server,
        connector: Callable[..., tuple[JIRA, dict[str, Any]]],
    ) -> tuple[JIRA, dict[str, Any]]:
        errors: list[JIRAError] = []

        if server.email and server.api_token:
            try:
                return connector(basic_auth=(server.email, server.api_token))
            except JIRAError as ex:
                if ex.status_code != 401:
                    raise
                logging.debug(
                    "Authentication method '%s' failed for server '%s': %s",
                    "email+api_token",
                    server.name,
                    ex.text,
                )
                errors.append(ex)

        if server.api_token:
            try:
                return connector(token_auth=server.api_token)
            except JIRAError as ex:
                if ex.status_code != 401:
                    raise
                logging.debug(
                    "Authentication method '%s' failed for server '%s': %s",
                    "bearer",
                    server.name,
                    ex.text,
                )
                errors.append(ex)

        if errors:
            raise errors[-1]

        raise ValueError(
            f"Incomplete Jira Cloud credentials for server '{server.name}'."
        )


class JiraAuthenticator:
    def __init__(self, strategies: list[AuthStrategy]) -> None:
        self._strategies = strategies

    def authenticate(
        self,
        server: Server,
        connector: Callable[..., tuple[JIRA, dict[str, Any]]],
    ) -> tuple[JIRA, dict[str, Any]]:
        for strategy in self._strategies:
            if strategy.supports(server):
                return strategy.authenticate(server, connector)
        raise ValueError(
            f"Unsupported authentication type '{server.auth_type}' for server '{server.name}'."
        )


class JiraService:
    def __init__(self, client: JIRA) -> None:
        self._client = client
        self._max_attempts = 3
        self._base_delay = 1.0

    def _is_transient(self, ex: Exception) -> bool:
        if isinstance(
            ex, (RequestsConnectionError, RequestsReadTimeout, Urllib3ReadTimeout)
        ):
            return True
        if isinstance(ex, JIRAError):
            # Treat rate limiting and 5xx as transient
            return ex.status_code in {429, 500, 502, 503, 504}
        return False

    def _retry(self, action_desc: str, func: Callable[[], Any]) -> Any:
        last_ex: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return func()
            except Exception as ex:  # noqa: BLE001 broad but filtered by _is_transient
                last_ex = ex
                if not self._is_transient(ex) or attempt == self._max_attempts:
                    break
                # Exponential backoff with jitter
                delay = self._base_delay * (2 ** (attempt - 1))
                delay = delay + random.uniform(0, 0.25 * delay)
                logging.warning(
                    "Transient failure during %s (attempt %d/%d): %s. Retrying in %.1fs...",
                    action_desc,
                    attempt,
                    self._max_attempts,
                    getattr(ex, "message", str(ex)),
                    delay,
                )
                time.sleep(delay)
        # Exhausted retries; raise last exception
        assert last_ex is not None
        raise last_ex

    def myself(self) -> dict[str, Any]:
        return self._retry("fetch profile", lambda: self._client.myself())

    def search_issues(
        self,
        jql: str,
        *,
        fields: list[str],
        limit: int | None = None,
    ) -> list[Issue]:
        search_kwargs: dict[str, Any] = {
            "jql_str": jql,
            "fields": fields,
        }
        search_kwargs["maxResults"] = False if limit is None else limit
        results: ResultList[Issue] = self._retry(
            "search issues", lambda: self._client.search_issues(**search_kwargs)
        )
        return list(results)

    def get_issue(self, issue_key: str, *, fields: list[str] | None = None) -> Issue:
        return self._retry(
            "get issue",
            lambda: self._client.issue(id=issue_key, fields=fields),
        )

    def add_worklog(
        self,
        *,
        issue_key: str,
        time_spent: str,
        comment: str,
    ) -> None:
        def _call() -> Any:
            return self._client.add_worklog(
                issue=issue_key,
                timeSpent=time_spent,
                adjustEstimate="auto",
                comment=comment,
            )

        self._retry("add worklog", _call)


def connect_to_jira(server: Server) -> tuple[JIRA, dict[str, Any]]:
    """Create an authenticated JIRA client for the given server configuration."""

    def _attempt_connection(**auth_kwargs: Any) -> tuple[JIRA, dict[str, Any]]:
        # Set a sensible default timeout so requests don't hang forever
        client = JIRA(server=server.url, timeout=REQUEST_TIMEOUT_SECONDS, **auth_kwargs)
        # Fetch profile to verify credentials; allow a couple of retries for transient faults
        attempts = 3
        last_ex: Exception | None = None
        for i in range(1, attempts + 1):
            try:
                profile = client.myself()
                return client, profile
            except Exception as ex:  # noqa: BLE001
                last_ex = ex
                # Only retry on transient network conditions
                transient = isinstance(
                    ex,
                    (RequestsConnectionError, RequestsReadTimeout, Urllib3ReadTimeout),
                ) or (
                    isinstance(ex, JIRAError)
                    and ex.status_code in {429, 500, 502, 503, 504}
                )
                if not transient or i == attempts:
                    break
                delay = 0.5 * (2 ** (i - 1))
                time.sleep(delay)
        assert last_ex is not None
        raise last_ex

    authenticator = JiraAuthenticator(
        strategies=[PatAuthStrategy(), CloudTokenAuthStrategy()]
    )
    return authenticator.authenticate(server, _attempt_connection)


class IssueSelectionFlow:
    def __init__(
        self,
        *,
        prompt: QuestionaryIO,
        jira_service: JiraService,
        spinner_factory: Callable[..., Halo] = Halo,
    ) -> None:
        self._prompt = prompt
        self._jira_service = jira_service
        self._spinner_factory = spinner_factory

    def select_issue(self, server: Server) -> str:
        selected_issue_key: str | None = None
        while selected_issue_key is None:
            view_choice = self._prompt.select(
                message="How would you like to find issues?",
                choices=self._build_view_choices(server),
            )

            if view_choice == MANUAL_ENTRY_VALUE:
                manual_key = self._prompt_manual_issue_key()
                if manual_key:
                    selected_issue_key = manual_key
                continue

            if view_choice == VIEW_MY_ISSUES:
                my_jql = server.issue_jql or DEFAULT_ISSUE_JQL
                issues = self._fetch_issues_with_jql(my_jql)
                self._print_issue_count(
                    message=f"Loaded {len(issues)} issue(s) assigned to you.",
                    issues=issues,
                )
                chosen_key = self._prompt_issue_selection(
                    issues=issues,
                    prompt_message="Select from your assigned issues",
                )
                if chosen_key:
                    selected_issue_key = chosen_key
                continue

            if view_choice == VIEW_TEAM_ISSUES:
                issues = self._fetch_issues_with_jql(server.team_issue_jql)
                self._print_issue_count(
                    message=f"Loaded {len(issues)} team issue(s).",
                    issues=issues,
                )
                chosen_key = self._prompt_issue_selection(
                    issues=issues,
                    prompt_message="Select shared/team issues",
                )
                if chosen_key:
                    selected_issue_key = chosen_key
                continue

            if view_choice == VIEW_PROJECT_ISSUES:
                jql = self._project_jql(server)
                if not jql:
                    self._prompt.print(
                        "No project keys configured for this server.",
                        style="fg:ansiyellow",
                    )
                    continue
                issues = self._fetch_issues_with_jql(jql)
                self._print_issue_count(
                    message=f"Loaded {len(issues)} project issue(s).",
                    issues=issues,
                )
                chosen_key = self._prompt_issue_selection(
                    issues=issues,
                    prompt_message="Select project issues",
                )
                if chosen_key:
                    selected_issue_key = chosen_key
                continue

            if view_choice == SEARCH_BY_TEXT_VALUE:
                search_term = self._prompt.text(
                    message="Search term to look for in Jira:",
                    instruction="Matches summary and description; include issue key to find it directly.",
                    validate=lambda text: True
                    if len(text.strip()) > 0
                    else "Please enter a value",
                ).strip()
                if not search_term:
                    continue
                keyword_jql = self._build_keyword_search_jql(search_term)
                issues = self._fetch_issues_with_jql(
                    keyword_jql, limit=SEARCH_RESULT_LIMIT
                )
                self._print_issue_count(
                    message=f"Loaded {len(issues)} issue(s) from keyword search.",
                    issues=issues,
                )
                chosen_key = self._prompt_issue_selection(
                    issues=issues,
                    prompt_message="Select issues from keyword search",
                )
                if chosen_key:
                    selected_issue_key = chosen_key
                continue

            if view_choice == SEARCH_BY_JQL_VALUE:
                custom_jql = self._prompt.text(
                    message="Enter the JQL to run:",
                    multiline=True,
                    instruction="Example: project = ABC AND statusCategory != Done",
                    validate=lambda text: True
                    if len(text.strip()) > 0
                    else "Please enter a value",
                ).strip()
                if not custom_jql:
                    continue
                issues = self._fetch_issues_with_jql(custom_jql)
                self._print_issue_count(
                    message=f"Loaded {len(issues)} issue(s) from custom JQL.",
                    issues=issues,
                )
                chosen_key = self._prompt_issue_selection(
                    issues=issues,
                    prompt_message="Select issues from custom JQL",
                )
                if chosen_key:
                    selected_issue_key = chosen_key
                continue

            self._prompt.print(
                "Unsupported selection choice. Please pick another option.",
                style="fg:ansired",
            )

        if selected_issue_key is None:
            self._prompt.print(
                "No issue selected. Exiting.",
                style="fg:ansired",
            )
            sys.exit(1)

        return selected_issue_key

    def _fetch_issues_with_jql(
        self,
        jql_to_run: str,
        *,
        limit: int | None = None,
    ) -> list[Issue]:
        if not jql_to_run:
            return []

        spinner = self._spinner_factory(text="Loading issues...", spinner="pong")
        spinner.start()
        try:
            issues = self._jira_service.search_issues(
                jql_to_run,
                fields=ISSUE_FIELDS,
                limit=limit,
            )
        except (
            JIRAError,
            RequestsReadTimeout,
            RequestsConnectionError,
            Urllib3ReadTimeout,
            RequestException,
        ) as ex:
            self._prompt.print(
                f"Failed to run JQL search: {ex.text if isinstance(ex, JIRAError) else str(ex)}",
                style="fg:ansired",
            )
            return []
        finally:
            spinner.stop()

        return issues

    def _build_keyword_search_jql(self, term: str) -> str:
        escaped_term = term.replace('"', '"')
        clauses = [
            f'summary ~ "{escaped_term}"',
            f'description ~ "{escaped_term}"',
        ]
        normalized_key = term.strip().upper()
        if ISSUE_KEY_PATTERN.match(normalized_key):
            clauses.insert(0, f'key = "{normalized_key}"')
        return " OR ".join(clauses) + " ORDER BY updated DESC"

    def _build_view_choices(
        self, server: Server
    ) -> list[questionary.Choice | questionary.Separator]:
        choices: list[questionary.Choice | questionary.Separator] = [
            questionary.Choice(
                title="My assigned issues",
                description="Issues assigned to you and not Done",
                value=VIEW_MY_ISSUES,
                shortcut_key="m",
            )
        ]
        if server.team_issue_jql:
            choices.append(
                questionary.Choice(
                    title="Shared/team buckets",
                    description="Your configured team JQL",
                    value=VIEW_TEAM_ISSUES,
                    shortcut_key="t",
                )
            )
        if server.project_keys:
            project_list = ", ".join(server.project_keys)
            choices.append(
                questionary.Choice(
                    title="All project tickets",
                    description=f"Issues in projects: {project_list}",
                    value=VIEW_PROJECT_ISSUES,
                    shortcut_key="p",
                )
            )
        choices.append(questionary.Separator())
        choices.append(
            questionary.Choice(
                title="Search Jira by keywords",
                description="Run a quick summary/description search",
                value=SEARCH_BY_TEXT_VALUE,
                shortcut_key="s",
            )
        )
        choices.append(
            questionary.Choice(
                title="Search Jira with custom JQL",
                description="Paste or type any JQL query",
                value=SEARCH_BY_JQL_VALUE,
            )
        )
        choices.append(
            questionary.Choice(
                title="Enter issue key manually",
                value=MANUAL_ENTRY_VALUE,
            )
        )
        return choices

    def _prompt_issue_selection(
        self,
        *,
        issues: list[Issue],
        prompt_message: str,
    ) -> str | None:
        if not issues:
            self._prompt.print(
                "No issues matched that choice.",
                style="fg:ansiyellow",
            )
            return None

        choices: list[questionary.Choice | questionary.Separator] = [
            questionary.Choice(
                title=f"{issue.key} - {issue.fields.summary}",
                description=f"Status: {issue.fields.status}",
                value=issue.key,
            )
            for issue in issues
        ]
        choices.append(questionary.Separator())
        choices.append(
            questionary.Choice(
                title="Back to view selector",
                value=RETURN_TO_VIEWS_VALUE,
                shortcut_key="b",
            )
        )

        selected_value = self._prompt.select(
            message=prompt_message,
            instruction="Use arrows to pick an issue or press 'b' to go back.",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
        )

        if selected_value == RETURN_TO_VIEWS_VALUE:
            return None
        return selected_value

    def _prompt_manual_issue_key(self) -> str | None:
        manual_key = (
            self._prompt.text(
                message="Enter the Jira issue key:",
                instruction="For example: TEAM-123",
                validate=lambda text: True
                if len(text.strip()) > 0
                else "Please enter a value",
            )
            .strip()
            .upper()
        )
        if not manual_key:
            return None
        return manual_key

    def _project_jql(self, server: Server) -> str | None:
        if not server.project_keys:
            return None
        project_list = ", ".join(server.project_keys)
        return f"project in ({project_list}) AND statusCategory not in (Done)"

    def _print_issue_count(self, *, message: str, issues: list[Issue]) -> None:
        self._prompt.print(
            message,
            style="fg:ansigreen" if issues else "fg:ansiyellow",
        )


class WorklogFlow:
    def __init__(
        self,
        *,
        prompt: QuestionaryIO,
        jira_service: JiraService,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
        spinner_factory: Callable[..., Halo] = Halo,
    ) -> None:
        self._prompt = prompt
        self._jira_service = jira_service
        self._clock = clock
        self._spinner_factory = spinner_factory

    def log_time(self, issue_key: str) -> bool:
        log_method = self._prompt_log_method()

        time_spent = "0m"
        comment = ""

        if log_method == RETURN_TO_LOG_METHOD_VALUE:
            self._prompt.print(
                "Returning to issue selection...",
                style="fg:ansiyellow",
            )
            return False

        if log_method == "manual":
            comment = self._prompt.text(
                message="Enter an optional comment for what you've worked on:",
                multiline=True,
            )
            time_spent = self._prompt.text(
                message='How much time did you time spent, e.g. "2d", or "30m"?',
                validate=lambda text: True if len(text) > 0 else "Please enter a value",
            )

        elif log_method == "auto":
            self._prompt.print(
                "Timer started. Leave this terminal open and press Enter when you're done working.",
                style="fg:ansicyan",
            )
            start_time = self._clock()
            spinner = self._spinner_factory(
                text="Tracking time...",
                spinner="dots12",
            )
            spinner.start()
            try:
                input()
            finally:
                spinner.stop()
            stop_time = self._clock()
            seconds_spent = max((stop_time - start_time).total_seconds(), 0)
            minutes_spent = max(int(round(seconds_spent / 60.0)), 1)
            time_spent = f"{minutes_spent}m"
            self._prompt.print(
                f"Timer stopped after approximately {minutes_spent} minute(s).",
                style="fg:ansigreen",
            )
            comment = self._prompt.text(
                message="Enter an optional comment for what you've worked on:",
                multiline=True,
            )

        else:
            raise ValueError(f"Unsupported log method '{log_method}'.")

        happy_with_time = False
        while not happy_with_time:
            happy_with_time = self._prompt.select(
                message=f"We've tracked a total of {time_spent}. Do you want to adjust the time?",
                choices=[
                    questionary.Choice(title=f"No, {time_spent} is fine.", value=True),
                    questionary.Choice(
                        title="Yes, I want to adjust the time spent.", value=False
                    ),
                ],
            )
            if not happy_with_time:
                time_spent = self._prompt.text(
                    message='How much time did you time spent, e.g. "2d", or "30m"?',
                    validate=lambda text: True
                    if len(text) > 0
                    else "Please enter a value",
                    default=time_spent,
                )

        # Attempt to add worklog with automatic retries; on failure, offer manual retry
        while True:
            try:
                self._jira_service.add_worklog(
                    issue_key=issue_key,
                    time_spent=time_spent,
                    comment=comment,
                )
                self._prompt.print(
                    f"Added worklog to issue {issue_key}", style="fg:ansigreen"
                )
                return True
            except (
                JIRAError,
                RequestsReadTimeout,
                RequestsConnectionError,
                Urllib3ReadTimeout,
                RequestException,
            ) as ex:  # noqa: PERF203
                # Give a concise, user-friendly error and option to retry
                error_text = ex.text if isinstance(ex, JIRAError) else str(ex)
                self._prompt.print(
                    f"Failed to add worklog: {error_text}",
                    style="fg:ansired",
                )
                retry = self._prompt.select(
                    message="Do you want to retry submitting the worklog?",
                    choices=[
                        questionary.Choice(title="Yes, retry.", value=True),
                        questionary.Choice(title="No, cancel.", value=False),
                    ],
                )
                if not retry:
                    return False

    def _prompt_log_method(self) -> str:
        return self._prompt.select(
            message="How do you want to log the time?",
            default="auto",
            choices=[
                questionary.Choice(
                    title="Start Timer",
                    description="Begin a timer now and stop it when you're done.",
                    value="auto",
                    shortcut_key="t",
                ),
                questionary.Choice(
                    title="Manual Time Entry",
                    description='Enter a duration such as "1h" or "30m".',
                    value="manual",
                    shortcut_key="m",
                ),
                questionary.Choice(
                    title="Back to issue selection",
                    description="Return and pick a different issue.",
                    value=RETURN_TO_LOG_METHOD_VALUE,
                    shortcut_key="b",
                ),
            ],
            instruction="Use arrows to choose or press 'b' to go back.",
        )


def _select_server(
    config: "Config",
    prompt: QuestionaryIO,
    prompter: ServerPrompter,
) -> Server:
    current_server: Server | None = None
    while current_server is None:
        server_choices = [
            questionary.Choice(title=f"{s.name} - {s.url}", value=s)
            for s in config.servers
        ]
        server_choices.append(questionary.Separator())
        server_choices.append(
            questionary.Choice(title="Add a new server", value="add_new_server")
        )
        selection = prompt.select(
            message="Please select a server to work with",
            choices=server_choices,
        )
        if selection == "add_new_server":
            add_new_server(config, prompter)
            continue
        current_server = selection
    return current_server


def main(
    args: dict[str, str] | None = None,
    server: Server | None = None,
    jira: JIRA | None = None,
    myself: dict[str, Any] | None = None,
    *,
    prompt: QuestionaryIO | None = None,
    clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    spinner_factory: Callable[..., Halo] = Halo,
) -> None:
    """The main program"""
    prompt = prompt or QuestionaryIO()
    server_prompter = ServerPrompter(prompt)
    config = Config()
    config.load()

    if len(config.servers) == 0:
        add_new_server(config, server_prompter)

    active_server = server or _select_server(config, prompt, server_prompter)
    assert active_server is not None

    jira_client = jira
    profile = myself

    if jira_client is None:
        try:
            jira_client, profile = connect_to_jira(active_server)
        except JIRAError as ex:
            if ex.status_code == 401:
                prompt.print(
                    "Authentication failed. Please verify your credentials or reconfigure the server.",
                    style="fg:ansired",
                )
                sys.exit(1)
            raise

    jira_service = JiraService(jira_client)

    if profile is None:
        try:
            profile = jira_service.myself()
        except JIRAError as ex:
            if ex.status_code == 401:
                jira_client, profile = connect_to_jira(active_server)
                jira_service = JiraService(jira_client)
            else:
                raise
        logging.debug(
            "You're authenticated with JIRA (%s) as: %s - %s (%s)",
            active_server.url,
            profile["name"],
            profile["displayName"],
            profile["emailAddress"],
        )

    issue_flow = IssueSelectionFlow(
        prompt=prompt,
        jira_service=jira_service,
        spinner_factory=spinner_factory,
    )
    worklog_flow = WorklogFlow(
        prompt=prompt,
        jira_service=jira_service,
        clock=clock,
        spinner_factory=spinner_factory,
    )

    worklog_created = False
    while not worklog_created:
        issue_key = issue_flow.select_issue(active_server)

        try:
            jira_service.get_issue(issue_key, fields=["id", "key"])
        except (
            JIRAError,
            RequestsReadTimeout,
            RequestsConnectionError,
            Urllib3ReadTimeout,
            RequestException,
        ) as ex:
            text = ex.text if isinstance(ex, JIRAError) else str(ex)
            prompt.print(
                f"Failed to confirm issue '{issue_key}': {text}", style="fg:ansired"
            )
            retry_issue = prompt.select(
                message="Try to confirm the issue again?",
                choices=[
                    questionary.Choice(title="Yes, retry.", value=True),
                    questionary.Choice(title="No, go back to selection.", value=False),
                ],
            )
            if not retry_issue:
                continue
            # Retry once more via loop by not setting worklog_created
            continue
        logging.debug("Selected issue exists")

        worklog_created = worklog_flow.log_time(issue_key)

    _continue = prompt.select(
        message="Work on another ticket?",
        choices=[
            questionary.Choice(title="Yes.", value=True),
            questionary.Choice(title="No.", value=False),
        ],
    )

    if _continue:
        main(
            args,
            server=active_server,
            jira=jira_client,
            myself=profile,
            prompt=prompt,
            clock=clock,
            spinner_factory=spinner_factory,
        )


def cli(args: dict[str, str] | None = None) -> None:
    prompt = QuestionaryIO()
    try:
        main(args, prompt=prompt)
        prompt.print(text="Thank you for using this tool.")
    except KeyboardInterrupt:
        prompt.print("Cancelled by user. Exiting.")
        sys.exit(1)

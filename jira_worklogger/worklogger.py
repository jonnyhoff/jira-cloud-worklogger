#!/bin/env python3

import configparser
import dataclasses
import datetime
import logging
import logging.config
import pathlib
import re
import sys
from collections.abc import Callable
from dataclasses import field
from typing import Any

import questionary
from halo import Halo
from jira import JIRA
from jira.client import ResultList
from jira.exceptions import JIRAError
from jira.resources import Issue

logging.basicConfig(level=logging.INFO)


DEFAULT_ISSUE_JQL = "assignee=currentUser() AND statusCategory not in (Done)"
DEFAULT_TEAM_ISSUE_JQL = ""  # Optional, user can configure later
SEARCH_RESULT_LIMIT = 50
SEARCH_BY_TEXT_VALUE = "__search_by_text__"
SEARCH_BY_JQL_VALUE = "__search_by_jql__"
MANUAL_ENTRY_VALUE = "__manual_entry__"
RETURN_TO_VIEWS_VALUE = "__return_to_views__"
VIEW_MY_ISSUES = "__view_my_issues__"
VIEW_TEAM_ISSUES = "__view_team_issues__"
VIEW_PROJECT_ISSUES = "__view_project_issues__"
VIEW_REVIEW_SELECTION = "__view_review_selection__"
VIEW_FINISH_SELECTION = "__view_finish_selection__"


@dataclasses.dataclass(frozen=True)
class SelectionResult:
    back_to_view_selector: bool
    selection_changed: bool


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


def validate_server_name(c: Config) -> Callable[[str], str | bool]:
    """Returns a function that can be used to validate a server name.

    Args:
        c (Config): The configuration can be used to check if a server name is already taken.

    Returns:
        Callable[[str], str|bool]: A function to be used in the validate argument of a questionary text.
    """

    def validate(name: str) -> str | bool:
        if len(name) <= 0:
            return "Please, enter a name for the server!"
        if c._parser.has_section(name):
            return "Name is already taken, please choose another one!"
        return True

    return validate


def add_new_server_questions(c: Config) -> Server:
    url = (
        questionary.text(
            message="Which JIRA server to connect to?",
            default="https://your-instance.atlassian.net",
            validate=lambda text: (
                True if len(text) > 0 else "Please, enter a JIRA server"
            ),
        )
        .unsafe_ask()
        .strip()
    )
    auth_type = questionary.select(
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
    ).unsafe_ask()
    name = (
        questionary.text(
            message="What name to give your server?",
            default="Red Hat",
            validate=validate_server_name(c),
        )
        .unsafe_ask()
        .strip()
    )

    issue_jql = (
        questionary.text(
            message="Which JQL should be used to list issues by default?",
            default=DEFAULT_ISSUE_JQL,
        )
        .unsafe_ask()
        .strip()
    )
    if not issue_jql:
        issue_jql = DEFAULT_ISSUE_JQL

    team_issue_jql = (
        questionary.text(
            message="Optional JQL for shared/team buckets (leave blank to skip):",
            default=DEFAULT_TEAM_ISSUE_JQL,
        )
        .unsafe_ask()
        .strip()
    )

    project_keys_input = (
        questionary.text(
            message="Optional Jira project keys for broader searches (comma separated):",
            default="",
        )
        .unsafe_ask()
        .strip()
    )
    project_keys = [key.strip() for key in project_keys_input.split(",") if key.strip()]

    if auth_type == "pat":
        # For a new PAT go to:
        # https://issues.redhat.com/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens
        pat = (
            questionary.password(
                message="What is your JIRA Personal Access Token (PAT)?",
                validate=lambda text: True if len(text) > 0 else "Please enter a value",
            )
            .unsafe_ask()
            .strip()
        )
        questionary.print(
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

    email = (
        questionary.text(
            message="What is your Atlassian account email?",
            validate=lambda text: True if len(text) > 0 else "Please enter a value",
        )
        .unsafe_ask()
        .strip()
    )
    api_token = (
        questionary.password(
            message="What is your Jira Cloud API token?",
            instruction="Create one at https://id.atlassian.com/manage-profile/security/api-tokens",
            validate=lambda text: True if len(text) > 0 else "Please enter a value",
        )
        .unsafe_ask()
        .strip()
    )
    questionary.print(
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


def add_new_server(c: Config) -> None:
    """Asks a few questions to add a new server configuration to the config"""
    s = add_new_server_questions(c)
    c.add_server(s)


def connect_to_jira(server: Server) -> tuple[JIRA, dict[str, Any]]:
    """Create an authenticated JIRA client for the given server configuration."""

    def _attempt_connection(**auth_kwargs: Any) -> tuple[JIRA, dict[str, Any]]:
        client = JIRA(server=server.url, **auth_kwargs)
        profile = client.myself()
        return client, profile

    if server.auth_type == "pat":
        return _attempt_connection(token_auth=server.pat)

    if server.auth_type == "cloud_token":
        errors: list[JIRAError] = []
        auth_attempts: list[tuple[str, dict[str, Any]]] = []
        if server.email and server.api_token:
            auth_attempts.append(
                (
                    "email+api_token",
                    {"basic_auth": (server.email, server.api_token)},
                )
            )
        if server.api_token:
            auth_attempts.append(("bearer", {"token_auth": server.api_token}))

        for method, kwargs in auth_attempts:
            try:
                return _attempt_connection(**kwargs)
            except JIRAError as ex:
                if ex.status_code == 401:
                    logging.debug(
                        "Authentication method '%s' failed for server '%s': %s",
                        method,
                        server.name,
                        ex.text,
                    )
                    errors.append(ex)
                    continue
                raise

        if errors:
            raise errors[-1]

    raise ValueError(
        f"Unsupported authentication type '{server.auth_type}' for server '{server.name}'."
    )


def main(
    args: dict[str, str] | None = None,
    server: Server | None = None,
    jira: JIRA | None = None,
    myself: dict[str, Any] | None = None,
) -> None:
    """The main program"""
    config = Config()
    config.load()

    if len(config.servers) == 0:
        add_new_server(config)

    while server is None:
        server_choices = [
            questionary.Choice(title=f"{s.name} - {s.url}", value=s)
            for s in config.servers
        ]
        server_choices.append(questionary.Separator())
        server_choices.append(
            questionary.Choice(title="Add a new server", value="add_new_server")
        )
        s = questionary.select(
            message="Please select a server to work with", choices=server_choices
        ).unsafe_ask()
        if s == "add_new_server":
            add_new_server(config)
        else:
            server = s

    assert server is not None

    # Some Authentication Methods
    if jira == None:
        try:
            jira, myself = connect_to_jira(server)
        except JIRAError as ex:
            if ex.status_code == 401:
                questionary.print(
                    "Authentication failed. Please verify your credentials or reconfigure the server.",
                    style="fg:ansired",
                )
                sys.exit(1)
            raise

    # Who has authenticated
    if myself == None:
        try:
            myself = jira.myself()
        except JIRAError as ex:
            if ex.status_code == 401:
                jira, myself = connect_to_jira(server)
            else:
                raise
        logging.debug(
            f"You're authenticated with JIRA ({server.url}) as: {myself['name']} - {myself['displayName']} ({myself['emailAddress']})"
        )

    # Which field to pull from the JIRA server for each issue
    pull_issue_fields = [
        "id",
        "key",
        "summary",
        "statusCategory",
        "status",
        "description",
    ]

    issue_cache: dict[str, Issue] = {}
    issue_key_pattern = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")

    def fetch_issues_with_jql(
        jql_to_run: str,
        *,
        limit: int | None = None,
    ) -> list[Issue]:
        logging.debug("Searching Jira with JQL: %s", jql_to_run)
        search_kwargs: dict[str, Any] = {
            "jql_str": jql_to_run,
            "fields": pull_issue_fields,
        }
        if limit is None:
            search_kwargs["maxResults"] = False
        else:
            search_kwargs["maxResults"] = limit

        spinner = Halo(text="Loading issues...", spinner="pong")
        spinner.start()
        try:
            search_results: ResultList[Issue] = jira.search_issues(**search_kwargs)
        except JIRAError as ex:
            questionary.print(
                f"Failed to run JQL search: {ex.text}",
                style="fg:ansired",
            )
            return []
        finally:
            spinner.stop()

        issues = list(search_results)
        for issue in issues:
            issue_cache[issue.key] = issue
        return issues

    def build_keyword_search_jql(term: str) -> str:
        escaped_term = term.replace('"', '\\"')
        clauses = [
            f'summary ~ "{escaped_term}"',
            f'description ~ "{escaped_term}"',
        ]
        normalized_key = term.strip().upper()
        if issue_key_pattern.match(normalized_key):
            clauses.insert(0, f'key = "{normalized_key}"')
        return " OR ".join(clauses) + " ORDER BY updated DESC"

    def issue_choices_for_view(
        issues: list[Issue],
    ) -> list[questionary.Choice | questionary.Separator]:
        choices: list[questionary.Choice | questionary.Separator] = [
            questionary.Choice(
                title=f"{issue.key} - {issue.fields.summary}",
                description=f"Status: {issue.fields.status}",
                value=issue.key,
                checked=issue.key in selected_issue_set,
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
        return choices

    selected_issue_keys: list[str] = []
    selected_issue_set: set[str] = set()

    def sync_selection_from_view(view_keys: list[str], chosen_keys: list[str]) -> None:
        nonlocal selected_issue_keys, selected_issue_set
        for key in chosen_keys:
            if key not in selected_issue_set:
                selected_issue_keys.append(key)
                selected_issue_set.add(key)
        for key in view_keys:
            if key not in chosen_keys and key in selected_issue_set:
                selected_issue_set.remove(key)
        selected_issue_keys = [
            key for key in selected_issue_keys if key in selected_issue_set
        ]

    def build_view_choices() -> list[questionary.Choice | questionary.Separator]:
        choices: list[questionary.Choice | questionary.Separator] = []
        choices.append(
            questionary.Choice(
                title="My assigned issues",
                description="Issues assigned to you and not Done",
                value=VIEW_MY_ISSUES,
                shortcut_key="m",
            )
        )
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
        choices.append(questionary.Separator())
        if selected_issue_keys:
            choices.append(
                questionary.Choice(
                    title=f"Review current selection ({len(selected_issue_keys)} selected)",
                    value=VIEW_REVIEW_SELECTION,
                )
            )
            choices.append(
                questionary.Choice(
                    title="Done selecting issues",
                    value=VIEW_FINISH_SELECTION,
                    shortcut_key="d",
                )
            )
        else:
            choices.append(
                questionary.Choice(
                    title="Review current selection",
                    disabled="No issues selected yet",
                )
            )
            choices.append(
                questionary.Choice(
                    title="Done selecting issues",
                    disabled="Select at least one issue",
                )
            )
        return choices

    def prompt_and_sync_from_issues(
        *,
        issues: list[Issue],
        prompt_message: str,
    ) -> SelectionResult:
        if not issues:
            questionary.print(
                "No issues matched that choice.",
                style="fg:ansiyellow",
            )
            return SelectionResult(
                back_to_view_selector=False,
                selection_changed=False,
            )

        for issue in issues:
            issue_cache[issue.key] = issue

        previous_selection = set(selected_issue_set)
        choices = issue_choices_for_view(issues)
        selected_values = questionary.checkbox(
            message=prompt_message,
            instruction="Use arrows to select issues, space to toggle, or choose 'Back to view selector'.",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).unsafe_ask()

        if RETURN_TO_VIEWS_VALUE in selected_values:
            return SelectionResult(
                back_to_view_selector=True,
                selection_changed=False,
            )

        selected_values = [
            value for value in selected_values if value != RETURN_TO_VIEWS_VALUE
        ]

        sync_selection_from_view([issue.key for issue in issues], selected_values)
        selection_changed = previous_selection != selected_issue_set
        return SelectionResult(
            back_to_view_selector=False,
            selection_changed=selection_changed,
        )

    def prompt_manual_issue_key() -> bool:
        previous_selection = set(selected_issue_set)
        manual_key = (
            questionary.text(
                message="Enter the Jira issue key:",
                instruction="For example: TEAM-123",
                validate=lambda text: True
                if len(text.strip()) > 0
                else "Please enter a value",
            )
            .unsafe_ask()
            .strip()
            .upper()
        )
        if not manual_key:
            return False
        if manual_key in selected_issue_set:
            questionary.print(
                f"Issue {manual_key} is already selected.",
                style="fg:ansiyellow",
            )
            return False
        selected_issue_set.add(manual_key)
        selected_issue_keys.append(manual_key)
        return previous_selection != selected_issue_set

    def review_current_selection() -> bool:
        if not selected_issue_keys:
            questionary.print("No issues selected yet.", style="fg:ansiyellow")
            return False

        previous_selection = set(selected_issue_set)
        review_choices = [
            questionary.Choice(title=key, value=key, checked=True)
            for key in selected_issue_keys
        ]
        updated_selection = questionary.checkbox(
            message="Review selected issues",
            instruction="Uncheck any issues you want to remove.",
            choices=review_choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).unsafe_ask()

        updated_set = set(updated_selection)
        nonlocal_selected = [key for key in selected_issue_keys if key in updated_set]
        selected_issue_keys.clear()
        selected_issue_keys.extend(nonlocal_selected)
        selected_issue_set.clear()
        selected_issue_set.update(updated_set)
        return previous_selection != selected_issue_set

    def project_jql() -> str | None:
        if not server.project_keys:
            return None
        project_list = ", ".join(server.project_keys)
        return f"project in ({project_list}) AND statusCategory not in (Done)"

    def maybe_prompt_to_start_logging() -> bool:
        if not selected_issue_keys:
            return False

        next_action = questionary.select(
            message=f"{len(selected_issue_keys)} issue(s) selected. What next?",
            choices=[
                questionary.Choice(
                    title="Log time now",
                    description="Jump straight to timer/manual entry options.",
                    value="log",
                    shortcut_key="l",
                ),
                questionary.Choice(
                    title="Keep selecting issues",
                    value="keep",
                    shortcut_key="k",
                ),
            ],
        ).unsafe_ask()
        return next_action == "log"

    proceed_to_logging = False
    while not proceed_to_logging:
        view_choice = questionary.select(
            message="How would you like to find issues?",
            choices=build_view_choices(),
        ).unsafe_ask()

        if view_choice == VIEW_FINISH_SELECTION:
            proceed_to_logging = True
            break

        if view_choice == VIEW_REVIEW_SELECTION:
            if review_current_selection() and maybe_prompt_to_start_logging():
                proceed_to_logging = True
                break
            continue

        if view_choice == MANUAL_ENTRY_VALUE:
            if prompt_manual_issue_key() and maybe_prompt_to_start_logging():
                proceed_to_logging = True
                break
            continue

        if view_choice == VIEW_MY_ISSUES:
            my_jql = server.issue_jql or DEFAULT_ISSUE_JQL
            issues = fetch_issues_with_jql(my_jql)
            questionary.print(
                f"Loaded {len(issues)} issue(s) assigned to you.",
                style="fg:ansigreen" if issues else "fg:ansiyellow",
            )
            result = prompt_and_sync_from_issues(
                issues=issues,
                prompt_message="Select from your assigned issues",
            )
            if result.back_to_view_selector:
                continue
            if result.selection_changed and maybe_prompt_to_start_logging():
                proceed_to_logging = True
                break
            continue

        if view_choice == VIEW_TEAM_ISSUES:
            issues = fetch_issues_with_jql(server.team_issue_jql)
            questionary.print(
                f"Loaded {len(issues)} team issue(s).",
                style="fg:ansigreen" if issues else "fg:ansiyellow",
            )
            result = prompt_and_sync_from_issues(
                issues=issues,
                prompt_message="Select shared/team issues",
            )
            if result.back_to_view_selector:
                continue
            if result.selection_changed and maybe_prompt_to_start_logging():
                proceed_to_logging = True
                break
            continue

        if view_choice == VIEW_PROJECT_ISSUES:
            jql = project_jql()
            if not jql:
                questionary.print(
                    "No project keys configured for this server.",
                    style="fg:ansiyellow",
                )
                continue
            issues = fetch_issues_with_jql(jql)
            questionary.print(
                f"Loaded {len(issues)} project issue(s).",
                style="fg:ansigreen" if issues else "fg:ansiyellow",
            )
            result = prompt_and_sync_from_issues(
                issues=issues,
                prompt_message="Select project issues",
            )
            if result.back_to_view_selector:
                continue
            if result.selection_changed and maybe_prompt_to_start_logging():
                proceed_to_logging = True
                break
            continue

        if view_choice == SEARCH_BY_TEXT_VALUE:
            search_term = (
                questionary.text(
                    message="Search term to look for in Jira:",
                    instruction="Matches summary and description; include issue key to find it directly.",
                    validate=lambda text: True
                    if len(text.strip()) > 0
                    else "Please enter a value",
                )
                .unsafe_ask()
                .strip()
            )
            if not search_term:
                continue
            keyword_jql = build_keyword_search_jql(search_term)
            issues = fetch_issues_with_jql(keyword_jql, limit=SEARCH_RESULT_LIMIT)
            questionary.print(
                f"Loaded {len(issues)} issue(s) from keyword search.",
                style="fg:ansigreen" if issues else "fg:ansiyellow",
            )
            result = prompt_and_sync_from_issues(
                issues=issues,
                prompt_message="Select issues from keyword search",
            )
            if result.back_to_view_selector:
                continue
            if result.selection_changed and maybe_prompt_to_start_logging():
                proceed_to_logging = True
                break
            continue

        if view_choice == SEARCH_BY_JQL_VALUE:
            custom_jql = (
                questionary.text(
                    message="Enter the JQL to run:",
                    multiline=True,
                    instruction="Example: project = ABC AND statusCategory != Done",
                    validate=lambda text: True
                    if len(text.strip()) > 0
                    else "Please enter a value",
                )
                .unsafe_ask()
                .strip()
            )
            if not custom_jql:
                continue
            issues = fetch_issues_with_jql(custom_jql)
            questionary.print(
                f"Loaded {len(issues)} issue(s) from custom JQL.",
                style="fg:ansigreen" if issues else "fg:ansiyellow",
            )
            result = prompt_and_sync_from_issues(
                issues=issues,
                prompt_message="Select issues from custom JQL",
            )
            if result.back_to_view_selector:
                continue
            if result.selection_changed and maybe_prompt_to_start_logging():
                proceed_to_logging = True
                break
            continue

        questionary.print(
            "Unsupported selection choice. Please pick another option.",
            style="fg:ansired",
        )

    if not selected_issue_keys:
        questionary.print(
            "No issues selected. Exiting.",
            style="fg:ansired",
        )
        sys.exit(1)

    issue_keys = selected_issue_keys

    # Load selected issues to ensure they all exist
    # TODO(kwk): Can we do this in parallel somehow?
    try:
        for issue_key in issue_keys:
            logging.debug(f"Loading issue {issue_key}")
            jira.issue(id=issue_key, fields=["id", "key"])
    except JIRAError as ex:
        questionary.print(f"Failed to find issue with key '{issue_keys}': {ex.text}")
        questionary.print("Please run the tool again and verify your selections.")
        sys.exit(1)
    logging.debug("All issues exist")

    log_method = questionary.select(
        message="How do you want to log the time?",
        default="auto",
        choices=[
            questionary.Choice(
                title="Automatically (with a timer)",
                description="We will start a timer so you can start working and later come back.",
                value="auto",
                shortcut_key="a",
            ),
            questionary.Choice(
                title="Manually",
                description='You can enter something like "1h" or "2w".',
                value="manual",
                shortcut_key="m",
            ),
        ],
    ).unsafe_ask()

    time_spent: str = "0m"

    comment: str = ""

    if log_method == "manual":
        comment = questionary.text(
            message="Enter an optional comment for what you've worked on:",
            multiline=True,
        ).unsafe_ask()
        time_spent = questionary.text(
            message='How much time did you time spent, e.g. "2d", or "30m"?',
            validate=lambda text: True if len(text) > 0 else "Please enter a value",
        ).unsafe_ask()

    if log_method == "auto":
        questionary.press_any_key_to_continue(
            message="Press any key to START the timer and begin logging your work..."
        ).unsafe_ask()
        start_time = datetime.datetime.now()
        questionary.print(
            "Timer running. Leave this terminal open and press Enter when you're done working.",
            style="fg:ansicyan",
        )
        spinner = Halo(
            text="Tracking time...",
            spinner="dots12",
        )
        spinner.start()
        try:
            input()
        finally:
            spinner.stop()
        stop_time = datetime.datetime.now()
        seconds_spent = max((stop_time - start_time).total_seconds(), 0)
        minutes_spent = max(int(round(seconds_spent / 60.0)), 1)
        time_spent = f"{minutes_spent}m"
        questionary.print(
            f"Timer stopped after approximately {minutes_spent} minute(s).",
            style="fg:ansigreen",
        )
        comment = questionary.text(
            message="Enter an optional comment for what you've worked on:",
            multiline=True,
        ).unsafe_ask()

    happy_with_time = False
    while not happy_with_time:
        happy_with_time = questionary.select(
            message=f"We've tracked a total of {time_spent}. Do you want to adjust the time?",
            choices=[
                questionary.Choice(title=f"No, {time_spent} is fine.", value=True),
                questionary.Choice(
                    title=f"Yes, I want to adjust the time spent.", value=False
                ),
            ],
        ).unsafe_ask()
        if not happy_with_time:
            time_spent = questionary.text(
                message='How much time did you time spent, e.g. "2d", or "30m"?',
                validate=lambda text: True if len(text) > 0 else "Please enter a value",
                default=time_spent,
            ).unsafe_ask()

    # Finally update the worklog of all issues
    for issue_key in issue_keys:
        logging.debug(f"Adding worklog for f{issue_key}.")
        jira.add_worklog(
            issue=issue_key,
            timeSpent=time_spent,
            adjustEstimate="auto",
            comment=comment,
        )
        questionary.print(f"Added worklog to issue {issue_key}")

    _continue = questionary.select(
        message=f"Work on another ticket?",
        choices=[
            questionary.Choice(title=f"Yes.", value=True),
            questionary.Choice(title=f"No.", value=False),
        ],
    ).unsafe_ask()

    if _continue:
        main(args, server=server, jira=jira, myself=myself)


def cli(args: dict[str, str] | None = None) -> None:
    try:
        main(args)
        questionary.print(text="Thank you for using this tool.")
    except KeyboardInterrupt:
        questionary.print("Cancelled by user. Exiting.")
        sys.exit(1)

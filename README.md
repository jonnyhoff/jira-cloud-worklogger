jira-time
=========

`jira-time` is an interactive command-line tool for logging work against Jira issues without touching the web UI. Start a timer, enter manual durations, and search across your projects from one resilient CLI.

Features
--------

- Interactive prompts for adding Jira Cloud or Server/Data Center instances.
- Timer-based and manual time logging with optional comments.
- Fast issue discovery via saved JQL, team/project views, keyword search, or direct key entry.
- Automatic retries with sensible HTTP timeouts so transient Jira or network failures do not crash the session.
- Stores configuration under `~/.config/jira-time/` so repeated runs pick up your saved servers and preferences.

Requirements
------------

- Python 3.10 or newer
- A Jira Cloud API token or Jira Server/Data Center personal access token

Installation
------------

`jira-time` is published on PyPI. Install it with your preferred Python tool:

```console
# Via pipx (recommended for standalone CLIs)
$ pipx install jira-time

# Or install into an existing virtual environment
$ pip install jira-time
```

After installation the `jira-time` command is available on your `PATH`.

Quick Start
-----------

1. Run `jira-time` from your terminal.
2. On first launch you will be prompted to add a Jira server:
   - Supply the base URL, e.g. `https://your-instance.atlassian.net`.
   - Choose **Jira Cloud - Email and API token** to authenticate with Atlassian cloud credentials, or **Jira Server / Data Center - Personal Access Token** for self-hosted instances.
   - Enter an optional friendly name, default issue JQL, shared/team JQL, and a comma-separated list of project keys used for broader searches.
3. Provide the requested credentials:
   - Cloud: Atlassian email address and API token (create/manage at <https://id.atlassian.com/manage-profile/security/api-tokens>).
   - Server/DC: Personal Access Token (PAT) generated from `https://<YOUR_JIRA_SERVER>/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens`.
4. `jira-time` verifies the credentials and writes them to `~/.config/jira-time/jira-time.conf`. Subsequent runs reuse the same server selection.

> **Security note:** Credentials are stored unencrypted in the config file. Ensure your local user account is trusted and the directory permissions are locked down appropriately.

Using the CLI
-------------

### Selecting issues

Each session starts by choosing how you would like to find tickets. Depending on your configuration you will see options such as:

- `My assigned issues` - uses the per-server `issue_jql` (defaults to `assignee=currentUser() AND statusCategory not in (Done)`).
- `Shared/team buckets` - shown when `team_issue_jql` is configured.
- `All project tickets` - lists issues from the configured `project_keys` and filters out Done work.
- `Search Jira by keywords` - runs a fuzzy search across summary and description; include a key like `TEAM-123` for an exact match.
- `Search Jira with custom JQL` - paste any JQL query and browse the results.
- `Enter issue key manually` - type a known issue key if you already have it.

While results load, a spinner appears so you can see progress. Inside any list you may press `b` to return to the view selector.

You can edit saved views later by modifying `~/.config/jira-time/jira-time.conf`:

```
[my-server]
url = https://example.atlassian.net
auth_type = cloud_token
email = alice@example.com
api_token = <redacted>
issue_jql = assignee = currentUser() AND statusCategory not in (Done)
team_issue_jql = project = MEET AND type = Task
project_keys = PROJ, OPS, MEET
```

Leave `team_issue_jql` or `project_keys` blank to hide those menu entries.

### Logging time

Once you pick an issue you can either:

- **Start Timer** - keep the terminal open while you work. Press Enter to stop the timer, review the tracked duration, and add an optional comment.
- **Manual Time Entry** - type a duration such as `30m`, `1h`, or `2d` and optionally describe the work performed.

Before submitting, `jira-time` lets you adjust the duration. Worklog API calls are automatically retried when Jira responds with transient errors (timeouts, HTTP 429/5xx, etc.). If retries still fail you can confirm whether to try again manually.

### Resilience and timeouts

All outbound Jira requests use a three second timeout and exponential backoff. Transient failures are retried up to three times before the tool asks how you would like to proceed. This keeps long-running sessions responsive and avoids crashing with obscure tracebacks.

Demos
-----

Automatic time logging in action:

![](docs/screencapture/automatic-time-logging.gif)

Manual entry flow:

![](docs/screencapture/manual-time-logging.gif)

Advanced multi-issue workflow, including integration with [did](https://github.com/psss/did):

![](docs/screencapture/jira-worklogger-and-did.gif)

Resulting Jira worklogs:

![](docs/screencapture/jira-result.png)

Running from source
-------------------

Clone the repository and install dependencies with Poetry:

```console
$ git clone https://github.com/jonnyhoff/jira-time
$ cd jira-time
$ poetry install
```

Launch the CLI from the virtual environment:

```console
$ poetry run jira-time
```

Development
-----------

- The project uses [Poetry](https://python-poetry.org/) for dependency management and packaging.
- Demo assets are recorded with [asciinema](https://github.com/asciinema/asciinema) and rendered using [agg](https://github.com/asciinema/agg).
- Contributions and bug reports are welcome via GitHub issues and pull requests.

License
-------

Distributed under the MIT License. See `LICENSE` for details.

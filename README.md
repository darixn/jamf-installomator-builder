# Jamf Installomator Builder


A local web app that automates the creation of Jamf Pro policies for [Installomator](https://github.com/Installomator/Installomator)-backed app deployments. Select your labels, configure behavior, click build — the tool creates Smart Groups, Self Service policies, and Auto-Update policies via the Jamf API.

## What It Creates

For each selected Installomator label, three Jamf objects are created:

| Object | Default Name | Purpose |
|---|---|---|
| Smart Group | `{AppName} Installed` | Scopes auto-updates to machines that have the app |
| Self Service Policy | `Install {AppName}` | Lets users install the app from Self Service |
| Auto-Update Policy | `Auto-Update {AppName}` | Runs daily on check-in for machines in the Smart Group |

All three are idempotent — re-running with the same labels skips objects that already exist.

## Requirements

- Python 3.9+
- A Jamf Pro instance with API Client Credentials (OAuth2)

### Jamf API Permissions

Create an API Client in Jamf Pro with these minimum privileges:

| Privilege | Used For |
|---|---|
| Read Scripts | Check if Installomator script exists |
| Create Scripts | Upload Installomator script if missing |
| Read Smart Computer Groups | Check for existing groups |
| Create Smart Computer Groups | Create scoping groups |
| Read Policies | Check for existing policies |
| Create Policies | Create Self Service + Auto-Update policies |
| Update Policies | Attach icons to policies |

## Setup

```bash
# Clone the repo
git clone https://github.com/yourusername/jamf-installomator-builder.git
cd jamf-installomator-builder

# Install dependencies
pip3 install -r requirements.txt

# Run
python3 server.py
```

The app opens automatically at `http://localhost:5001`.

### Options

```
python3 server.py                # normal run
python3 server.py --debug        # dry-run mode, no Jamf API calls
python3 server.py --port 8080    # custom port
```

## Usage

1. **Jamf Connection** — enter your Jamf Pro URL, Client ID, and Client Secret. Click "Test Connection" to verify.
2. **Installomator Source** — choose where to pull labels and the script from (see [Sources](#installomator-sources)).
3. **Select Apps** — search and check off the labels you want. Use "Select Visible" after filtering to bulk-select.
4. **Behavior Settings** — configure Installomator parameters (notifications, blocking process action, etc.). Use presets for common configurations. Settings apply uniformly to all selected labels.
5. **Icons** *(optional)* — point to a folder of PNG files named after each label (e.g., `googlechrome.png`).
6. **Preview** — click Preview to see exactly what will be created before committing.
7. **Build** — click Build Policies. Progress streams in real time. Results show created/skipped/failed counts with links to each object in Jamf.

## Installomator Sources

| Source | Description |
|---|---|
| **Official** | Pulls labels and script from `Installomator/Installomator` on GitHub (default) |
| **Custom Fork** | Your org's fork — specify `owner/repo` and branch |
| **Local** | A local clone or standalone `Installomator.sh` file |

The tool fetches the actual Installomator script from your chosen source and uploads it to Jamf. This means fork customizations and local modifications are fully supported.

## Changing the Naming Convention

The default names for created objects are:

```
Smart Group:          {AppName} Installed
Self Service Policy:  Install {AppName}
Auto-Update Policy:   Auto-Update {AppName}
```

To change these, edit `jamf_api.py`:

**Smart Group name** — line 146:
```python
group_name = f"{display_name} Installed"
```

**Self Service policy name** — line 201:
```python
policy_name = f"Install {app_name}"
```

**Auto-Update policy name** — line 258:
```python
policy_name = f"Auto-Update {app_name}"
```

For example, to prefix everything with your org name:

```python
# Smart Group
group_name = f"ACME - {display_name} Installed"

# Self Service
policy_name = f"ACME - Install {app_name}"

# Auto-Update
policy_name = f"ACME - Auto-Update {app_name}"
```

Or to use a different naming pattern entirely:

```python
# Smart Group
group_name = f"SG - {display_name}"

# Self Service
policy_name = f"[Self Service] {app_name}"

# Auto-Update
policy_name = f"[Auto-Update] {app_name}"
```

The `{display_name}` / `{app_name}` variable is the human-readable name resolved from the Installomator fragment (e.g., "Google Chrome", "Microsoft Teams").

> **Note:** The naming convention is also used for idempotency checks. If you change the pattern after a build, the tool won't recognize previously created objects and will attempt to create duplicates. Rename or delete the old objects in Jamf first.

## Behavior Settings

These map to Installomator environment variables passed as Jamf script parameters:

| Parameter | Description | Options |
|---|---|---|
| `BLOCKING_PROCESS_ACTION` | What to do when the app is open during install | `prompt_user`, `prompt_user_then_kill`, `kill`, `prompt_user_loop`, `silent_fail`, `tell_user`, `tell_user_then_kill` |
| `NOTIFY` | macOS notification behavior | `all`, `success`, `silent` |
| `REOPEN` | Relaunch the app after updating | `yes`, `no` |
| `IGNORE_APP_STORE_APPS` | Skip apps managed by the Mac App Store | `yes`, `no` |
| `INSTALL` | Force reinstall even if up to date | *(empty)* = smart update, `force` = always reinstall |

### Presets

| Preset | Self Service | Auto-Update |
|---|---|---|
| **Recommended** | Prompt user, notify all | Prompt then kill, silent |
| **Silent Background** | Silent fail, no notifications | Prompt then kill, silent |
| **Aggressive Updates** | Force kill, force install | Force kill, force install |

## Features

- Dark mode toggle (persisted across sessions)
- Search with match count and "Selected Only" filter
- Behavior presets for common configurations
- Tooltips on every behavior option
- Preview table before building
- Session persistence via localStorage (never stores credentials)
- Retry failed items
- Clickable "Open in Jamf" links for created objects
- Export build log as `.txt` file
- Support for official Installomator, custom forks, and local scripts

## Project Structure

```
jamf-installomator-builder/
├── server.py           # Flask web server, SSE build streaming
├── jamf_api.py         # Jamf Classic/Pro API client
├── installomator.py    # Label discovery and name resolution
├── requirements.txt    # Python dependencies
└── templates/
    └── index.html      # Single-page web UI
```


<img width="3458" height="7294" alt="image" src="https://github.com/user-attachments/assets/44f1c075-ca57-465d-86f0-85b2d7ce93fa" />


## License

MIT

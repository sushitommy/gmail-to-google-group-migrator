# Gmail to Google Group Migrator

Migrates historical emails from a personal Gmail account to a Google Workspace Collaborative Inbox (Google Group) using the Gmail API and the Google Groups Migration API.

## Overview

| Component | Value |
|-----------|-------|
| Source Account | Configurable (Auto-detected or prompted) |
| Target Google Group | Configurable (Selected from a list or prompted) |
| Gmail Query / Labels | Configurable (Interactively selected via checkbox menu or custom query) |
| Unique Subjects | Configurable (Appends sender's name/email to prevent grouping) |
| Main Script | `migrate.py` |

The script:

1. **Interactive & CLI Configuration**: Prompts you for the source email and target group, loads them from `config.json`, or accepts them as command-line arguments.
2. **Interactive Label Checkbox Menu**: Connects to Gmail, fetches all labels, and displays them as an interactive checkbox list. You can navigate with the arrow keys, toggle with the **Spacebar**, and confirm with **Enter**.
3. **Sub-Label Expansion**: Automatically expands selected parent labels to include all of their sub-labels (e.g., selecting `Work` will also migrate `Work/Clients` and `Work/Clients/ProjectA`).
4. **Unique Subject Lines (Optional)**: Modifies raw RFC822 bytes on-the-fly to append the sender's name or email to the subject line (e.g., `Contact form [John Doe]`). This prevents Google Groups from automatically threading/grouping unrelated messages with the same subject.
5. **Auto-Detection & Selection**: Automatically detects your authenticated Gmail address and lists available Google Groups in your workspace for easy selection.
6. **Verification & Diagnostics**: Offers step-by-step connection checks (`--test`), dry-runs (`--dry-run`), and migration limits (`--limit`) to test the flow before full execution.
7. **Dual-Authentication Flow**: Authenticates your personal Gmail account (source) and your Workspace Admin account (target) separately, storing tokens in `token_source.json` and `token_target.json` respectively.
8. **Gmail Retrieval**: Retrieves all message IDs matching your selected labels/query from the source mailbox (including archive and sent items).
9. **Raw Export**: Fetches the raw RFC822/MIME content for each message via the Gmail API.
10. **Group Archive**: Uploads each message to the target Google Group via the Groups Migration API.
11. **Fault Tolerance**: Tracks progress visually using `tqdm` and logs failed messages to `failed_emails.txt`.
12. **Reporting**: Prints a clean summary report with total counts and status.

## Prerequisites

- Python 3.10 or higher
- A Google Cloud project with the required APIs enabled (see setup below)
- OAuth2 Client ID credentials (`credentials.json`) downloaded from the Google Cloud Console
- Access to the source Gmail account
- Administrator/owner permissions on the target Google Group in Google Workspace

## Installation

```bash
pip install google-auth google-auth-oauthlib google-api-python-client tqdm
```

Or using a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install google-auth google-auth-oauthlib google-api-python-client tqdm
```

---

## Google Cloud Console Setup

To connect to the Google APIs, you must set up a Google Cloud project and generate OAuth2 credentials.

### 1. Create a Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project dropdown in the top navigation bar and select **New Project**.
3. Name your project (e.g., `Gmail to Google Group Migrator`) and click **Create**.

### 2. Enable APIs

You must enable three specific APIs in your Cloud project:

1. Go to **APIs & Services → Library** using the left-hand menu.
2. Search for and click on the following APIs, then click **Enable**:
   - **Gmail API** (used to read emails from the source account)
   - **Groups Migration API** (used to push emails to the target Google Group)
   - **Admin SDK API** (used to retrieve the list of Google Groups in your workspace)

### 3. Configure the OAuth Consent Screen

Before generating credentials, you must configure how the authentication screen looks:

1. Go to **APIs & Services → OAuth consent screen**.
2. Choose **External** (or **Internal** if you are running this entirely within your own Google Workspace organization). Click **Create**.
3. Fill in the required fields:
   - **App name**: `Gmail to Google Group Migrator`
   - **User support email**: Your email address
   - **Developer contact information**: Your email address
4. Click **Save and Continue**.
5. **Scopes Step**: Click **Add or Remove Scopes** and add these three scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/apps.groups.migration`
   - `https://www.googleapis.com/auth/admin.directory.group.readonly`
6. Click **Save and Continue**.

---

## Troubleshooting Access & Permissions (CRITICAL)

When setting up and running the script, you are highly likely to encounter two common Google security blocks. Follow these instructions to resolve them.

### Roadblock 1: `AccessDeniedError: (access_denied)` (Personal Gmail Block)
If you log in with your personal Gmail account and get an `AccessDeniedError` in the terminal, it means Google is blocking the login because your email has not been added as an authorized test user in your Google Cloud project.

#### **How to fix it:**
1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Select your project in the top-left dropdown.
3. In the left-hand menu, navigate to **APIs & Services → OAuth consent screen**.
4. Scroll down to the **Test users** section.
5. Click **+ ADD USERS**.
6. Type your personal Gmail address (e.g., `your-source@gmail.com`) and click **Save**.
   *Note: Ensure there are no trailing spaces or typos when entering the email address.*

---

### Roadblock 2: "Access blocked: Your institution's admin needs to review this app..." (Workspace Block)
If you log in with your Google Workspace account (e.g., your organization admin account) and see a screen saying:
> *"Access blocked: Your institution's admin needs to review this app before you can access it."*

Google Workspace blocks unverified third-party apps by default. You must explicitly trust the app in your Google Workspace Admin Console.

#### **How to fix it:**
1. Log in to the [Google Admin Console](https://admin.google.com/) as a **Super Administrator**.
2. Go to **Security → Access and data control → API controls**.
3. Click **Manage Third-Party App Access**.
4. Click **Add app** and select **OAuth App Name Or Client ID**.
5. Paste your **OAuth Client ID** and click **Search**.
   *(You can find this in your `credentials.json` file under `client_id`. It looks like `624179796066-42v3a0e8msr6dgl1pmdrraqq6nsuq306.apps.googleusercontent.com`).*
6. Click **Select** next to your app, tick the Client ID checkbox, and click **Select**.
7. Choose **All users** and click **Continue**.
8. Select **Trusted** (Can access all Google services) and click **Continue** / **Finish**.

---

## Google Workspace Group Configuration

To allow the Groups Migration API to write to your Google Group:

1. Log in to the [Google Admin Console](https://admin.google.com/) as a Super Administrator.
2. Go to **Apps → Google Workspace → Groups for Business**.
3. Ensure the target group exists and is configured as a **Collaborative Inbox**.
4. **Enable API Access**:
   - Go to **Security → Access and data control → API controls**.
   - Ensure that API access is enabled for your domain.
5. **Group Permissions**:
   - The Google account you log in with during the OAuth browser flow **must** have permission to post/archive messages to the target Google Group. Usually, being a Group Owner, Manager, or a Workspace Admin is sufficient.

---

## Step-by-Step Verification & Diagnostics

Before migrating thousands of emails, you can verify that every single step of the API and authentication flow works correctly.

### Step 1: Run Diagnostics (`--test`)

Run the script with the `--test` flag to perform a safe connection test. This will:
- Authenticate and establish connections.
- Verify Gmail API read access by fetching your profile and printing the total message count.
- Verify message retrieval by pulling a single message ID, fetching its raw RFC822 content, and printing its Subject/Date headers.

```bash
python migrate.py --test
```

### Step 2: Run a Dry-Run Simulation (`--dry-run`)

Run a dry-run to simulate the entire migration process. The script will fetch all message IDs, download their raw content, and display exactly what it *would* upload to the Google Group without actually performing any writes or modifications.

```bash
python migrate.py --dry-run
```

### Step 3: Run a Limited Test Migration (`--limit`)

To fully test the write path (uploading to the Google Group) without migrating your entire mailbox, you can limit the migration to a small number of emails (e.g., 1 or 5):

```bash
python migrate.py --limit 5
```

Once you verify that these emails have successfully appeared in your Google Group, you are ready to run the full migration!

---

## Usage & Configuration

The script provides three flexible ways to configure the `SOURCE_EMAIL`, `GROUP_ID`, `GMAIL_QUERY`, and `UNIQUE_SUBJECTS`:

### 1. Interactive Mode (Default)

Simply run the script with no arguments:

```bash
python migrate.py
```

If no configuration file is found, the script will guide you through an interactive setup:

- **Source Email**: It will attempt to connect to Gmail and auto-detect your authenticated email address. You can press **Enter** to accept it or type a custom email.
- **Target Google Group**: It will connect to the Admin SDK and fetch all Google Groups in your workspace. It will display them as a numbered list:
  ```
  Available Google Groups:
    1. Customer Support (support@yourdomain.com)
    2. Info Desk (info@yourdomain.com)
    3. Enter a custom Google Group email address
  Select a group (1-3) [1]:
  ```
  Simply type the number of the group you want to migrate to, or select the last option to type a custom email address manually.
- **Label / Filter Selection**: It will fetch all labels from your Gmail account and present them in an interactive checkbox list.
  - Use **Up/Down arrow keys** to navigate.
  - Use **Spacebar** to toggle a label on/off (`[*]`).
  - Use **Enter** to confirm.
  - **Sub-Label Expansion**: If you select a parent label (e.g., `Work`), the script will automatically detect and include all sub-labels (e.g., `Work/Clients` and `Work/Clients/ProjectA`).
- **Unique Subjects**: It will ask if you want to append the sender's name/email to the subject line to prevent Google Groups threading:
  ```
  Do you want to append the sender's name/email to the subject line to prevent Google Groups from grouping/threading them? (y/n) [n]:
  ```
- **Save Configuration**: It will ask if you want to save these settings to `config.json` so you don't have to enter them again.

### 2. Command-Line Arguments

You can bypass prompts by passing the settings directly as arguments:

```bash
python migrate.py --source user@gmail.com --group group@domain.com --query "label:Work" --unique-subjects
```

To save these arguments to `config.json` for future runs, add the `--save` flag:

```bash
python migrate.py --source user@gmail.com --group group@domain.com --query "label:Work" --unique-subjects --save
```

### 3. Configuration File (`config.json`)

You can create or edit `config.json` manually in the same directory:

```json
{
    "SOURCE_EMAIL": "user@gmail.com",
    "GROUP_ID": "group@domain.com",
    "GMAIL_QUERY": "label:\"Work\" OR label:\"Work/Clients\"",
    "UNIQUE_SUBJECTS": true
}
```

If `config.json` exists, the script will load these values automatically and run without prompting.

---

### First Run (OAuth Login)

When running the script for the first time:

1. **Two browser windows** will open sequentially.
2. Log in to the **first window** with your **source Gmail account** (personal Gmail).
3. Log in to the **second window** with your **Workspace Admin account** (or group owner/manager account).
4. The script will save the authorized credentials to `token_source.json` and `token_target.json` for future runs.

### Subsequent Runs

The script will automatically use the cached tokens to authenticate; no browser login is required unless the tokens expire or are revoked.

## Output and Logs

| File | Description |
|------|-------------|
| `token_source.json` | Caches your source Gmail OAuth2 tokens (automatically created). |
| `token_target.json` | Caches your target Workspace OAuth2 tokens (automatically created). |
| `config.json` | Stores your source, target, query, and unique subject configurations. |
| `failed_emails.txt` | Logs message IDs and error details of any failed migrations. |
| Terminal | Visual progress bar (`tqdm`) and final summary report. |

### Final Report Example

```
============================================================
  MIGRATION FINAL REPORT
============================================================
  Source Account:        your-source@gmail.com
  Target Google Group:   your-group@yourdomain.com
  Gmail Query:           label:"Work" OR label:"Work/Clients"
  Unique Subjects:       Enabled
============================================================
  Total Found:           1250
  Successfully Migrated: 1248
  Failed:                2
  Error Log File:        failed_emails.txt
============================================================
  Status:                WARNING
============================================================
```

## Error Handling & Troubleshooting

- **Exponential Back-off**: HTTP `429` (rate limits) and `5xx` (server errors) are automatically retried up to 5 times with randomized jitter.
- **Per-Message Fault Tolerance**: If a message fails to migrate, its ID and error are logged to `failed_emails.txt`, and the script immediately moves to the next message without crashing.
- **Idempotency & Deduplication**: The Google Groups Migration API automatically deduplicates messages based on the `Message-ID` header. This makes the migration script safe to restart at any time.

### Common Issues

| Issue | Solution |
|-------|----------|
| `credentials.json not found` | Download the OAuth credentials JSON from the Cloud Console and place it in the script's directory. |
| `403 Forbidden` on Groups Migration | Ensure the target group exists, the Groups Migration API is enabled in the Admin Console, and the authorized account has permissions. |
| `401 Unauthorized` | Delete `token_source.json` and `token_target.json` and run the script again to re-authenticate. |
| Rate Limits (`429`) | The script will automatically back off and retry. Avoid running multiple migration scripts in parallel. |
| Consent Screen Blocked | Ensure the source account is added as a test user in the OAuth consent screen settings. |

## Security Best Practices

- **Never** commit or share `credentials.json`, `token_*.json`, or `config.json`.
- Add these files to your `.gitignore`:

```gitignore
credentials.json
token_source.json
token_target.json
config.json
failed_emails.txt
venv/
```

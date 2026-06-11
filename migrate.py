#!/usr/bin/env python3
"""
Migrates historical emails from a personal Gmail account to a
Google Workspace Collaborative Inbox (Google Group) using the Gmail API and
Groups Migration API.

Installation:
    pip install google-auth google-auth-oauthlib google-api-python-client tqdm
"""

import argparse
import base64
import email
import io
import json
import random
import re
import sys
import time
from datetime import datetime
from email.policy import default
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Constants & Default Configuration
# ---------------------------------------------------------------------------

SOURCE_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TARGET_SCOPES = [
    "https://www.googleapis.com/auth/apps.groups.migration",
    "https://www.googleapis.com/auth/admin.directory.group.readonly",
]

CREDENTIALS_FILE = "credentials.json"
TOKEN_SOURCE_FILE = "token_source.json"
TOKEN_TARGET_FILE = "token_target.json"
CONFIG_FILE = "config.json"
FAILED_LOG_FILE = "failed_emails.txt"

MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def validate_email(email_str: str) -> bool:
    """Validates if a string has a valid email format."""
    return bool(EMAIL_REGEX.match(email_str.strip()))


def prompt_email(prompt_text: str, default_value: str = "") -> str:
    """Interactively prompts the user for an email address with validation."""
    while True:
        display_default = f" [{default_value}]" if default_value else ""
        try:
            user_input = input(f"{prompt_text}{display_default}: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled by user.")
            sys.exit(0)

        if not user_input and default_value:
            return default_value

        if validate_email(user_input):
            return user_input

        print("Invalid email format. Please try again.")


def getch() -> str:
    """Reads a single keypress from the terminal in a cross-platform way."""
    try:
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):  # Arrow keys on Windows
            ch2 = msvcrt.getch()
            if ch2 == b'H':
                return '\x1b[A'  # Up
            if ch2 == b'P':
                return '\x1b[B'  # Down
        if ch == b' ':
            return ' '
        if ch in (b'\r', b'\n'):
            return '\n'
        try:
            return ch.decode('utf-8')
        except UnicodeDecodeError:
            return ''
    except ImportError:
        import tty
        import termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
            if ch == '\x1b':  # Escape sequence
                ch += sys.stdin.read(2)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def authenticate(token_file: str, scopes: list[str], description: str) -> Credentials:
    """Loads or retrieves OAuth2 credentials for a specific role and saves them."""
    creds: Credentials | None = None
    token_path = Path(token_file)
    credentials_path = Path(CREDENTIALS_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                print(
                    f"Error: '{CREDENTIALS_FILE}' not found.\n"
                    "Please download OAuth2 credentials from the Google Cloud Console "
                    "and place the file in the same directory as this script."
                )
                sys.exit(1)
            
            print(f"\n🔑 [OAuth Flow] Authorizing {description}...")
            print(f"Please log in using the browser window that opens.")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), scopes
            )
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# API Helper Functions
# ---------------------------------------------------------------------------


def is_retryable_http_error(error: HttpError) -> bool:
    """Determines if an HttpError is retryable (status 429 or 5xx)."""
    status = error.resp.status
    return status == 429 or status >= 500


def execute_with_backoff(request, description: str = "API call") -> dict:
    """
    Executes a Google API request with exponential back-off for 429/5xx errors.
    """
    backoff = INITIAL_BACKOFF_SECONDS
    last_error: HttpError | None = None

    for attempt in range(MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as error:
            if is_retryable_http_error(error) and attempt < MAX_RETRIES - 1:
                jitter = random.uniform(0, backoff * 0.1)
                wait_time = min(backoff + jitter, MAX_BACKOFF_SECONDS)
                print(
                    f"\n{description}: HTTP {error.resp.status} – "
                    f"retry {attempt + 1}/{MAX_RETRIES} in {wait_time:.1f}s..."
                )
                time.sleep(wait_time)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                last_error = error
            else:
                raise

    if last_error:
        raise last_error
    raise RuntimeError(f"{description}: Unexpected error after retries")


def fetch_available_groups(creds) -> list[dict]:
    """
    Attempts to fetch Google Groups that the authenticated user has access to
    using the Admin SDK Directory API. Returns a list of group dicts or empty list.
    """
    try:
        admin_service = build("admin", "directory_v1", credentials=creds)
        
        # Try listing groups where the user is a member/owner first (safer, requires fewer privileges)
        groups_list = []
        try:
            request = admin_service.groups().list(userKey="me", maxResults=200)
            response = execute_with_backoff(request, "Admin SDK groups.list (userKey=me)")
            groups_list = response.get("groups", [])
        except Exception:
            # Fallback: Try listing all groups in the customer account (requires admin privileges)
            request = admin_service.groups().list(customer="my_customer", maxResults=200)
            response = execute_with_backoff(request, "Admin SDK groups.list (customer=my_customer)")
            groups_list = response.get("groups", [])
            
        return [{"email": g["email"], "name": g.get("name", g["email"])} for g in groups_list if "email" in g]
    except Exception as e:
        # Gracefully handle cases where Admin SDK is not enabled or user lacks permissions
        print(f"\nNote: Could not automatically list Google Groups ({e}).")
        print("Make sure the 'Admin SDK API' is enabled in your Google Cloud Console.")
        return []


def fetch_gmail_labels(gmail_service) -> list[dict]:
    """Fetches all labels from the source Gmail account."""
    try:
        request = gmail_service.users().labels().list(userId="me")
        response = execute_with_backoff(request, "Gmail labels.list")
        return response.get("labels", [])
    except Exception as e:
        print(f"Warning: Could not fetch Gmail labels: {e}")
        return []


def prompt_label_selection(labels: list[dict]) -> str:
    """
    Presents labels to the user, lets them select one or more interactively
    using the Spacebar to toggle and Enter to confirm, and returns a Gmail search
    query string (q) matching those labels and their sub-labels.
    """
    if not labels:
        return ""

    # Filter and sort user and system labels
    user_labels = sorted([l for l in labels if l.get("type") == "user"], key=lambda x: x["name"].lower())
    system_labels = sorted([l for l in labels if l.get("type") == "system"], key=lambda x: x["name"].lower())

    # Build the list of selectable options
    options = [
        {"name": "ALL_MAIL", "display": "[ALL MAIL] Migrate all emails (no label filter)", "selected": False, "is_all_mail": True, "is_no_label": False},
        {"name": "NO_LABEL", "display": "[UNLABELED] Migrate only emails without any user labels", "selected": False, "is_all_mail": False, "is_no_label": True}
    ]
    
    for l in user_labels:
        options.append({"name": l["name"], "display": l["name"], "selected": False, "is_all_mail": False, "is_no_label": False})
        
    useful_systems = ["INBOX", "SENT", "STARRED", "UNREAD", "IMPORTANT"]
    for l in system_labels:
        if l["name"] in useful_systems:
            options.append({"name": l["name"], "display": f"{l['name']} (System)", "selected": False, "is_all_mail": False, "is_no_label": False})

    cursor_idx = 0

    print("\nSelect Gmail labels to migrate:")
    print("Use Up/Down arrow keys to navigate, SPACE to select/deselect, ENTER to confirm.\n")

    # Hide cursor
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while True:
            # Render options
            for idx, opt in enumerate(options):
                cursor = ">" if idx == cursor_idx else " "
                checked = "[*]" if opt["selected"] else "[ ]"
                sys.stdout.write(f"\r\033[K {cursor} {checked} {opt['display']}\n")
            sys.stdout.flush()

            # Wait for keypress
            ch = getch()

            if ch == ' ':  # Spacebar
                if options[cursor_idx]["is_all_mail"]:
                    is_sel = not options[cursor_idx]["selected"]
                    for opt in options:
                        opt["selected"] = False
                    options[cursor_idx]["selected"] = is_sel
                else:
                    # Deselect ALL_MAIL if we select anything else
                    options[0]["selected"] = False
                    options[cursor_idx]["selected"] = not options[cursor_idx]["selected"]
            elif ch in ('\x1b[A', 'k'):  # Up arrow or 'k'
                cursor_idx = (cursor_idx - 1) % len(options)
            elif ch in ('\x1b[B', 'j'):  # Down arrow or 'j'
                cursor_idx = (cursor_idx + 1) % len(options)
            elif ch in ('\r', '\n'):  # Enter
                break

            # Move cursor back up to redraw options
            sys.stdout.write(f"\033[{len(options)}A")
            sys.stdout.flush()

    finally:
        # Restore cursor
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        print()  # Move past the menu

    # Process selected labels
    selected_labels = [opt for opt in options if opt["selected"] and not opt["is_all_mail"] and not opt["is_no_label"]]
    unlabeled_selected = any(opt["selected"] for opt in options if opt["is_no_label"])
    
    if any(opt["selected"] for opt in options if opt["is_all_mail"]):
        print("Selected: ALL MAIL (no label filter)")
        return ""

    # Construct Gmail search query (q)
    query_parts = []
    
    if selected_labels:
        selected_names = [l["name"] for l in selected_labels]
        
        # Find sub-labels (only for user labels, as system labels don't have hierarchy)
        expanded_names = set(selected_names)
        for sel in selected_names:
            # If it's a user label, find any label starting with "sel/"
            for l in user_labels:
                name = l["name"]
                if name.startswith(sel + "/"):
                    expanded_names.add(name)

        print(f"\nSelected labels (including sub-labels):")
        for name in sorted(expanded_names):
            print(f"  - {name}")

        # Add labels to query parts
        label_query = " OR ".join(f'label:"{name}"' for name in expanded_names)
        # Wrap in parentheses if we are combining with unlabeled
        if unlabeled_selected:
            query_parts.append(f"({label_query})")
        else:
            query_parts.append(label_query)

    if unlabeled_selected:
        print("Selected: UNLABELED (emails without any user labels)")
        query_parts.append("has:nouserlabels")

    if not query_parts:
        print("Selected: ALL MAIL (no label filter)")
        return ""

    # Combine with OR so we get emails that match EITHER the selected labels OR are unlabeled
    q_query = " OR ".join(query_parts)
    return q_query


def list_all_message_ids(gmail_service, q: str = "", limit: int = 0) -> list[str]:
    """Retrieves message IDs from the source mailbox matching a query."""
    message_ids: list[str] = []
    page_token: str | None = None

    while True:
        max_results = min(500, limit - len(message_ids)) if limit > 0 else 500
        if limit > 0 and max_results <= 0:
            break

        request = gmail_service.users().messages().list(
            userId="me",
            pageToken=page_token,
            maxResults=max_results,
            q=q if q else None,
        )
        response = execute_with_backoff(request, "Gmail messages.list")
        messages = response.get("messages", [])
        message_ids.extend(msg["id"] for msg in messages)
        
        if limit > 0 and len(message_ids) >= limit:
            message_ids = message_ids[:limit]
            break

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return message_ids


def fetch_raw_message(gmail_service, message_id: str) -> bytes:
    """Retrieves the raw RFC822/MIME content of a message."""
    request = gmail_service.users().messages().get(
        userId="me",
        id=message_id,
        format="raw",
    )
    response = execute_with_backoff(request, f"Gmail get raw ({message_id})")
    raw_data = response.get("raw")
    if not raw_data:
        raise ValueError(f"No raw data for message {message_id}")
    return base64.urlsafe_b64decode(raw_data)


def push_to_group(groups_service, raw_bytes: bytes, group_id: str) -> dict:
    """Pushes an RFC822 message to the target Google Group."""
    media = MediaIoBaseUpload(
        io.BytesIO(raw_bytes),
        mimetype="message/rfc822",
        resumable=False,
    )
    request = groups_service.archive().insert(
        groupId=group_id,
        media_body=media,
    )
    return execute_with_backoff(request, f"Groups Migration insert ({group_id})")


# ---------------------------------------------------------------------------
# Configuration Setup
# ---------------------------------------------------------------------------


def load_and_setup_config(gmail_service, target_creds, args) -> tuple[str, str, str]:
    """
    Loads configuration from CLI arguments, config.json, auto-detection, or interactive prompts.
    Saves configuration to config.json if requested or prompted.
    Returns a tuple of (source_email, group_id, gmail_query).
    """
    # 1. Try to load existing config from config.json
    config_path = Path(CONFIG_FILE)
    saved_config = {}
    if config_path.exists():
        try:
            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: Failed to read {CONFIG_FILE}: {e}")

    # 2. Determine source email
    source_email = args.source or saved_config.get("SOURCE_EMAIL", "")
    prompted_source = False
    
    if not source_email or not validate_email(source_email):
        # Attempt auto-detection via Gmail API
        try:
            profile_request = gmail_service.users().getProfile(userId="me")
            profile = execute_with_backoff(profile_request, "Gmail getProfile")
            detected_email = profile.get("emailAddress")
            if detected_email and validate_email(detected_email):
                print(f"Auto-detected Source Gmail: {detected_email}")
                use_detected = ""
                try:
                    use_detected = input("Use this as the source email? (y/n) [y]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    print("\nOperation cancelled.")
                    sys.exit(0)
                
                if use_detected in ("", "y", "yes"):
                    source_email = detected_email
                else:
                    source_email = prompt_email("Enter Source Gmail Address", "your-source@gmail.com")
                    prompted_source = True
            else:
                source_email = prompt_email("Enter Source Gmail Address", "your-source@gmail.com")
                prompted_source = True
        except Exception:
            source_email = prompt_email("Enter Source Gmail Address", "your-source@gmail.com")
            prompted_source = True

    # 3. Determine group ID
    group_id = args.group or saved_config.get("GROUP_ID", "")
    prompted_group = False
    
    if not group_id or not validate_email(group_id):
        # Attempt to fetch available groups using target credentials
        print("\nFetching available Google Groups in your workspace...")
        groups = fetch_available_groups(target_creds)
        
        if groups:
            print("\nAvailable Google Groups:")
            for idx, g in enumerate(groups, 1):
                print(f"  {idx}. {g['name']} ({g['email']})")
            print(f"  {len(groups) + 1}. Enter a custom Google Group email address")
            
            while True:
                try:
                    choice_str = input(f"Select a group (1-{len(groups) + 1}) [1]: ").strip()
                except (KeyboardInterrupt, EOFError):
                    print("\nOperation cancelled.")
                    sys.exit(0)
                
                if not choice_str:
                    choice = 1
                else:
                    try:
                        choice = int(choice_str)
                    except ValueError:
                        print("Invalid selection. Please enter a number.")
                        continue
                
                if 1 <= choice <= len(groups):
                    group_id = groups[choice - 1]["email"]
                    prompted_group = True
                    break
                elif choice == len(groups) + 1:
                    group_id = prompt_email("Enter Target Google Group Email", "your-group@yourdomain.com")
                    prompted_group = True
                    break
                else:
                    print(f"Please enter a number between 1 and {len(groups) + 1}.")
        else:
            group_id = prompt_email("Enter Target Google Group Email", "your-group@yourdomain.com")
            prompted_group = True

    # 4. Determine Gmail Query / Label Selection
    gmail_query = args.query or saved_config.get("GMAIL_QUERY", "")
    prompted_query = False
    
    if not gmail_query and not args.query:
        # Fetch labels and prompt user to select
        print("\nFetching available Gmail labels...")
        labels = fetch_gmail_labels(gmail_service)
        if labels:
            gmail_query = prompt_label_selection(labels)
            prompted_query = True

    # 5. Save config if prompted, or if --save flag is passed
    should_save = args.save
    
    # 6. Ask if we should make subjects unique (if not already specified in CLI)
    unique_subjects = args.unique_subjects or saved_config.get("UNIQUE_SUBJECTS", False)
    prompted_unique = False
    if not args.unique_subjects and "UNIQUE_SUBJECTS" not in saved_config:
        try:
            unique_choice = input("\nDo you want to append the sender's name/email to the subject line to prevent Google Groups from grouping/threading them? (y/n) [n]: ").strip().lower()
            unique_subjects = unique_choice in ("y", "yes")
            prompted_unique = True
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.")
            sys.exit(0)

    if (prompted_source or prompted_group or prompted_query or prompted_unique) and not should_save:
        try:
            save_choice = input("\nDo you want to save these settings to config.json? (y/n) [y]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.")
            sys.exit(0)
        should_save = save_choice in ("", "y", "yes")

    if should_save:
        config_data = {
            "SOURCE_EMAIL": source_email,
            "GROUP_ID": group_id,
            "GMAIL_QUERY": gmail_query,
            "UNIQUE_SUBJECTS": unique_subjects
        }
        try:
            config_path.write_text(json.dumps(config_data, indent=4), encoding="utf-8")
            print(f"Configuration saved to {CONFIG_FILE}")
        except Exception as e:
            print(f"Warning: Failed to save configuration to {CONFIG_FILE}: {e}")

    return source_email, group_id, gmail_query, unique_subjects


def make_raw_message_subject_unique(raw_bytes: bytes) -> bytes:
    """
    Parses a raw RFC822 message, extracts the sender's name/email,
    and appends it to the Subject header to prevent Google Groups threading.
    Returns the modified raw bytes.
    """
    try:
        # Parse the message
        msg = email.message_from_bytes(raw_bytes, policy=default)
        
        # Extract From header
        from_header = msg.get("From", "")
        sender_info = ""
        if from_header:
            # Parse sender info (e.g., "John Doe <john@example.com>")
            parsed_from = email.utils.parseaddr(from_header)
            sender_name, sender_email = parsed_from
            sender_info = sender_name if sender_name else sender_email
            
        if sender_info:
            # Clean up sender info to be short and clear
            sender_info = sender_info.strip().replace("[", "").replace("]", "")
            
            # Extract and update Subject header
            subject = msg.get("Subject", "").strip()
            new_subject = f"{subject} [{sender_info}]" if subject else f"[{sender_info}]"
            
            # Update the header in the message object
            # (This safely handles encoding of any special characters)
            del msg["Subject"]
            msg["Subject"] = new_subject
            
            # Return the modified bytes
            return msg.as_bytes()
    except Exception as e:
        print(f"\nWarning: Failed to make subject unique: {e}")
        
    return raw_bytes


# ---------------------------------------------------------------------------
# Diagnostics & Verification Mode
# ---------------------------------------------------------------------------


def run_diagnostics(gmail_service, groups_service, source_email: str, group_id: str, gmail_query: str) -> None:
    """Runs step-by-step connection and read-access diagnostic checks."""
    print("\n" + "=" * 60)
    print("  RUNNING DIAGNOSTICS & VERIFICATION")
    print("=" * 60)

    # Step 1: Test Gmail API Connection & Profile
    print("\n[Step 1/3] Testing Gmail API Connection...")
    try:
        profile_request = gmail_service.users().getProfile(userId="me")
        profile = execute_with_backoff(profile_request, "Gmail getProfile")
        print(f"  ✓ Connected successfully!")
        print(f"  ✓ Authenticated Email: {profile.get('emailAddress')}")
        print(f"  ✓ Total Messages in Mailbox: {profile.get('messagesTotal')}")
    except Exception as e:
        print(f"  ✗ Gmail API Connection Failed: {e}")
        return

    # Step 2: Test Gmail API Read Access (Fetch 1 Message matching query)
    print("\n[Step 2/3] Testing Gmail API Read Access...")
    if gmail_query:
        print(f"  ℹ Using Gmail Query: {gmail_query}")
    try:
        list_request = gmail_service.users().messages().list(userId="me", maxResults=1, q=gmail_query if gmail_query else None)
        list_resp = execute_with_backoff(list_request, "Gmail messages.list (test)")
        messages = list_resp.get("messages", [])
        if not messages:
            print("  ✓ Connected, but no messages match the selected labels/query.")
        else:
            test_msg_id = messages[0]["id"]
            print(f"  ✓ Found at least one matching message (ID: {test_msg_id})")
            raw_bytes = fetch_raw_message(gmail_service, test_msg_id)
            print(f"  ✓ Successfully fetched raw message content ({len(raw_bytes)} bytes)")
            
            # Parse and show headers for verification
            msg = email.message_from_bytes(raw_bytes, policy=default)
            print(f"  ✓ Message Date: {msg.get('Date', '(No Date)')}")
            print(f"  ✓ Message Subject: {msg.get('Subject', '(No Subject)')}")
    except Exception as e:
        print(f"  ✗ Gmail API Read Access Failed: {e}")
        return

    # Step 3: Verify Groups Migration API Setup
    print("\n[Step 3/3] Verifying Groups Migration API Setup...")
    print(f"  ℹ Target Group: {group_id}")
    print("  ℹ Note: The Groups Migration API does not provide a read-only 'test' endpoint.")
    print("  ℹ To verify full write permissions, you can run a single-message test migration:")
    print(f"      python migrate.py --limit 1")
    print("  ℹ Or run a dry-run to simulate the migration without writing:")
    print(f"      python migrate.py --dry-run")
    
    print("\n" + "=" * 60)
    print("  DIAGNOSTICS COMPLETED SUCCESSFULLY")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Error Logging
# ---------------------------------------------------------------------------


def log_failure(message_id: str, error: Exception) -> None:
    """Writes a failed message ID and error message to failed_emails.txt."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    with open(FAILED_LOG_FILE, "a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} | {message_id} | {error}\n")


# ---------------------------------------------------------------------------
# Final Report
# ---------------------------------------------------------------------------


def print_final_report(
    source_email: str,
    group_id: str,
    total_found: int,
    total_success: int,
    total_failed: int,
    dry_run: bool = False,
    gmail_query: str = "",
    unique_subjects: bool = False,
) -> None:
    """Prints a clear summary report in the terminal."""
    report_title = "MIGRATION DRY-RUN REPORT" if dry_run else "MIGRATION FINAL REPORT"
    status = "SUCCESS" if total_failed == 0 else "WARNING"
    separator = "=" * 60

    print()
    print(separator)
    print(f"  {report_title}")
    print(separator)
    print(f"  Source Account:        {source_email}")
    print(f"  Target Google Group:   {group_id}")
    if gmail_query:
        print(f"  Gmail Query:           {gmail_query}")
    print(f"  Unique Subjects:       {'Enabled' if unique_subjects else 'Disabled'}")
    print(separator)
    print(f"  Total Found/Processed: {total_found}")
    print(f"  Successfully Migrated: {total_success}")
    print(f"  Failed:                {total_failed}")
    if total_failed > 0:
        print(f"  Error Log File:        {FAILED_LOG_FILE}")
    print(separator)
    print(f"  Status:                {status}")
    print(separator)
    print()


# ---------------------------------------------------------------------------
# Main Program
# ---------------------------------------------------------------------------


def main() -> None:
    print("Gmail → Google Group Migration")
    print("==============================")

    # 1. Parse CLI arguments first
    parser = argparse.ArgumentParser(
        description="Migrate emails from a Gmail account to a Google Group with verification steps."
    )
    parser.add_argument(
        "-s", "--source",
        help="Source Gmail address (e.g., user@gmail.com)"
    )
    parser.add_argument(
        "-g", "--group",
        help="Target Google Group email address (e.g., group@domain.com)"
    )
    parser.add_argument(
        "-q", "--query",
        help="Gmail search query to filter messages (e.g., 'label:Work')"
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save the provided command-line arguments to config.json"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run connection and read-access diagnostic checks without migrating."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and process emails, but do not upload them to the Google Group."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit the number of emails to migrate (useful for testing)."
    )
    parser.add_argument(
        "--unique-subjects",
        action="store_true",
        help="Append the sender's name/email to the subject line to prevent Google Groups from threading them."
    )
    args = parser.parse_args()

    if args.dry_run:
        print("⚠️  DRY-RUN MODE ACTIVE: No emails will be uploaded to the Google Group.")

    # 2. Authenticate BOTH accounts separately
    source_creds = authenticate(TOKEN_SOURCE_FILE, SOURCE_SCOPES, "Source Gmail Account (personal Gmail)")
    target_creds = authenticate(TOKEN_TARGET_FILE, TARGET_SCOPES, "Target Workspace Account (admin/group owner)")

    gmail_service = build("gmail", "v1", credentials=source_creds)
    groups_service = build("groupsmigration", "v1", credentials=target_creds)

    # 3. Load configuration (passing services so we can auto-detect, list groups, and select labels)
    source_email, group_id, gmail_query, unique_subjects = load_and_setup_config(gmail_service, target_creds, args)

    print()
    print(f"Source: {source_email}")
    print(f"Target: {group_id}")
    if gmail_query:
        print(f"Query:  {gmail_query}")
    print(f"Unique Subjects: {'Enabled' if unique_subjects else 'Disabled'}")
    print()

    # Run diagnostics if --test flag is set
    if args.test:
        run_diagnostics(gmail_service, groups_service, source_email, group_id, gmail_query)
        return

    print("Retrieving messages from source mailbox...")
    message_ids = list_all_message_ids(gmail_service, q=gmail_query, limit=args.limit)
    total_found = len(message_ids)
    
    if args.limit > 0:
        print(f"Limited to first {args.limit} messages (Total found: {total_found}).\n")
    else:
        print(f"{total_found} messages found.\n")

    if total_found == 0:
        print_final_report(
            source_email=source_email,
            group_id=group_id,
            total_found=0,
            total_success=0,
            total_failed=0,
            dry_run=args.dry_run,
            gmail_query=gmail_query,
            unique_subjects=unique_subjects
        )
        return

    total_success = 0
    total_failed = 0

    desc_text = "Simulating" if args.dry_run else "Migrating"
    with tqdm(total=total_found, desc=desc_text, unit="mail") as progress_bar:
        for message_id in message_ids:
            try:
                raw_bytes = fetch_raw_message(gmail_service, message_id)
                
                # Apply unique subjects if enabled
                if unique_subjects:
                    raw_bytes = make_raw_message_subject_unique(raw_bytes)
                
                if args.dry_run:
                    # In dry-run, parse headers and print info to show it works
                    msg = email.message_from_bytes(raw_bytes, policy=default)
                    subject = msg.get("Subject", "(No Subject)")
                    progress_bar.write(f"[Dry-Run] Would migrate: Subject: {subject} ({len(raw_bytes)} bytes)")
                else:
                    push_to_group(groups_service, raw_bytes, group_id)
                
                total_success += 1
            except (HttpError, ValueError, OSError) as error:
                total_failed += 1
                log_failure(message_id, error)
            except Exception as error:
                total_failed += 1
                log_failure(message_id, error)
            finally:
                progress_bar.update(1)

    print_final_report(
        source_email=source_email,
        group_id=group_id,
        total_found=total_found,
        total_success=total_success,
        total_failed=total_failed,
        dry_run=args.dry_run,
        gmail_query=gmail_query,
        unique_subjects=unique_subjects
    )


if __name__ == "__main__":
    main()
